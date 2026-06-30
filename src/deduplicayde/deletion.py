"""Playwright browser automation to trash items in Google Photos.

IMPORTANT: This must run in the 'delete' Docker service which has Xvfb + noVNC.
Watch the browser at http://localhost:6080/vnc.html while this runs.

Usage:
    docker compose run -p 6080:6080 delete delete --album=receipts --dry-run
    docker compose run -p 6080:6080 delete delete --album=receipts --confirm
"""
import json
import os
import time
from pathlib import Path

from . import auth, db, photos_api
from .logger import log_info, log_item, log_error

_SECRETS_DIR = os.environ.get("SECRETS_DIR", "/secrets")
_SESSION_FILE = os.path.join(_SECRETS_DIR, "playwright_session.json")
_PHOTOS_URL = "https://photos.google.com"

_ALBUM_PURPOSE_MAP = {
    "receipts": "receipt",
    "vague": "vague",
}

_BATCH_SIZE = 100  # items to trash per browser session before pausing


def run(album: str, confirm: bool = False, dry_run: bool = True) -> None:
    if album not in _ALBUM_PURPOSE_MAP:
        raise ValueError(f"--album must be one of: {list(_ALBUM_PURPOSE_MAP)}")

    purpose = _ALBUM_PURPOSE_MAP[album]
    db.init_db()

    if dry_run:
        _dry_run_report(purpose)
        return

    if not confirm:
        print(
            f"\nThis will permanently move items from the '{album}' album to Google Photos Trash.\n"
            "The 60-day recovery window applies — items won't be gone forever immediately.\n"
            f"Re-run with --confirm to proceed (and --album={album})."
        )
        return

    if purpose == "vague":
        print(
            "\nWARNING: Vague items require manual visual review in the Google Photos album\n"
            "before deletion. Have you reviewed the 'deduplicAYde – Vague' album? [y/N] ",
            end="",
        )
        resp = input().strip().lower()
        if resp != "y":
            print("Aborting. Review the album first.")
            return

    _run_playwright(purpose)


def _dry_run_report(purpose: str) -> None:
    with db.get_conn() as conn:
        rows = conn.execute(
            """
            SELECT media_item_id, filename, staged_album_id
            FROM media_items
            WHERE label=? AND staged_album_id IS NOT NULL AND deletion_status IS NULL
            """,
            (purpose,),
        ).fetchall()

    print(f"\n[DRY-RUN] Would trash {len(rows)} items with label='{purpose}':")
    for r in rows[:20]:
        print(f"  {r['filename']}  ({r['media_item_id']})")
    if len(rows) > 20:
        print(f"  ... and {len(rows) - 20} more")
    print(
        f"\nRe-run with --confirm (and without --dry-run) to actually delete."
    )


def _run_playwright(purpose: str) -> None:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    with db.get_conn() as conn:
        rows = conn.execute(
            """
            SELECT mi.media_item_id, mi.filename, a.album_id
            FROM media_items mi
            JOIN albums a ON a.album_id = mi.staged_album_id
            WHERE mi.label=? AND mi.staged_album_id IS NOT NULL AND mi.deletion_status IS NULL
            """,
            (purpose,),
        ).fetchall()

    if not rows:
        print(f"No staged items with label='{purpose}' pending deletion.")
        return

    # Group by album_id (should be just one, but handle multiple gracefully)
    album_groups: dict[str, list[dict]] = {}
    for r in rows:
        album_groups.setdefault(r["album_id"], []).append(dict(r))

    total_deleted = 0
    total_failed = 0

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=False,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = _load_or_create_context(pw, browser)
        page = context.new_page()

        try:
            for album_id, items in album_groups.items():
                log_info("deletion", "Processing album", album_id=album_id, item_count=len(items))
                deleted, failed = _delete_album_items(page, album_id, items)
                total_deleted += deleted
                total_failed += failed
        finally:
            _save_context(context)
            context.close()
            browser.close()

    print(
        f"\nDeletion complete: {total_deleted} trashed, {total_failed} failed."
        "\n(Items are in Trash. Google deletes them permanently after 60 days.)"
        "\nDO NOT empty Trash manually — the 60-day window is intentional."
    )


def _load_or_create_context(pw, browser):
    if os.path.exists(_SESSION_FILE):
        log_info("deletion", "Loading saved browser session")
        return browser.new_context(storage_state=_SESSION_FILE)
    log_info("deletion", "No saved session; starting fresh (you may need to log in)")
    return browser.new_context()


def _save_context(context) -> None:
    os.makedirs(_SECRETS_DIR, exist_ok=True)
    context.storage_state(path=_SESSION_FILE)
    log_info("deletion", "Browser session saved")


