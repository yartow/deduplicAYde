"""Locate flagged local files on photos.google.com and stage them into a
review album via Playwright.

Google's OAuth scope for this app can only see/touch items it uploaded itself
(see CLAUDE.md), so `albums.batchAddMediaItems` can never be given a valid
mediaItemId for a pre-existing item. This module replaces that API call with
browser automation: it drives photos.google.com as the logged-in human user
(not subject to OAuth scopes at all), locates each flagged item by date +
capture timestamp, and adds matches to the review album via the UI's own
"Create or add to album" action. Album *creation* still goes through the
working API call (`staging.get_or_create_albums`) — only adding pre-existing
items to it moves here.

Selectors below are confirmed against the live site (three read-only Playwright
probes against the real, authenticated account — screenshots + DOM attribute
dumps, no clicks on anything destructive) after the first real run left the
staging album empty. Key findings baked into this module:
  - The search box only exists on the photos.google.com home view (not at a
    direct /search URL) and is collapsed into a button that must be clicked to
    reveal the actual input.
  - Google Photos does not expose original filenames anywhere in the UI's
    accessible DOM. Each grid tile's aria-label is a synthesized description +
    precise (second-level) capture timestamp instead, e.g. "Photo - Landscape -
    Jul 1, 2026, 8:35:25 PM". Matching is therefore done on that timestamp
    against local_timestamp (already resolved via EXIF/sidecar in round0.py),
    not on filename.
  - Selection checkboxes are `[role="checkbox"]` and carry the same aria-label
    as their tile, so they're read and clicked directly — no separate
    thumbnail lookup or lightbox open/close needed for the common case.
The "Create or add to album" picker's internal item-selection interaction, and
deletion.py's select-all/confirm-dialog flow, were not reachable to verify
without actually completing an add/trash action — validate those live:
  1. `stage --purpose=receipt --dry-run` — prints candidates, no browser needed.
  2. A single day watched live via http://localhost:6080/vnc.html against a
     small test album, before trusting a batch run.

Must run in the Playwright-capable `delete` service (Xvfb + noVNC).

Usage:
    docker compose run -p 6080:6080 delete stage --purpose=receipt --dry-run
    docker compose run -p 6080:6080 delete stage --purpose=receipt --no-dry-run
"""
import re
import time
from collections import defaultdict
from datetime import datetime
from io import BytesIO

import imagehash

from . import browser, db
from .logger import log_info, log_item, log_error

_PHOTOS_URL = "https://photos.google.com"

# Screenshot compression shifts phash more than a same-file comparison would —
# generous threshold, only used to break ties between same-timestamp same-day
# candidates (rare: two flagged receipts captured the same second). Needs live
# calibration once real screenshots can be compared.
_PHASH_TIEBREAK_THRESHOLD = 12

_ARIA_TS_RE = re.compile(r"(\w{3} \d{1,2}, \d{4}, \d{1,2}:\d{2}:\d{2}\s?[AP]M)")

# Hard per-phase wall-clock budget passed to browser.hard_timeout() — see that
# function's docstring for why this exists (a run hung for 2+ hours despite
# every individual Playwright timeout already in this module).
_DAY_BUDGET_SECONDS = 120

