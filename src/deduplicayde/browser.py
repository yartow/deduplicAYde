"""Shared Playwright browser/session plumbing for driving photos.google.com
as the logged-in human user (not subject to Google Photos API OAuth scopes).

Used by both `deletion.py` (trash staged items) and `locate_stage.py`
(locate + stage items into a review album) — both need the same browser
launch and login-handling behavior.

Confirmed live on the first real `stage --no-dry-run` run: Google's login flow
blocks sign-ins performed inside a CDP-attached (i.e. Playwright-controlled)
browser with "this browser or app may not be secure" — independent of DOM
selectors, since it fires during the sign-in handshake itself. The fix is to
never perform that handshake under Playwright's control at all: `manual_login()`
launches the bundled Chromium binary directly via subprocess (no Playwright
driver attached, so no CDP fingerprint) against a persistent profile directory,
the user logs in there via noVNC and just closes the window, and every later
`launch_context()` call reuses that already-authenticated profile through
Playwright's `launch_persistent_context`. Real Chrome (channel="chrome") isn't
an option here — it has no Linux arm64 build, and this runs on Apple Silicon —
so both the manual login and the automated runs deliberately use the exact
same bundled Chromium binary, avoiding any cross-browser profile-format risk.
A persistent context writes cookies/storage back to that same directory
continuously, so there's no separate session-file export/import step — any
login completed later via the noVNC fallback in ensure_logged_in() also
persists automatically for next time.
"""
import os
import subprocess
import time

_SECRETS_DIR = os.environ.get("SECRETS_DIR", "/secrets")
_PROFILE_DIR = os.path.join(_SECRETS_DIR, "chrome-profile")
_PHOTOS_URL = "https://photos.google.com"


def manual_login() -> None:
    """One-time setup: launch the bundled Chromium binary directly (bypassing
    Playwright/CDP entirely) so the user can sign into Google without
    triggering its automation-controlled-browser block. Blocks until the user
    closes the browser window; the profile is saved to _PROFILE_DIR as a side
    effect of Chromium's normal on-disk profile writes — no explicit save step."""
    from playwright.sync_api import sync_playwright

    os.makedirs(_PROFILE_DIR, exist_ok=True)
    with sync_playwright() as pw:
        executable = pw.chromium.executable_path

    print(
        "\n==> Launching a plain, non-automated browser window for you to log into Google."
        "\n==> Open http://localhost:6080/vnc.html to see it."
        "\n==> Log into your Google account there, then just close the browser window when done."
    )
    subprocess.run(
        [
            executable,
            f"--user-data-dir={_PROFILE_DIR}",
            "--no-first-run",
            "--no-sandbox",
            _PHOTOS_URL,
        ],
        check=False,
    )
    print(f"\nProfile saved to {_PROFILE_DIR}. You can now run `stage`/`delete` normally.")


def launch_context(pw):
    from .logger import log_info

    if not os.path.isdir(_PROFILE_DIR):
        raise FileNotFoundError(
            f"No Chrome profile found at {_PROFILE_DIR}.\n"
            "Before the first Playwright run, log in once via:\n"
            "  docker compose run -p 6080:6080 delete login\n"
            "and complete the Google sign-in there."
        )
    log_info("browser", "Launching persistent Chromium profile", profile_dir=_PROFILE_DIR)
    return pw.chromium.launch_persistent_context(
        _PROFILE_DIR,
        headless=False,
        args=["--no-sandbox", "--disable-dev-shm-usage"],
    )


def ensure_logged_in(page, target_url: str) -> None:
    """Navigate to target_url; if Google redirects to a login page, pause and
    let the user complete login via the noVNC viewer, then retry. With a
    persistent, pre-authenticated profile this should be rare — session expiry
    or an occasional Google reverification prompt, not the normal first-run path."""
    page.goto(target_url, wait_until="networkidle", timeout=60_000)
    time.sleep(2)

    if "accounts.google.com" in page.url or "signin" in page.url.lower():
        print(
            "\n==> Google needs you to re-verify this session."
            "\n==> Open http://localhost:6080/vnc.html to see the browser window."
            "\n==> Complete it there, then press Enter here to continue"
            "\n==> (this browser profile persists automatically, so this shouldn't happen often)..."
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