def _delete_album_items(page, album_id: str, items: list[dict]) -> tuple[int, int]:
    from playwright.sync_api import TimeoutError as PWTimeout

    album_url = f"{_PHOTOS_URL}/album/{album_id}"
    log_info("deletion", "Navigating to album", url=album_url)

    page.goto(album_url, wait_until="networkidle", timeout=60_000)
    time.sleep(2)

    # Check if we need to log in
    if "accounts.google.com" in page.url or "signin" in page.url.lower():
        print(
            "\n==> Google is asking you to log in."
            "\n==> Open http://localhost:6080/vnc.html in your browser to see the browser window."
            "\n==> Complete the login there, then press Enter here to continue..."
        )
        input()
        page.goto(album_url, wait_until="networkidle", timeout=60_000)

    deleted = 0
    failed = 0

    # Process in batches to avoid selecting too many at once
    for batch_start in range(0, len(items), _BATCH_SIZE):
        batch = items[batch_start : batch_start + _BATCH_SIZE]

        try:
            count = _trash_visible_items(page, album_url, len(batch))
            deleted += count
            # Mark as deleted in DB
            with db.get_conn() as conn:
                for item in batch[:count]:
                    db.set_deleted(conn, item["media_item_id"])
                    log_item(
                        "deletion", "deleted", media_item_id=item["media_item_id"],
                        filename=item["filename"],
                    )
        except Exception as e:
            log_error("deletion", "Batch failed", error=str(e), batch_start=batch_start)
            for item in batch:
                with db.get_conn() as conn:
                    db.set_deleted(conn, item["media_item_id"], status="failed")
                log_item("deletion", "failed", media_item_id=item["media_item_id"], error=str(e))
            failed += len(batch)

        # Reload between batches
        if batch_start + _BATCH_SIZE < len(items):
            page.reload(wait_until="networkidle")
            time.sleep(2)

    return deleted, failed


def _trash_visible_items(page, album_url: str, expected_count: int) -> int:
    """Select all visible items in the current album view and move to trash."""
    from playwright.sync_api import TimeoutError as PWTimeout

    # Scroll to load all items
    _scroll_to_load_all(page)

    # Click the first photo to enter selection mode
    photos = page.locator("img[data-p]").all()
    if not photos:
        # Try alternative selectors
        photos = page.locator("[data-media-key]").all()

    if not photos:
        raise RuntimeError("Could not find any photos in the album")

    log_info("deletion", "Photos found in view", count=len(photos))

    # Enter selection mode by clicking first photo's checkbox area
    # Google Photos shows checkboxes on hover
    first_photo = photos[0]
    first_photo.hover()
    time.sleep(0.3)

    # Look for a checkbox
    checkbox = page.locator("[data-is-checked]").first
    if checkbox.is_visible(timeout=2000):
        checkbox.click()
    else:
        # Keyboard shortcut: hover + click usually works
        first_photo.click(modifiers=["Shift"])

    # Select all: Shift+A or look for "Select all" button
    time.sleep(0.5)
    page.keyboard.press("a")  # 'a' selects all in Google Photos
    time.sleep(1)

    # Find and click trash/delete button
    trash_button = None
    for selector in [
        "button[aria-label*='Delete']",
        "button[aria-label*='Trash']",
        "button[aria-label*='Move to trash']",
        "[data-tooltip*='Delete']",
        "[data-tooltip*='Trash']",
    ]:
        try:
            btn = page.locator(selector).first
            if btn.is_visible(timeout=1000):
                trash_button = btn
                break
        except PWTimeout:
            continue

    if trash_button is None:
        # Try the three-dot menu
        more_menu = page.locator("button[aria-label='More options']").first
        if more_menu.is_visible(timeout=2000):
            more_menu.click()
            time.sleep(0.5)
            trash_button = page.locator("text=Move to trash").first

    if trash_button is None:
        raise RuntimeError("Could not find trash/delete button")

    trash_button.click()
    time.sleep(1)

    # Confirm in the dialog if one appears
    for confirm_selector in [
        "button:has-text('Move to trash')",
        "button:has-text('Delete')",
        "button:has-text('Confirm')",
        "[aria-label='Move to trash']",
    ]:
        try:
            confirm_btn = page.locator(confirm_selector).last
            if confirm_btn.is_visible(timeout=2000):
                confirm_btn.click()
                time.sleep(2)
                break
        except Exception:
            continue

    log_item("deletion", "batch_trashed", count=expected_count, album_url=album_url)
    return expected_count


def _scroll_to_load_all(page, max_scrolls: int = 20) -> None:
    prev_height = 0
    for _ in range(max_scrolls):
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(1)
        new_height = page.evaluate("document.body.scrollHeight")
        if new_height == prev_height:
            break
        prev_height = new_height
