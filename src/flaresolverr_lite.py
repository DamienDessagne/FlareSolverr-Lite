import asyncio
import mimetypes
import os
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from urllib.parse import urlparse

# Cross-platform input handling (Windows/Linux)
try:
    import msvcrt
except ImportError:
    import select

    msvcrt = None

import nodriver as uc
from aiohttp import web
from nodriver import cdp

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---------------------
# --- CONFIGURATION ---
# ---------------------

PORT = 8191  # The port the server will listen on
CHROME_PROFILE = os.path.join(PROJECT_ROOT, "chrome_profile")  # The Chrome profile to use
CHROME_PATH = ""  # Path to the Chrome binary. Leave empty to let nodriver auto-detect it (see notes below)
STARTUP_DELAY_SECONDS = 90  # Time to wait before starting the server

# TIMEOUTS (Seconds)
CF_CLICK_DELAY = 20  # Delay before attempting a forced click on challenges
CHALLENGE_GIVE_UP = 60  # Give up on a challenge still unsolved after this long (keeps the browser alive)
DEFAULT_REQUEST_TIMEOUT = 180  # Default hard cap if the client doesn't provide a maxTimeout
NAV_COMMIT_TIMEOUT = 30  # Max wait for the browser to actually replace the current document
PAGE_LOAD_TIMEOUT = 15  # Max wait for the destination page to finish loading before extraction
DOM_READY_GRACE = 10  # Read a still-loading document anyway once it has been pending this long

# ---------------------

# CHALLENGE TRIGGERS
CHALLENGE_TITLES = [
    "Just a moment", "Attention Required", "Cloudflare",
    "Security Check", "DDOS-GUARD", "Access denied",
    "Checking your browser", "DDoS protection", "challenge-platform"
]

# GLOBALS
browser = None
browser_lock = asyncio.Lock()
RUNTIME_TEMP_DIR = None

# STATE TRACKING
current_download = {
    "active": False,
    "filename": None,
    "filepath": None,
    "completed": False,
    "event": asyncio.Event()
}

# --- UTILS ---
os.system('')
COLORS_MAP = {
    "[REQUEST]": "\033[94m[REQUEST]\033[0m",
    "[SUCCESS]": "\033[92m[SUCCESS]\033[0m",
    "[ERROR]": "\033[91m[ERROR]\033[0m",
    "[CRITICAL]": "\033[91m[CRITICAL]\033[0m",
    "[WARN]": "\033[93m[WARN]\033[0m",
    "[QUEUE]": "\033[95m[QUEUE]\033[0m"
}
color_msg = lambda msg: next((msg.replace(tag, colored) for tag, colored in COLORS_MAP.items() if tag in msg), msg)


def log(message):
    timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(f"[{timestamp}] {color_msg(message)}")


def smart_wait(seconds):
    if seconds <= 0:
        return

    log(f"[INFO] Waiting {seconds}s before starting...")
    print("   >> Press ENTER to SKIP waiting <<")
    for i in range(seconds, 0, -1):
        sys.stdout.write(f"\r   ... Starting in {i} seconds ...   ")
        sys.stdout.flush()
        for _ in range(10):
            skip = False
            if msvcrt:
                # Windows
                if msvcrt.kbhit():
                    msvcrt.getch()
                    skip = True
            else:
                # Linux / Unix
                if select.select([sys.stdin], [], [], 0)[0]:
                    sys.stdin.readline()
                    skip = True

            if skip:
                print("\n")
                log("[INFO] Startup wait skipped by user.")
                return
            time.sleep(0.1)
    print("\n")
    log("[INFO] Startup wait complete.")


