"""Shared Playwright browser/session plumbing for driving photos.google.com
as the logged-in human user (not subject to Google Photos API OAuth scopes).

Used by both `deletion.py` (trash staged items) and `locate_stage.py`
(locate + stage items into a review album) — both need the same browser
launch, session persistence, and login-handling behavior.
"""
import os
import time

_SECRETS_DIR = os.environ.get("SECRETS_DIR", "/secrets")
_SESSION_FILE = os.path.join(_SECRETS_DIR, "playwright_session.json")
_PHOTOS_URL = "https://photos.google.com"


def launch_browser(pw):
    return pw.chromium.launch(
        headless=False,
        args=["--no-sandbox", "--disable-dev-shm-usage"],
    )


def load_or_create_context(pw, browser):
    from .logger import log_info

    if os.path.exists(_SESSION_FILE):
        log_info("browser", "Loading saved browser session")
        return browser.new_context(storage_state=_SESSION_FILE)
    log_info("browser", "No saved session; starting fresh (you may need to log in)")
    return browser.new_context()


def save_context(context) -> None:
    from .logger import log_info

    os.makedirs(_SECRETS_DIR, exist_ok=True)
    context.storage_state(path=_SESSION_FILE)
    log_info("browser", "Browser session saved")


def ensure_logged_in(page, target_url: str) -> None:
    """Navigate to target_url; if Google redirects to a login page, pause and
    let the user complete login via the noVNC viewer, then retry."""
    page.goto(target_url, wait_until="networkidle", timeout=60_000)
    time.sleep(2)

    if "accounts.google.com" in page.url or "signin" in page.url.lower():
        print(
            "\n==> Google is asking you to log in."
            "\n==> Open http://localhost:6080/vnc.html in your browser to see the browser window."
            "\n==> Complete the login there, then press Enter here to continue..."
        )
        input()
        page.goto(target_url, wait_until="networkidle", timeout=60_000)


def scroll_to_load_all(page, max_scrolls: int = 20) -> None:
    prev_height = 0
    for _ in range(max_scrolls):
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(1)
        new_height = page.evaluate("document.body.scrollHeight")
        if new_height == prev_height:
            break
        prev_height = new_height
