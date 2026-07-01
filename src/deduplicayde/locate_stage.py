"""Locate flagged local files on photos.google.com and stage them into a
review album via Playwright.

Google's OAuth scope for this app can only see/touch items it uploaded itself
(see CLAUDE.md), so `albums.batchAddMediaItems` can never be given a valid
mediaItemId for a pre-existing item. This module replaces that API call with
browser automation: it drives photos.google.com as the logged-in human user
(not subject to OAuth scopes at all), locates each flagged item by date +
filename, and adds matches to the review album via the UI's own "Add to
album" action. Album *creation* still goes through the working API call
(`staging.get_or_create_albums`) — only adding pre-existing items to it moves
here.

IMPORTANT — unverified against the live site: the date-search interaction and
every CSS/ARIA selector below are best-effort guesses, exactly like the ones
already in deletion.py's trash flow. Validate incrementally:
  1. `stage --purpose=receipt --dry-run` — prints candidates, no browser needed.
  2. A single day watched live via http://localhost:6080/vnc.html against a
     small test album, before trusting a batch run.

Must run in the Playwright-capable `delete` service (Xvfb + noVNC).

Usage:
    docker compose run -p 6080:6080 delete stage --purpose=receipt --dry-run
    docker compose run -p 6080:6080 delete stage --purpose=receipt --no-dry-run
"""
import time
from collections import defaultdict
from io import BytesIO

import imagehash

from . import browser, db
from .logger import log_info, log_item, log_error

_PHOTOS_URL = "https://photos.google.com"

# Screenshot compression shifts phash more than a same-file comparison would —
# generous threshold, only used to break ties between same-filename same-day
# candidates. Needs live calibration once real screenshots can be compared.
_PHASH_TIEBREAK_THRESHOLD = 12


def run(purpose: str, dry_run: bool = True) -> None:
    from . import auth, staging

    db.init_db()

    with db.get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, filename, local_path, local_timestamp
            FROM media_items
            WHERE label=? AND staged_album_id IS NULL AND deletion_status IS NULL
            """,
            (purpose,),
        ).fetchall()

    items = [dict(r) for r in rows]

    if not items:
        print(f"No unstaged items with label='{purpose}'.")
        return

    if dry_run:
        print(f"\n[DRY-RUN] Would locate + stage {len(items)} items with label='{purpose}':")
        for it in items[:20]:
            print(f"  {it['filename']}  ({it['local_timestamp'] or 'no timestamp'})")
        if len(items) > 20:
            print(f"  ... and {len(items) - 20} more")
        print("\nRe-run with --no-dry-run to actually locate and stage via Playwright.")
        return

    creds = auth.get_credentials()
    album_ids = staging.get_or_create_albums(creds)
    if purpose not in album_ids:
        raise ValueError(f"No album configured for purpose={purpose!r}")
    album_id = album_ids[purpose]
    with db.get_conn() as conn:
        album_title = conn.execute(
            "SELECT title FROM albums WHERE album_id=?", (album_id,)
        ).fetchone()["title"]

    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        b = browser.launch_browser(pw)
        context = browser.load_or_create_context(pw, b)
        page = context.new_page()
        browser.ensure_logged_in(page, _PHOTOS_URL)
        try:
            staged, unmatched = stage_items(page, purpose, album_id, album_title, items)
        finally:
            browser.save_context(context)
            context.close()
            b.close()

    print(
        f"\nStaging complete: {staged} located and added to '{album_title}', "
        f"{unmatched} not found."
    )
    if unmatched:
        print("Unmatched items stay unstaged — re-run this command later to retry them.")


def stage_items(
    page,
    purpose: str,
    album_id: str,
    album_title: str,
    items: list[dict],
) -> tuple[int, int]:
    """Locate each item on photos.google.com by day + filename, select matches,
    and add them to album_title. Returns (staged_count, unmatched_count).

    Groups items by day to minimize page loads. On success, writes
    db.set_staged() per matched item immediately — the same provenance
    pattern deletion.py already uses for db.set_deleted() — so an interrupted
    run only needs to retry whatever's left unstaged.
    """
    by_day: dict[str, list[dict]] = defaultdict(list)
    for item in items:
        by_day[_day_key(item["local_timestamp"])].append(item)

    staged = 0
    unmatched = 0

    for day, day_items in by_day.items():
        log_info("locate_stage", "Searching day", day=day, item_count=len(day_items))
        try:
            matched = _locate_and_select_day(page, day, day_items)
        except Exception as e:
            log_error("locate_stage", "day_search_failed", day=day, error=str(e))
            unmatched += len(day_items)
            continue

        unmatched += len(day_items) - len(matched)
        if not matched:
            continue

        try:
            _add_selection_to_album(page, album_title)
        except Exception as e:
            log_error("locate_stage", "add_to_album_failed", day=day, error=str(e))
            unmatched += len(matched)
            continue

        with db.get_conn() as conn:
            for item in matched:
                db.set_staged(conn, item["id"], album_id)
                log_item(
                    "locate_stage", "staged",
                    item_id=item["id"], filename=item["filename"],
                    purpose=purpose, album_id=album_id,
                )
        staged += len(matched)

    return staged, unmatched


def _day_key(local_timestamp: str | None) -> str:
    if not local_timestamp:
        return "unknown"
    return local_timestamp[:10]  # ISO "YYYY-MM-DD" prefix


def _search_day(page, day: str) -> None:
    """Navigate to photos.google.com and search for the given date.

    UNVERIFIED: typing an ISO date into the search box is assumed to work the
    same way a human typing "2019-03-05" would, based on Google Photos'
    natural-language search. Confirm against the real account before trusting it.
    """
    page.goto(f"{_PHOTOS_URL}/search", wait_until="networkidle", timeout=60_000)
    search_box = page.locator("input[aria-label*='Search']").first
    search_box.click()
    search_box.fill(day)
    page.keyboard.press("Enter")
    page.wait_for_load_state("networkidle", timeout=30_000)
    time.sleep(1)
    browser.scroll_to_load_all(page)


def _locate_and_select_day(page, day: str, day_items: list[dict]) -> list[dict]:
    """Search the given day, open each visible item, and select the ones that
    match a local file in day_items (by filename, phash as tiebreak only if
    more than one flagged item shares a filename on this day). Returns the
    subset of day_items that were matched AND selected in the UI (ready for
    one 'Add to album' action)."""
    if day == "unknown":
        log_item("locate_stage", "skipped_no_timestamp", day=day, count=len(day_items))
        return []

    _search_day(page, day)

    thumbnails = page.locator("[data-media-key]").all()
    if not thumbnails:
        thumbnails = page.locator("img[data-p]").all()
    if not thumbnails:
        log_item("locate_stage", "no_results_for_day", day=day, expected=len(day_items))
        return []

    remaining: dict[str, list[dict]] = defaultdict(list)
    for item in day_items:
        remaining[item["filename"].lower()].append(item)

    matched: list[dict] = []

    for thumb in thumbnails:
        if not remaining:
            break

        filename = _read_filename(page, thumb)
        candidates = remaining.get((filename or "").lower())
        if not candidates:
            _close_lightbox(page)
            continue

        if len(candidates) == 1:
            choice = candidates[0]
        else:
            choice = _phash_tiebreak(page, candidates)
            if choice is None:
                log_item(
                    "locate_stage", "ambiguous_same_filename",
                    day=day, filename=filename, candidate_count=len(candidates),
                )
                _close_lightbox(page)
                continue

        _close_lightbox(page)
        _select_thumbnail(page, thumb)
        matched.append(choice)
        candidates.remove(choice)
        if not candidates:
            del remaining[choice["filename"].lower()]

    for leftover_list in remaining.values():
        for leftover in leftover_list:
            log_item(
                "locate_stage", "not_found_in_cloud",
                item_id=leftover["id"], filename=leftover["filename"], day=day,
            )

    return matched


def _read_filename(page, thumbnail) -> str | None:
    """Open a thumbnail's lightbox + info panel, read the filename, close it."""
    thumbnail.click()
    time.sleep(0.5)
    page.keyboard.press("i")
    time.sleep(0.5)

    for selector in ["[aria-label='Filename']", "[data-filename]", ".filename"]:
        try:
            el = page.locator(selector).first
            if el.is_visible(timeout=1000):
                return el.inner_text()
        except Exception:
            continue
    return None


