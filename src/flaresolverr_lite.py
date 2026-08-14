import asyncio
import mimetypes
import os
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
STARTUP_DELAY_SECONDS = 90  # Time to wait before starting the server

# TIMEOUTS (Seconds)
CF_CLICK_DELAY = 20  # Delay before attempting a forced click on challenges
DEFAULT_REQUEST_TIMEOUT = 180  # Default hard cap if the client doesn't provide a maxTimeout

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

    # 1. Graceful stop attempt
    if browser:
        try:
            await browser.aclose()
        except Exception:
            pass
    browser = None

    # 2. Targeted Process Kill
    try:
        if os.name == 'nt':
            profile_folder = os.path.basename(CHROME_PROFILE)
            cmd = f'wmic process where "name=\'chrome.exe\' and commandline like \'%{profile_folder}%\'" call terminate'
            subprocess.run(cmd, shell=True, stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
            await asyncio.sleep(2)
        else:
            profile_folder = os.path.basename(CHROME_PROFILE)
            subprocess.run(f"pkill -f {profile_folder}", shell=True, stderr=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL)
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
                browser_args=config_args
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
            log(f"[WARN] Start attempt {attempt}/3 failed: {e}")
            await force_kill_chrome()

    log("[CRITICAL] Could not start Chrome.")
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
        try:
            # Brief timeouts prevent deadlocks if the execution context is destroyed during navigation
            title = await asyncio.wait_for(page.evaluate("document.title"), timeout=2.0) or ""
            content = await asyncio.wait_for(page.get_content(), timeout=2.0) or ""

            is_challenge = False
            for trigger in CHALLENGE_TITLES:
                if trigger.lower() in title.lower() or "challenge-running" in content:
                    is_challenge = True
                    if not cf_challenge_detected:
                        log("[INFO] CloudFlare challenge detected")
                        cf_challenge_detected = True
                    break

            if not is_challenge:
                if cf_challenge_detected:
                    log(f"[INFO] CloudFlare challenge solved after {'{:.2f}'.format(elapsed)}s")

                    # Ensure the destination page has finished loading post-redirection
                    wait_start = time.time()
                    while time.time() - wait_start < 15.0:
                        try:
                            ready_state = await page.evaluate("document.readyState")
                            if ready_state == "complete":
                                log("[INFO] Destination page fully loaded and ready.")
                                break
                        except Exception:
                            # Expected if context is destroyed during the redirect phase
                            pass
                        await asyncio.sleep(0.5)

                return "html", None, None
        except Exception:
            pass

        # 3. INTERACTION (Delayed Click)
        if elapsed > CF_CLICK_DELAY and not click_triggered:
            log(f"[WAIT] Auto-solve failed after {CF_CLICK_DELAY}s. Attempting force click...")
            await safe_verify_cf(page)
            click_triggered = True

        if int(elapsed) % 5 == 0 and int(elapsed) > 0:
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

    try:
        await page.get(url)
    except Exception:
        pass

    result_type, filename, filepath = await wait_for_completion(page)

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

    import urllib3
    urllib3.disable_warnings()

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