async def force_kill_chrome():
    global browser
    log("[INFO] Maintenance: Killing old script-specific Chrome processes...")

    # Capture the PID before aclose(), which clears it on success
    pid = getattr(browser, '_process_pid', None) if browser else None

    # 1. Graceful stop attempt
    if browser:
        try:
            await browser.aclose()
        except Exception:
            pass
    browser = None

    # 2. Kill by PID: only ever touches the exact Chrome this script started. Killing the root
    #    browser process takes its renderer/gpu children down with it.
    if pid:
        try:
            if os.name == 'nt':
                subprocess.run(f'taskkill /F /T /PID {pid}', shell=True,
                               stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
            else:
                os.kill(pid, signal.SIGKILL)
        except Exception:
            pass
        await asyncio.sleep(2)
        return

    # 3. Fallback sweep when no PID is known (leftovers of a previous run, at startup): match on
    #    the full profile path so Chrome instances opened by the user are never touched.
    try:
        if os.name == 'nt':
            # Get-CimInstance works on Win10 and Win11 alike (wmic was removed in Win11 24H2+)
            ps = ("Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
                  f"Where-Object {{ $_.CommandLine -like '*{CHROME_PROFILE}*' }} | "
                  "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }")
            subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
        else:
            subprocess.run(["pkill", "-f", CHROME_PROFILE],
                           stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
        await asyncio.sleep(2)
    except Exception:
        pass


# --- CDP HANDLERS ---
async def on_download_will_begin(event: cdp.browser.DownloadWillBegin):
    log(f"[DOWNLOAD] Started: {event.suggested_filename}")
    current_download["active"] = True
    current_download["filename"] = event.suggested_filename
    current_download["filepath"] = os.path.join(RUNTIME_TEMP_DIR, event.suggested_filename)
    current_download["completed"] = False
    current_download["event"].clear()


async def on_download_progress(event: cdp.browser.DownloadProgress):
    if event.state == "completed":
        log("[DOWNLOAD] Completed.")
        current_download["completed"] = True
        current_download["event"].set()
    elif event.state == "canceled":
        log("[DOWNLOAD] Canceled.")
        current_download["active"] = False
        current_download["event"].set()


# --- BROWSER MANAGEMENT ---
async def start_browser():
    global browser
    log("[INFO] Starting Chrome instance...")

    if not RUNTIME_TEMP_DIR:
        raise Exception("Temp Directory not initialized!")

    if CHROME_PATH:
        if not os.path.isfile(CHROME_PATH) or not os.access(CHROME_PATH, os.X_OK):
            log(f"[CRITICAL] CHROME_PATH is set but is not an executable file: {CHROME_PATH}")
            return False
        log(f"[INFO] Using configured Chrome binary: {CHROME_PATH}")

    last_error = None
    for attempt in range(1, 4):
        try:
            config_args = [
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--password-store=basic",
                "--start-maximized",
                "--test-type",
                "--lang=en-US",
            ]
            browser = await uc.start(
                user_data_dir=CHROME_PROFILE,
                headless=False,
                browser_args=config_args,
                browser_executable_path=CHROME_PATH or None
            )

            try:
                # Target the main visible tab to avoid attaching to background processes/service workers
                tab = getattr(browser, 'main_tab', None)
                if not tab:
                    tab = browser.tabs[0]

                await tab.send(cdp.browser.set_download_behavior(
                    behavior="allow",
                    download_path=os.path.abspath(RUNTIME_TEMP_DIR),
                    events_enabled=True
                ))
                tab.add_handler(cdp.browser.DownloadWillBegin, on_download_will_begin)
                tab.add_handler(cdp.browser.DownloadProgress, on_download_progress)
                await tab
            except Exception as e:
                log(f"[WARN] CDP Setup warning: {e}")

            log(f"[INFO] Chrome started. Downloads routed to: {RUNTIME_TEMP_DIR}")
            return True
        except Exception as e:
            last_error = e
            log(f"[WARN] Start attempt {attempt}/3 failed: {type(e).__name__}: {e}")
            await force_kill_chrome()

    log(f"[CRITICAL] Could not start Chrome: {type(last_error).__name__}: {last_error}")
    if isinstance(last_error, FileNotFoundError) and not CHROME_PATH:
        log("[CRITICAL] Chrome was not found automatically (nodriver only scans $PATH on Linux, so a "
            "flatpak/snap install is invisible to it). Set CHROME_PATH in the CONFIGURATION section.")
    return False


async def get_main_tab():
    global browser
    # Only restart if the browser is actually dead: the process exited, or its websocket is gone/closed
    if not browser or browser.stopped or not browser.socket or bool(browser.socket.close_code):
        await force_kill_chrome()
        if not await start_browser():
            raise Exception("Browser unavailable")
    try:
        # Prioritize main_tab over tabs[0] to prevent hanging on invisible background targets
        tab = getattr(browser, 'main_tab', None)
        if not tab:
            return await browser.get("about:blank", new_tab=True, new_window=False)
        return tab
    except Exception:
        await force_kill_chrome()
        await start_browser()
        return await browser.get("about:blank", new_tab=True, new_window=False)


async def eval_js(page, expression, timeout=2.0):
    """Evaluate an expression and return its string result, or None if it could not be read.

    nodriver's evaluate() returns an ExceptionDetails/RemoteObject instead of raising when the JS
    fails or the execution context is being swapped mid-navigation, so anything that is not a
    plain string means "unknown" and must never be mistaken for an answer.
    """
    try:
        result = await asyncio.wait_for(page.evaluate(expression), timeout=timeout)
    except Exception:
        return None
    return result if isinstance(result, str) else None


async def wait_for_download_start(timeout=5.0):
    start = time.time()
    while time.time() - start < timeout:
        if current_download["active"]:
            return True
        await asyncio.sleep(0.1)
    return False


async def navigate_and_commit(page, url):
    """Navigate the tab we actually observe, and wait until the old document is really gone.

    page.get() cannot be used for this: it returns as soon as Page.navigate is acknowledged (its
    load-event wait is dead code in current nodriver), and it navigates the first page target
    instead of this tab. Both the target and the timing would be wrong. A marker planted in the
    current document tells us exactly when that document has been replaced.

    Returns (committed, error_text).
    """
    token = f"fsl_{time.time_ns()}"
    marker_set = await eval_js(page, f"window.__fsl_nav = '{token}'; window.__fsl_nav") == token

    try:
        _, _, error_text, is_download = await asyncio.wait_for(
            page.send(cdp.page.navigate(url)), timeout=15.0)
    except asyncio.TimeoutError:
        return False, "Page.navigate timed out"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"

    # A download never commits a document: the CDP handlers take over from here
    if is_download:
        if not await wait_for_download_start():
            log("[WARN] Navigation reported a download that never started.")
        return True, None
    if current_download["active"]:
        return True, None
    # ERR_ABORTED is also what a Content-Disposition download looks like when Chrome does not
    # flag it, so let the wait loop below decide between a download and a real failure
    if error_text and "ERR_ABORTED" not in error_text:
        return False, error_text

    if not marker_set:
        # about:blank, a chrome:// page or a context being torn down: no marker to watch
        log("[WARN] Could not tag the previous document, falling back to a timed wait.")
        await asyncio.sleep(1.5)
        return True, None

    start = time.time()
    while time.time() - start < NAV_COMMIT_TIMEOUT:
        if current_download["active"]:
            return True, None

        # Only an explicit 'new' proves the document was replaced. Read errors happen while the
        # context is being swapped, and must keep us waiting rather than end the wait early.
        state = await eval_js(page, f"(window.__fsl_nav === '{token}') ? 'old' : 'new'")
        if state == "new":
            return True, None

        await asyncio.sleep(0.25)

    return False, None


async def wait_for_full_load(page, timeout=PAGE_LOAD_TIMEOUT):
    start = time.time()
    while time.time() - start < timeout:
        if current_download["active"]:
            return False
        # Read failures are expected while the context is destroyed during a redirect
        if await eval_js(page, "document.readyState") == "complete":
            return True
        await asyncio.sleep(0.5)
    return False


async def detect_challenge(page):
    """True if the tab currently shows a challenge, False if not, None if it could not be read."""
    title = await eval_js(page, "document.title")
    try:
        content = await asyncio.wait_for(page.get_content(), timeout=2.0) or ""
    except Exception:
        content = ""

    if title is None and not content:
        return None

    title = title or ""
    for trigger in CHALLENGE_TITLES:
        if trigger.lower() in title.lower() or "challenge-running" in content:
            return True
    return False


async def safe_verify_cf(page):
    try:
        await asyncio.wait_for(page.verify_cf(), timeout=10.0)
        log(f"[ACTION] CloudFlare verification click attempted...")
    except Exception:
        pass


async def wait_for_completion(page):
    start_time = time.time()
    cf_challenge_detected = False
    click_triggered = False
    last_progress_log = 0

    while True:
        # 1. DOWNLOAD (Priority)
        if current_download["active"]:
            log("[INTERCEPTOR] Download detected. Waiting for completion...")
            await current_download["event"].wait()

            if current_download["completed"] and current_download["filepath"]:
                return "download", current_download["filename"], current_download["filepath"]

            if not current_download["active"]:
                log("[WARN] Download canceled, resuming page checks...")

        # 2. HTML CONTENT
        elapsed = time.time() - start_time

        # A document that just committed is still empty: reading it now would show neither a
        # challenge nor content, and would be reported as a successful blank page. Wait for the
        # parser, but never hang forever on a page whose subresources stall.
        if await eval_js(page, "document.readyState") not in ("interactive", "complete") \
                and elapsed < DOM_READY_GRACE:
            await asyncio.sleep(1)
            continue

        # None means the tab could not be read at all: stay in the loop rather than guess
        is_challenge = await detect_challenge(page)

        if is_challenge and not cf_challenge_detected:
            log("[INFO] CloudFlare challenge detected")
            cf_challenge_detected = True

        if is_challenge is False:
            # Let the page settle before deciding, then confirm: a challenge caught between two
            # of its own reloads reads as a blank, challenge-free document.
            settled = await wait_for_full_load(page)
            if current_download["active"]:
                # A download started while waiting: hand it back to the interceptor
                continue

            if await detect_challenge(page):
                if not cf_challenge_detected:
                    log("[INFO] CloudFlare challenge detected")
                    cf_challenge_detected = True
            else:
                if cf_challenge_detected:
                    log(f"[INFO] CloudFlare challenge solved after "
                        f"{'{:.2f}'.format(time.time() - start_time)}s")
                if settled:
                    log("[INFO] Destination page fully loaded and ready.")
                else:
                    log("[WARN] Destination page still loading, extracting anyway.")

                return "html", None, None

        # 3. INTERACTION (Delayed Click)
        if elapsed > CF_CLICK_DELAY and not click_triggered:
            log(f"[WAIT] Auto-solve failed after {CF_CLICK_DELAY}s. Attempting force click...")
            await safe_verify_cf(page)
            click_triggered = True

        # 4. GIVE UP on a challenge that never resolves (IP ban, hard block): returning an error
        # here keeps the browser alive instead of letting the execution timeout kill it.
        if cf_challenge_detected and click_triggered and elapsed > CHALLENGE_GIVE_UP:
            log(f"[ERROR] Challenge still unsolved after {int(elapsed)}s. Giving up (browser kept alive).")
            return "blocked", None, None

        if int(elapsed) - last_progress_log >= 5:
            last_progress_log = int(elapsed)
            log(f"[WAIT] Processing... ({int(elapsed)}s)")

        await asyncio.sleep(1)


async def process_request_in_tab(url):
    start_time = time.time()
    log(f"[REQUEST] Processing: {url}")

    # Reset State
    current_download["active"] = False
    current_download["filename"] = None
    current_download["filepath"] = None
    current_download["completed"] = False
    current_download["event"].clear()

    # Flush ephemeral download directory
    if not os.path.exists(RUNTIME_TEMP_DIR):
        try:
            os.makedirs(RUNTIME_TEMP_DIR, exist_ok=True)
            log("[WARN] Ephemeral download directory was deleted externally. Recreated.")
        except Exception as e:
            log(f"[ERROR] Could not recreate ephemeral directory: {e}")
    else:
        for f in os.listdir(RUNTIME_TEMP_DIR):
            try:
                os.remove(os.path.join(RUNTIME_TEMP_DIR, f))
            except:
                pass

    page = await get_main_tab()
    await page.bring_to_front()

    committed, nav_error = await navigate_and_commit(page, url)
    if nav_error:
        log(f"[ERROR] Navigation failed: {nav_error}")
        return {"status": "error", "message": f"Navigation failed: {nav_error}"}
    if not committed:
        # Returning here rather than reading the tab: whatever it still holds is the previous page
        log(f"[ERROR] Navigation never committed after {NAV_COMMIT_TIMEOUT}s (document unchanged).")
        return {"status": "error", "message": "Navigation did not commit"}

    result_type, filename, filepath = await wait_for_completion(page)

    # --- RESULT: BLOCKED ---
    if result_type == "blocked":
        return {"status": "error", "message": f"Challenge not solved after {CHALLENGE_GIVE_UP}s (page still blocked)"}

    # --- RESULT: DOWNLOAD ---
    if result_type == "download" and filepath:
        if os.path.exists(filepath):
            log(f"[INTERCEPTOR] Reading file: {filename}")
            try:
                # Wait briefly for OS file lock release
                await asyncio.sleep(0.5)

                with open(filepath, "rb") as f:
                    file_content = f.read()

                if len(file_content) == 0:
                    raise Exception("File is empty (0 bytes)")

                mime_type, _ = mimetypes.guess_type(filename)
                if not mime_type:
                    mime_type = "application/octet-stream"

                body_response = file_content.decode('latin-1')

                log(f"[SUCCESS] Downloaded file: {filename} ({'{:.2f}'.format(len(file_content) / 1024)}kb) in "
                    f"{'{:.2f}'.format(time.time() - start_time)}s")
                return {
                    "status": "ok",
                    "solution": {
                        "url": url,
                        "status": 200,
                        "cookies": [],
                        "userAgent": "FlareSolverr-Lite/Interceptor",
                        "response": body_response,
                        "headers": {
                            "Content-Type": mime_type,
                            "Content-Disposition": f'attachment; filename="{filename}"'
                        }
                    }
                }
            except Exception as e:
                log(f"[ERROR] Failed to read file: {e}")
                return {"status": "error", "message": str(e)}
        else:
            return {"status": "error", "message": "File not found on disk"}

    # --- RESULT: HTML ---
    elif result_type == "html":
        try:
            async def extract_data():
                # Execute raw CDP command to bypass JSON parsing issues with partitioned cookies in Chrome 130+
                def raw_get_cookies(urls):
                    cmd_dict = {
                        "method": "Network.getCookies",
                        "params": {"urls": urls}
                    }
                    response = yield cmd_dict
                    return response.get("cookies", [])

                raw_cookies = await page.send(raw_get_cookies([url]))
                ua = await page.evaluate("navigator.userAgent")
                html = await page.get_content()

                return raw_cookies, ua, html

            # Cap extraction phase to prevent stalling on continuous reloads
            cdp_cookies, user_agent, html_content = await asyncio.wait_for(extract_data(), timeout=15.0)

            cookies = []
            domain_key = urlparse(url).netloc

            for c in cdp_cookies:
                domain = c.get("domain", "")
                if domain.lstrip('.') in domain_key or domain_key in domain:
                    cookies.append({
                        "name": c.get("name"),
                        "value": c.get("value"),
                        "domain": domain,
                        "path": c.get("path", "/"),
                        "expiry": int(c.get("expires", -1))
                    })

            log(f"[SUCCESS] HTML Content retrieved in {'{:.2f}'.format(time.time() - start_time)}s. ({len(cookies)} cookies)")
            return {
                "status": "ok",
                "solution": {
                    "url": url,
                    "status": 200,
                    "cookies": cookies,
                    "userAgent": user_agent,
                    "response": html_content
                }
            }
        except asyncio.TimeoutError:
            log("[ERROR] Timeout while extracting HTML/Cookies. Page might be stuck reloading.")
            return {"status": "error", "message": "Extraction timeout (page hung)"}
        except Exception as e:
            log(f"[ERROR] Error: {str(e)}")
            return {"status": "error", "message": str(e)}

    return {"status": "error", "message": "Unknown error"}


async def solve_challenge(url, request_timeout):
    global browser
    await browser_lock.acquire()

    try:
        # Apply the execution timeout here, ensuring queue wait time does not consume the request quota
        return await asyncio.wait_for(process_request_in_tab(url), timeout=request_timeout)
    except asyncio.TimeoutError:
        log(f"[CRITICAL] EXECUTION TIMEOUT ({request_timeout}s)! Killing browser.")
        await force_kill_chrome()
        browser = None
        return {"status": "error", "message": "Execution Timeout"}
    except Exception as e:
        log(f"[CRITICAL] Browser Error: {e}. Killing Chrome...")
        await force_kill_chrome()
        browser = None
        return {"status": "error", "message": str(e)}
    finally:
        if browser_lock.locked():
            browser_lock.release()


async def handle_post(request):
    try:
        data = await request.json()
        cmd = data.get('cmd')

        max_timeout_ms = data.get('maxTimeout')
        if max_timeout_ms and isinstance(max_timeout_ms, (int, float)):
            request_timeout = max_timeout_ms / 1000.0
        else:
            request_timeout = DEFAULT_REQUEST_TIMEOUT

        if cmd in ['sessions.create', 'sessions.list']:
            return web.json_response({"status": "ok", "sessions": ["prowlarr"]})
        elif cmd == 'sessions.destroy':
            return web.json_response({"status": "ok", "message": "Session destroyed"})
        elif cmd in ['request.get', 'request.post']:
            log(f"[QUEUE] Request received: {data.get('url')} (execution timeout: {request_timeout}s)")

            # Offload execution to the queue supervisor
            return web.json_response(await solve_challenge(data.get('url'), request_timeout))

        else:
            return web.json_response({"status": "error", "message": "Command not implemented"})
    except Exception as e:
        return web.json_response({"status": "error", "message": str(e)})


async def main():
    global RUNTIME_TEMP_DIR
    print("=============================")
    print("      FLARESOLVERR-LITE      ")
    print("=============================")

    smart_wait(STARTUP_DELAY_SECONDS)
    await force_kill_chrome()

    with tempfile.TemporaryDirectory(prefix="flaresolverr_lite_dl_") as tmp_dir:
        RUNTIME_TEMP_DIR = tmp_dir
        log(f"[INIT] Ephemeral download directory: {RUNTIME_TEMP_DIR}")

        app = web.Application()
        app.router.add_post('/v1', handle_post)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', PORT)
        await site.start()
        log(f"[INFO] Server running on port {PORT} (press Ctrl-C to shutdown)...")

        if not browser:
            await start_browser()

        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            pass
        finally:
            log("[SHUTDOWN] Stopping server...")
            await force_kill_chrome()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass