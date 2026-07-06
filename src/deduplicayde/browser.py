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
import contextlib
import json
import os
import signal
import subprocess
import time

_SECRETS_DIR = os.environ.get("SECRETS_DIR", "/secrets")
_PROFILE_DIR = os.path.join(_SECRETS_DIR, "chrome-profile")
_PHOTOS_URL = "https://photos.google.com"

_SINGLETON_FILES = ("SingletonLock", "SingletonCookie", "SingletonSocket")


def _clear_stale_profile_lock() -> None:
    """Remove Chrome's SingletonLock/Cookie/Socket files before launching.

    Every `docker compose run` starts a brand-new container with its own
    process tree, so any pre-existing lock file in the (bind-mounted,
    host-persisted) profile directory can only be left over from a previous,
    now-dead container — it is never valid to keep it. Without this, a run
    that didn't exit cleanly (Ctrl+C, `docker stop`, a crash) leaves the next
    run permanently unable to launch Chromium at all ("profile appears to be
    in use by another Chromium process on another computer"), confirmed live
    repeatedly during development."""
    for name in _SINGLETON_FILES:
        path = os.path.join(_PROFILE_DIR, name)
        if os.path.exists(path) or os.path.islink(path):
            os.remove(path)


def _mark_profile_exited_cleanly() -> None:
    """Rewrite the profile's Preferences file to say the previous session
    exited normally, so Chrome doesn't show its native "Restore pages?"
    infobar on this launch.

    Confirmed live (X11 framebuffer screenshot mid-run) that
    `--disable-session-crashed-bubble` alone does NOT suppress this bubble in
    the Chromium build Playwright bundles — the bubble kept appearing even
    with that flag passed at launch. Inspecting the profile directly showed
    why: `Default/Preferences`'s `profile.exit_type` was "Crashed", left over
    from earlier runs in this session that were killed via `docker stop` or
    `hard_timeout` instead of a clean `context.close()`. The infobar renders
    in the top-right corner of the viewport, exactly where Google Photos puts
    its selection toolbar (add-to-album/share/delete icons), blanking it out
    — that's why `_add_selection_to_album`'s "Create or add to album" click
    was timing out on effectively every day. Patching `exit_type` back to
    "Normal" before each launch removes the bubble at the source, regardless
    of how the previous run actually ended."""
    prefs_path = os.path.join(_PROFILE_DIR, "Default", "Preferences")
    if not os.path.exists(prefs_path):
        return
    try:
        with open(prefs_path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return
    profile = data.setdefault("profile", {})
    profile["exit_type"] = "Normal"
    profile["exited_cleanly"] = True
    with open(prefs_path, "w") as f:
        json.dump(data, f)


def manual_login() -> None:
    """One-time setup: launch the bundled Chromium binary directly (bypassing
    Playwright/CDP entirely) so the user can sign into Google without
    triggering its automation-controlled-browser block. Blocks until the user
    closes the browser window; the profile is saved to _PROFILE_DIR as a side
    effect of Chromium's normal on-disk profile writes — no explicit save step."""
    from playwright.sync_api import sync_playwright

    os.makedirs(_PROFILE_DIR, exist_ok=True)
    _clear_stale_profile_lock()
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
            "--disable-session-crashed-bubble",
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
    _clear_stale_profile_lock()
    _mark_profile_exited_cleanly()
    log_info("browser", "Launching persistent Chromium profile", profile_dir=_PROFILE_DIR)
    return pw.chromium.launch_persistent_context(
        _PROFILE_DIR,
        headless=False,
        # --disable-session-crashed-bubble: confirmed live (X11 framebuffer
        # screenshot of a stuck `stage` run) that after a prior run was killed
        # via docker stop/hard_timeout instead of a clean context.close(),
        # Chrome shows a "Restore pages? Chromium didn't shut down correctly"
        # bubble in the top-right corner of the viewport on every subsequent
        # launch — exactly where Google Photos renders its selection toolbar
        # (add-to-album/share/delete icons), blanking it out and causing
        # add_to_album_failed's "Create or add to album" click to time out.
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-session-crashed-bubble",
        ],
    )


def ensure_logged_in(page, target_url: str) -> None:
    """Navigate to target_url; if Google redirects to a login page, pause and
    let the user complete login via the noVNC viewer, then retry. With a
    persistent, pre-authenticated profile this should be rare — session expiry
    or an occasional Google reverification prompt, not the normal first-run path.

    Uses wait_until="domcontentloaded", not "networkidle" — confirmed live that
    photos.google.com keeps background connections (websockets/polling) open
    indefinitely, so "networkidle" can time out even on a normal, successful
    load. domcontentloaded is all that's needed here since this function only
    inspects page.url, not page content."""
    page.goto(target_url, wait_until="domcontentloaded", timeout=60_000)
    time.sleep(2)

    if "accounts.google.com" in page.url or "signin" in page.url.lower():
        print(
            "\n==> Google needs you to re-verify this session."
            "\n==> Open http://localhost:6080/vnc.html to see the browser window."
            "\n==> Complete it there, then press Enter here to continue"
            "\n==> (this browser profile persists automatically, so this shouldn't happen often)..."
        )
        input()
        page.goto(target_url, wait_until="domcontentloaded", timeout=60_000)


@contextlib.contextmanager
def hard_timeout(seconds: int):
    """Hard wall-clock watchdog via SIGALRM, for wrapping a single Playwright
    operation that must never be allowed to block forever.

    Confirmed live: a `stage` run hung for 2+ hours mid-batch (a Chrome
    renderer process pegged at high CPU while the Python process sat almost
    entirely idle) in a way that didn't respect any of the individual
    Playwright `timeout=` values already set elsewhere in this codebase — some
    failure mode inside the browser itself, not a missing timeout parameter.
    This is a last-resort safety net so one bad item/batch can never freeze an
    entire run; raises builtin TimeoutError, which the existing per-item
    except-and-continue handlers in locate_stage.py/deletion.py already catch."""
    def _handler(signum, frame):
        raise TimeoutError(f"Exceeded {seconds}s hard time budget")

    previous = signal.signal(signal.SIGALRM, _handler)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def reveal(page, locator) -> None:
    """Scroll a grid checkbox into view and hover it with a real mouse move.

    Confirmed live via an X11 framebuffer screenshot of a stuck `stage` run
    (grabbed with `ffmpeg -f x11grab` against the container's Xvfb display,
    since the run's own Playwright session held the only browser instance):
    a fully-loaded, fully-scrolled search-results grid showed *zero* visible
    checkboxes anywhere on the page — they're CSS-hidden (opacity 0) by
    default and only revealed on hover of their tile. That explains why
    `Locator.scroll_into_view_if_needed()` / `.hover()` / `.click()` /
    `.screenshot()` all hung for their full timeout with "element is not
    visible": every one of those methods requires the target to already be
    Playwright-"visible" as a precondition, which a hover-only-revealed
    checkbox can never satisfy without first receiving the very hover that
    only a successful precondition-passing call could deliver.

    Scrolls via raw JS `scrollIntoView` (pure layout geometry, unaffected by
    opacity/visibility) followed by a real `page.mouse.move()` to the
    element's center — a genuine mouse-move event, which triggers native
    `:hover` CSS regardless of the target's current opacity. Still useful
    ahead of click() (below) purely for the CSS reveal (e.g. so a follow-up
    `.screenshot()` for the phash tiebreak shows the actual tile), even
    though click() no longer uses the coordinates this computes."""
    locator.evaluate("el => el.scrollIntoView({block: 'center', behavior: 'instant'})")
    time.sleep(0.2)
    rect = locator.evaluate(
        "el => { const r = el.getBoundingClientRect(); "
        "return {x: r.x, y: r.y, width: r.width, height: r.height}; }"
    )
    cx = rect["x"] + rect["width"] / 2
    cy = rect["y"] + rect["height"] / 2
    page.mouse.move(cx, cy)
    time.sleep(0.3)


def click(locator) -> None:
    """Dispatch a real DOM click directly via the element's own .click()
    method, instead of page.mouse.click() at raw (x, y) coordinates.

    Confirmed live (2026-07-03, user watching over noVNC in real time):
    page.mouse.click(cx, cy) was producing no visible effect whatsoever on
    the target checkbox — no checkmark, no highlight, nothing — and no OS-level
    mouse cursor was ever visibly moving on screen during automation, in a
    live continuous observation (not just a single frame grab). CDP's
    synthetic mouse-input events apparently aren't reliably registering with
    the page under this Xvfb setup, despite page.mouse.move() successfully
    triggering :hover CSS (see reveal() above) — hover and click evidently go
    through different enough paths that one working doesn't imply the other
    does. locator.evaluate("el => el.click()") bypasses coordinate-based
    input simulation entirely and calls the DOM element's real click()
    method directly — React's event delegation picks this up exactly like a
    genuine click — and it works regardless of the element's current CSS
    visibility, same as before, but without depending on exact pixel
    coordinates or on synthetic mouse input actually being delivered."""
    locator.evaluate("el => el.click()")


def scroll_to_load_all(page, max_scrolls: int = 20) -> None:
    """Scroll to the bottom repeatedly to force lazy-loaded content to render,
    then scroll back to the top. Confirmed live (locate_stage day_search_failed
    hover timeouts on higher-indexed checkboxes) that leaving the page scrolled
    to the bottom puts earlier-indexed grid items outside the viewport, and
    Playwright's actionability checks require an element to actually intersect
    the viewport before hover/click will succeed."""
    prev_height = 0
    for _ in range(max_scrolls):
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(1)
        new_height = page.evaluate("document.body.scrollHeight")
        if new_height == prev_height:
            break
        prev_height = new_height
    page.evaluate("window.scrollTo(0, 0)")
    time.sleep(0.5)