# Cap on scroll steps while scanning a single day's search results — see
# _locate_and_select_day's docstring for why this scans incrementally rather
# than snapshotting the whole grid once. One viewport-height scroll per step,
# so this comfortably covers even a very prolific day.
_MAX_DAY_SCAN_SCROLLS = 40


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
        context = browser.launch_context(pw)
        page = context.new_page()
        browser.ensure_logged_in(page, _PHOTOS_URL)
        try:
            staged, unmatched = stage_items(page, purpose, album_id, album_title, items)
        finally:
            context.close()

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
    """Locate each item on photos.google.com by day + capture timestamp, select
    matches, and add them to album_title. Returns (staged_count, unmatched_count).

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
            with browser.hard_timeout(_DAY_BUDGET_SECONDS):
                matched = _locate_and_select_day(page, day, day_items)
        except Exception as e:
            log_error("locate_stage", "day_search_failed", day=day, error=str(e))
            unmatched += len(day_items)
            continue

        unmatched += len(day_items) - len(matched)
        if not matched:
            continue

        try:
            with browser.hard_timeout(_DAY_BUDGET_SECONDS):
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
    """Navigate to the photos.google.com home view and search for the given
    date. The real search input is present and directly clickable on load —
    confirmed live; an earlier version tried clicking a separate collapsed
    button first, which doesn't work (Playwright: "element is not visible").

    Uses "domcontentloaded" + an explicit wait for search results, not
    "networkidle" — confirmed live that photos.google.com keeps background
    connections open indefinitely, so "networkidle" can time out even on a
    normal load. Pressing Enter after a search is also a client-side route
    change, not a real navigation, so wait_for_load_state wouldn't reliably
    reflect when results have rendered anyway."""
    page.goto(_PHOTOS_URL, wait_until="domcontentloaded", timeout=60_000)
    search_box = page.locator("input[aria-label='Search your photos and albums']").first
    search_box.click(timeout=10_000)
    search_box.fill(day)
    page.keyboard.press("Enter")
    try:
        page.wait_for_selector("[role='checkbox']", timeout=15_000)
    except Exception:
        pass  # no results for this day, or slow to render — caller handles an empty list
    time.sleep(1)
    # Deliberately no browser.scroll_to_load_all() here — it scrolls to the
    # bottom to force lazy-loading, then back to the top. Confirmed live
    # (day_search_failed: "Locator.evaluate: Timeout 30000ms exceeded ...
    # waiting for locator("[role='checkbox']").nth(55)") that Google Photos
    # virtualizes this grid, unmounting items scrolled out of view — landing
    # back at the top leaves any deep-index item unmounted again, so a caller
    # iterating a frozen index list hangs waiting for it to reattach.
    # _locate_and_select_day scans+scrolls incrementally instead.


def _parse_aria_timestamp(aria_label: str | None) -> str | None:
    """Extract the capture timestamp embedded in a grid tile's aria-label
    (e.g. "Photo - Landscape - Jul 1, 2026, 8:35:25 PM" or "Burst photo -
    Landscape - Jul 1, 2026, 7:49:07 PM - 2 photos in sequence") and normalize
    it to the same "YYYY-MM-DDTHH:MM:SS" format round0.py stores as
    local_timestamp. Google Photos doesn't expose original filenames in the
    UI at all — confirmed live — so this timestamp is the only reliable
    match key available."""
    if not aria_label:
        return None
    m = _ARIA_TS_RE.search(aria_label)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%b %d, %Y, %I:%M:%S %p").strftime("%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None


def _locate_and_select_day(page, day: str, day_items: list[dict]) -> list[dict]:
    """Search the given day and select the checkboxes whose embedded
    timestamp matches a local file in day_items (phash as tiebreak only if
    more than one flagged item shares the exact same second on this day).
    Returns the subset of day_items that were matched AND selected in the UI
    (ready for one 'Create or add to album' action).

    Scans the grid incrementally (one viewport-height scroll at a time),
    re-querying `[role='checkbox']` fresh at each scroll position, instead of
    snapshotting `page.locator(...).all()` once up front and iterating it by
    index. Confirmed live that Google Photos virtualizes this grid — items
    scrolled out of view get unmounted — so a frozen list of nth-index
    locators goes stale the moment scrolling happens between the snapshot and
    a later index being used (a day with 56 total photos on it produced
    "Locator.evaluate: Timeout 30000ms exceeded ... waiting for
    locator(\"[role='checkbox']\").nth(55)", because by the time the loop
    reached index 55, that item had never been (re-)mounted)."""
    if day == "unknown":
        log_item("locate_stage", "skipped_no_timestamp", day=day, count=len(day_items))
        return []

    _search_day(page, day)

    if page.locator("[role='checkbox']").count() == 0:
        log_item("locate_stage", "no_results_for_day", day=day, expected=len(day_items))
        return []

    remaining: dict[str, list[dict]] = defaultdict(list)
    for item in day_items:
        remaining[item["local_timestamp"]].append(item)

    matched: list[dict] = []
    seen_labels: set[str] = set()
    prev_height = -1

    for _ in range(_MAX_DAY_SCAN_SCROLLS):
        if not remaining:
            break

        # Restart this inner scan from a fresh page.locator(...).all() after
        # every match instead of continuing to iterate the same snapshot —
        # confirmed live (2026-07-03) that reveal()'s scrollIntoView for one
        # match can jump the viewport far enough to unmount OTHER checkboxes
        # still pending later in the very same snapshot (a "Locator.
        # get_attribute: Timeout 30000ms exceeded ... waiting for
        # locator(\"[role='checkbox']\").nth(59)" on a day with lots of total
        # photos), not just between separate outer-loop scroll steps. Cheap
        # to re-query; items already in seen_labels are skipped instantly.
        matched_this_pass = True
        while matched_this_pass and remaining:
            matched_this_pass = False

            for box in page.locator("[role='checkbox']").all():
                if not remaining:
                    break

                label = box.get_attribute("aria-label")
                if not label or label in seen_labels:
                    continue
                seen_labels.add(label)

                ts = _parse_aria_timestamp(label)
                candidates = remaining.get(ts)
                if not candidates:
                    continue

                # Act on `box` itself, immediately, rather than re-locating —
                # tried get_by_role(name=label, exact=True) as a staleness
                # guard, but confirmed live it can itself fail to (re-)match:
                # this grid's aria-labels contain a narrow no-break space
                # (U+202F) before "AM"/"PM", and accessible-name matching
                # apparently doesn't treat that the same as the raw attribute
                # string get_attribute() returned, producing a locator that
                # matches nothing and hangs. Unnecessary anyway now that the
                # scan restarts after every reveal() (see above): `box` is
                # only ever used as the *first* match found in a snapshot
                # taken fresh this pass, before any scrollIntoView in this
                # pass has had a chance to invalidate it.
                #
                # Scroll into view + real hover, not Locator.scroll_into_view_if_needed()/
                # hover() — confirmed live those hang forever on these checkboxes,
                # which are CSS-hidden until hovered (see browser.reveal()'s docstring).
                browser.reveal(page, box)
                # Always restart the scan after a reveal(), even if the match
                # turns out ambiguous below and no click follows — the
                # scrollIntoView already happened either way, so the rest of
                # this snapshot can't be trusted regardless of outcome.
                matched_this_pass = True

                if len(candidates) == 1:
                    choice = candidates[0]
                else:
                    choice = _phash_tiebreak(box, candidates)
                    if choice is None:
                        log_item(
                            "locate_stage", "ambiguous_same_timestamp",
                            day=day, timestamp=ts, candidate_count=len(candidates),
                        )
                        break

                browser.click(box)
                matched.append(choice)
                candidates.remove(choice)
                if not candidates:
                    del remaining[ts]
                break

                matched_this_pass = True
                break

        if not remaining:
            break

        # Compare scrollHeight AFTER scrolling+settling, not before — confirmed
        # live that checking "already at bottom" against pre-scroll geometry
        # produced a near-total match failure (every day's "not_found_in_cloud"
        # for its whole batch): the page is often still lazily expanding when
        # this loop's first iteration starts, so a pre-scroll snapshot can look
        # like the bottom even though the real dated-photo grid hasn't rendered
        # below the "Most relevant to your search" carousel yet. This mirrors
        # scroll_to_load_all()'s already-proven convergence check.
        page.evaluate("window.scrollBy(0, window.innerHeight)")
        time.sleep(0.8)
        new_height = page.evaluate("document.body.scrollHeight")
        if new_height == prev_height:
            break
        prev_height = new_height

    for leftover_list in remaining.values():
        for leftover in leftover_list:
            log_item(
                "locate_stage", "not_found_in_cloud",
                item_id=leftover["id"], filename=leftover["filename"], day=day,
            )

    return matched


def _add_selection_to_album(page, album_title: str) -> None:
    """`Locator.is_visible()` is a one-shot state check, not a polling wait
    like click()/hover() — confirmed live that checking it immediately after
    a selection click raced the toolbar's render and produced false
    "Could not find" errors even with the correct selector. `.click(timeout=)`
    uses Playwright's real auto-waiting actionability check instead.

    "Create or add to album" doesn't open the album picker directly — confirmed
    live it opens a content-type menu first (Album / Documents / Screenshots &
    recordings / Animation / Collage / Highlight video), and only clicking
    "Album" there (a <li role="menuitem" aria-label="Album">) reaches the
    actual "Add to album" picker. Rows in that picker are
    <li role="option" aria-label="<title> · N items">, so match by substring —
    exact text match fails since the item count is appended to the label.

    On a search-results page, the direct "Create or add to album" button
    often isn't attached at all, so this polls Locator.count() (a non-waiting
    DOM check) briefly and falls back to the "More options" kebab menu, whose
    dropdown includes an "Add to album" entry, when it never shows up.

    Whichever button is used, it's opened via browser.reveal()/click(),
    not Locator.click() — confirmed live (2026-07-03) that the "More options"
    kebab is CSS-hidden until hovered, the exact same pattern as the grid
    checkboxes (see browser.reveal()'s docstring): Playwright's full call log
    showed the button resolving to a real, attached element but stuck on
    "element is not visible" through 23+ retries, because .click()'s own
    actionability wait can never trigger the hover that would reveal it. The
    direct button is presumed to share this pattern too, so it gets the same
    treatment rather than a plain .click()."""
    from playwright.sync_api import TimeoutError as PWTimeout

    add_button = page.locator("button[aria-label='Create or add to album']").first
    found_direct = False
    for _ in range(10):  # ~2s — toolbar can render a beat after the last selection click
        if add_button.count() > 0:
            found_direct = True
            break
        time.sleep(0.2)

    if found_direct:
        browser.reveal(page, add_button)
        browser.click(add_button)
    else:
        # .last, not .first — confirmed live (2026-07-03) that .first opens a
        # *different*, always-present "More options" button (page-level, not
        # selection-scoped): its menu contained only "Select photos", a
        # generic action unrelated to the current selection. A second button
        # with the same aria-label appears once items are selected, later in
        # DOM order (React typically appends dynamically-shown toolbar
        # controls after the persistent header), so .last targets that one.
        more_options = page.locator("button[aria-label='More options']").last
        browser.reveal(page, more_options)
        browser.click(more_options)
        time.sleep(0.5)
        add_to_album_item = page.locator("[role='menuitem']").filter(
            has_text=re.compile("add to album", re.I)
        ).first
        try:
            add_to_album_item.click(timeout=5_000)
        except PWTimeout:
            # Surface what's actually in the open menu instead of just
            # "not found" — cheaper than guessing at the real wording blind
            # when a screenshot can't reliably catch the menu mid-open.
            menu_text = "<no [role=menu] found>"
            try:
                menu_text = page.locator("[role='menu']").first.inner_text(timeout=1_000)
            except Exception:
                pass
            raise RuntimeError(
                f"No 'add to album' menu item found; open menu text: {menu_text!r}"
            )
    time.sleep(0.5)

    album_menu_item = page.locator("[role='menuitem'][aria-label='Album']").first
    album_menu_item.click(timeout=5_000)
    time.sleep(0.5)

    album_option = page.locator(f"[role='option'][aria-label*='{album_title}']").first
    album_option.click(timeout=5_000)
    time.sleep(1)


def _phash_tiebreak(box, candidates: list[dict]) -> dict | None:
    """Compare the checkbox tile's own screenshot phash against each
    candidate's local file phash; return the closest one within threshold,
    or None if no candidate is close enough. Experimental — see module docstring."""
    screen_hash = _screenshot_phash(box)
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


def _screenshot_phash(box) -> str | None:
    """box is the checkbox's Locator — its bounding box covers the tile's
    thumbnail image (confirmed live), so no separate lightbox open is needed."""
    try:
        from PIL import Image
        png_bytes = box.screenshot()
        return str(imagehash.phash(Image.open(BytesIO(png_bytes))))
    except Exception:
        return None