def _close_lightbox(page) -> None:
    page.keyboard.press("Escape")
    time.sleep(0.3)


def _select_thumbnail(page, thumbnail) -> None:
    thumbnail.hover()
    time.sleep(0.2)
    checkbox = page.locator("[data-is-checked]").first
    if checkbox.is_visible(timeout=1000):
        checkbox.click()
    else:
        thumbnail.click(modifiers=["Shift"])


def _add_selection_to_album(page, album_title: str) -> None:
    add_button = None
    for selector in [
        "button[aria-label*='Add to album']",
        "[data-tooltip*='Add to album']",
    ]:
        try:
            btn = page.locator(selector).first
            if btn.is_visible(timeout=2000):
                add_button = btn
                break
        except Exception:
            continue
    if add_button is None:
        raise RuntimeError("Could not find 'Add to album' button")
    add_button.click()
    time.sleep(0.5)

    album_option = page.locator(f"text={album_title}").first
    if not album_option.is_visible(timeout=2000):
        raise RuntimeError(f"Could not find album option '{album_title}' in add-to-album picker")
    album_option.click()
    time.sleep(1)


def _phash_tiebreak(page, candidates: list[dict]) -> dict | None:
    """Compare the currently-open lightbox image's phash against each
    candidate's local file phash; return the closest one within threshold,
    or None if no candidate is close enough. Experimental — see module docstring."""
    screen_hash = _screenshot_phash(page)
    if not screen_hash:
        return None

    best = None
    best_dist = None
    for candidate in candidates:
        local_hash = _local_phash(candidate["local_path"])
        if not local_hash:
            continue
        dist = imagehash.hex_to_hash(local_hash) - imagehash.hex_to_hash(screen_hash)
        if best_dist is None or dist < best_dist:
            best, best_dist = candidate, dist

    if best is not None and best_dist is not None and best_dist <= _PHASH_TIEBREAK_THRESHOLD:
        return best
    return None


def _local_phash(local_path: str) -> str | None:
    try:
        from PIL import Image
        try:
            from pillow_heif import register_heif_opener
            register_heif_opener()
        except ImportError:
            pass
        return str(imagehash.phash(Image.open(local_path)))
    except Exception:
        return None


def _screenshot_phash(page) -> str | None:
    try:
        from PIL import Image
        img_el = page.locator("img[src*='googleusercontent']").first
        png_bytes = img_el.screenshot()
        return str(imagehash.phash(Image.open(BytesIO(png_bytes))))
    except Exception:
        return None
