# FlareSolverr-Lite

A lightweight FlareSolverr alternative based on Nodriver. Uses native Chrome to solve Cloudflare challenges and reliably download files from CloudFlare protected websites.

## Why?
Original FlareSolverr is great but often struggles with **Cloudflare v2 detection** due to Selenium/Undetected-Chromedriver TLS fingerprinting.
**FlareSolverr-Lite** attempts to solve this by using **Nodriver** (direct CDP protocol) and your system's native Chrome browser with a dedicated profile. It is strictly single-threaded and crash-proof by design.

## Features
- **Native Chrome:** Passes TLS fingerprint checks easily.
- **Download Interception:** Catch files hidden behind CF challenges using native CDP events.
- **Supervisor System:** Auto-restarts the browser if a request hangs or deadlocks.
- **Lightweight:** Single script, uses your standard Chrome browser.

## Installation
1. Install [Google Chrome](https://www.google.com/chrome/).
2. Install [Python 3.10+](https://www.python.org/).
3. Download or clone this repo.
4. Install dependencies: `pip install -r requirements.txt`

## Usage
Run the server: `python src/flaresolverr_lite.py`.
The server listens on `http://localhost:8191`.

## Notes
- Running the browser non-headless **HIGHLY** improves the chances of CloudFlare considering you as a normal user and not blocking on challenges.
- If you keep getting stuck on CloudFlare challenges, try logging in with a Chrome account directly in Chrome.
- You can configure the script to your liking directly in `src/flaresolverr_lite.py`. The CONFIGURATION section on top contains everything you should need.
- If the script fails to start Chrome, set `CHROME_PATH` to your Chrome binary. Nodriver only looks for it in your `PATH` on Linux, so a flatpak or snap install, or a launch from a service with a reduced `PATH`, will not be found automatically.
- The wait at the start of the script is not necessary, it is useful if you run the script at OS startup or session opening, when everything else is starting at the same time and starting Chrome is a bit of a heavy burden.
