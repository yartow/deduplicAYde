"""Playwright browser automation to trash items in Google Photos.

IMPORTANT: This must run in the 'delete' Docker service which has Xvfb + noVNC.
Watch the browser at http://localhost:6080/vnc.html while this runs.

Usage:
    docker compose run -p 6080:6080 delete delete --album=receipts --dry-run
    docker compose run -p 6080:6080 delete delete --album=receipts --confirm
"""
import time

from . import browser, db
from .logger import log_info, log_item, log_error

_PHOTOS_URL = "https://photos.google.com"

_ALBUM_PURPOSE_MAP = {
    "receipts": "receipt",
    "vague": "vague",
    "short-videos": "short_video",
}

_BATCH_SIZE = 100  # items to trash per browser session before pausing

# Hard wall-clock budget per batch, via browser.hard_timeout() — see that
# function's docstring. Larger than locate_stage.py's per-day budget since a
# batch can involve selecting/trashing up to _BATCH_SIZE items in one go.
_BATCH_BUDGET_SECONDS = 300


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

    if purpose == "short_video":
        print(
            "\nIMPORTANT: Have you tested the Playwright deletion flow on a small test album first?\n"
            "Per the runbook, the first Playwright run must be against a small album before\n"
            "pointing it at a larger set. Proceed? [y/N] ",
            end="",
        )
        resp = input().strip().lower()
        if resp != "y":
            print("Aborting.")
            return

    _run_playwright(purpose)


def _dry_run_report(purpose: str) -> None:
    with db.get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, filename, staged_album_id
            FROM media_items
            WHERE label=? AND staged_album_id IS NOT NULL AND deletion_status IS NULL
            """,
            (purpose,),
        ).fetchall()

    print(f"\n[DRY-RUN] Would trash {len(rows)} items with label='{purpose}':")
    for r in rows[:20]:
        print(f"  {r['filename']}  (id={r['id']})")
    if len(rows) > 20:
        print(f"  ... and {len(rows) - 20} more")
    print(
        f"\nRe-run with --confirm (and without --dry-run) to actually delete."
    )


def _run_playwright(purpose: str) -> None:
    from playwright.sync_api import sync_playwright

    with db.get_conn() as conn:
        rows = conn.execute(
            """
            SELECT mi.id, mi.filename, a.album_id
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
        context = browser.launch_context(pw)
        page = context.new_page()

        try:
            for album_id, items in album_groups.items():
                log_info("deletion", "Processing album", album_id=album_id, item_count=len(items))
                deleted, failed = _delete_album_items(page, album_id, items)
                total_deleted += deleted
                total_failed += failed
        finally:
            context.close()

    print(
        f"\nDeletion complete: {total_deleted} trashed, {total_failed} failed."
        "\n(Items are in Trash. Google deletes them permanently after 60 days.)"
        "\nDO NOT empty Trash manually — the 60-day window is intentional."
    )


def _delete_album_items(page, album_id: str, items: list[dict]) -> tuple[int, int]:
    album_url = f"{_PHOTOS_URL}/album/{album_id}"
    log_info("deletion", "Navigating to album", url=album_url)

    browser.ensure_logged_in(page, album_url)

    deleted = 0
    failed = 0

    # Process in batches to avoid selecting too many at once
    for batch_start in range(0, len(items), _BATCH_SIZE):
        batch = items[batch_start : batch_start + _BATCH_SIZE]

        try:
            with browser.hard_timeout(_BATCH_BUDGET_SECONDS):
                count = _trash_visible_items(page, album_url, len(batch))
            deleted += count
            # Mark as deleted in DB
            with db.get_conn() as conn:
                for item in batch[:count]:
                    db.set_deleted(conn, item["id"])
                    log_item(
                        "deletion", "deleted", item_id=item["id"],
                        filename=item["filename"],
                    )
        except Exception as e:
            log_error("deletion", "Batch failed", error=str(e), batch_start=batch_start)
            for item in batch:
                with db.get_conn() as conn:
                    db.set_deleted(conn, item["id"], status="failed")
                log_item("deletion", "failed", item_id=item["id"], error=str(e))
            failed += len(batch)

        # Reload between batches. "domcontentloaded" not "networkidle" —
        # photos.google.com keeps background connections open indefinitely
        # (confirmed live), so networkidle can time out even on success.
        if batch_start + _BATCH_SIZE < len(items):
            page.reload(wait_until="domcontentloaded")
            time.sleep(2)

    return deleted, failed


def _trash_visible_items(page, album_url: str, expected_count: int) -> int:
    """Select all visible items in the current album view and move to trash."""
    from playwright.sync_api import TimeoutError as PWTimeout

    # Scroll to load all items
    browser.scroll_to_load_all(page)

    # Selection checkboxes are [role="checkbox"] divs, confirmed live against
    # the real account (see locate_stage.py's module docstring) — [data-p]/
    # [data-media-key]/[data-is-checked] don't exist in the real DOM at all.
    checkboxes = page.locator("[role='checkbox']").all()
    if not checkboxes:
        raise RuntimeError("Could not find any photos in the album")

    log_info("deletion", "Photos found in view", count=len(checkboxes))

    # Not Locator.hover()/click() — confirmed live (see browser.reveal()'s
    # docstring) that these checkboxes are CSS-hidden until hovered, and
    # hover()/click() both hang forever waiting for a "visible" precondition
    # the element can only satisfy after the hover they're blocking.
    browser.reveal(page, checkboxes[0])
    browser.click(checkboxes[0])

    # Select all: Shift+A or look for "Select all" button
    time.sleep(0.5)
    page.keyboard.press("a")  # 'a' selects all in Google Photos
    time.sleep(1)

    # Find and click trash/delete button. `Locator.is_visible()` is a one-shot
    # state check, not a polling wait like click()/hover() — confirmed live in
    # locate_stage.py that checking it immediately after a UI-changing action
    # races the render and produces false "not found" errors even with a
    # correct selector. `wait_for(state="visible")` polls properly.
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
            btn.wait_for(state="visible", timeout=1000)
            trash_button = btn
            break
        except PWTimeout:
            continue

    if trash_button is None:
        # Try the three-dot menu
        more_menu = page.locator("button[aria-label='More options']").first
        try:
            more_menu.wait_for(state="visible", timeout=2000)
            more_menu.click()
            time.sleep(0.5)
            trash_button = page.locator("text=Move to trash").first
        except PWTimeout:
            pass

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
            confirm_btn.wait_for(state="visible", timeout=2000)
            confirm_btn.click()
            time.sleep(2)
            break
        except Exception:
            continue

    log_item("deletion", "batch_trashed", count=expected_count, album_url=album_url)
    return expected_count
