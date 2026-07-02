"""Video-specific cleanup operations.

detect-short-videos  Find local videos ≤ N seconds, stage for cloud deletion, delete locally.
purge-local-videos   Delete all remaining video files locally (cloud copies untouched).

Run detect-short-videos BEFORE purge-local-videos so short clips are staged
in Google Photos before the bulk local purge removes them from disk.

Cloud deletion of short videos happens later via:
    docker compose run -p 6080:6080 delete delete --album=short-videos --no-dry-run --confirm
"""
import os
import subprocess
from pathlib import Path

from . import auth, browser, db, staging
from .logger import log_info, log_item, log_error

_LIBRARY_DIR = Path(os.environ.get("DATA_DIR", "/data")) / "library"

_VIDEO_EXTS = {
    ".mp4", ".mov", ".avi", ".mkv", ".m4v", ".3gp", ".wmv",
    ".flv", ".webm", ".ts", ".mts", ".m2ts", ".mpg", ".mpeg",
}

_SHORT_VIDEO_ALBUM_TITLE = "deduplicAYde – Short Videos"


def _is_video(path: Path) -> bool:
    return path.suffix.lower() in _VIDEO_EXTS


def _ffprobe_duration(path: Path) -> float | None:
    """Return video duration in seconds via ffprobe, or None on any failure."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True, text=True, timeout=30,
        )
        s = result.stdout.strip()
        return float(s) if s else None
    except (ValueError, subprocess.TimeoutExpired, FileNotFoundError):
        return None


def detect_short_videos(dry_run: bool = True, max_duration_secs: float = 3.0) -> None:
    """Find local video files ≤ max_duration_secs, locate + stage them in Google
    Photos via Playwright, then delete locally once staging is confirmed.

    Requires both ffprobe (duration check) and Playwright — run via the
    `delete` service, same as `delete`/`stage`:
        docker compose run -p 6080:6080 delete detect-short-videos --no-dry-run

    After this command, run:
        docker compose run -p 6080:6080 delete delete --album=short-videos --no-dry-run --confirm
    to trash them from Google Photos via Playwright.
    """
    db.init_db()

    with db.get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, filename, local_path, local_timestamp
            FROM media_items
            WHERE local_video_purged_at IS NULL
              AND (label IS NULL OR label != 'short_video')
            """
        ).fetchall()

    candidates = [
        dict(row) for row in rows
        if _is_video(Path(row["local_path"])) and Path(row["local_path"]).exists()
    ]

    log_info("detect_short_videos", "Scanning local videos", total=len(candidates))
    print(f"Scanning {len(candidates)} local video file(s) for duration ≤ {max_duration_secs}s…")

    short_items: list[dict] = []

    for item in candidates:
        duration = _ffprobe_duration(Path(item["local_path"]))
        if duration is None:
            log_item("detect_short_videos", "duration_unknown",
                     item_id=item["id"], filename=item["filename"])
            continue
        if duration <= max_duration_secs:
            log_item("detect_short_videos", "short_video_found",
                     item_id=item["id"], filename=item["filename"],
                     duration_secs=round(duration, 2))
            short_items.append(item)

    print(f"Found {len(short_items)} video(s) ≤ {max_duration_secs}s.")

    if not short_items:
        print("Nothing to do.")
        return

    if dry_run:
        print(f"\n[DRY-RUN] Would locate + stage into '{_SHORT_VIDEO_ALBUM_TITLE}' and delete locally:")
        for item in short_items[:30]:
            print(f"  {item['filename']}  (id={item['id']})")
        if len(short_items) > 30:
            print(f"  … and {len(short_items) - 30} more")
        print(
            "\nRe-run with --no-dry-run to locate, stage, and delete locally.\n"
            "Then trash from Google Photos with:\n"
            "  docker compose run -p 6080:6080 delete delete --album=short-videos --no-dry-run --confirm"
        )
        return

    with db.get_conn() as conn:
        for item in short_items:
            conn.execute(
                "UPDATE media_items SET label='short_video', updated_at=? WHERE id=?",
                (db.now_iso(), item["id"]),
            )

    from . import locate_stage
    from playwright.sync_api import sync_playwright

    creds = auth.get_credentials()
    album_id = staging.get_or_create_album(creds, "short_video", _SHORT_VIDEO_ALBUM_TITLE)

    with sync_playwright() as pw:
        context = browser.launch_context(pw)
        page = context.new_page()
        browser.ensure_logged_in(page, "https://photos.google.com")
        try:
            staged, unmatched = locate_stage.stage_items(
                page, "short_video", album_id, _SHORT_VIDEO_ALBUM_TITLE, short_items
            )
        finally:
            context.close()

    # Only delete local copies once cloud staging is confirmed for that item.
    with db.get_conn() as conn:
        staged_ids = {
            r["id"] for r in conn.execute(
                "SELECT id FROM media_items WHERE staged_album_id=?", (album_id,)
            ).fetchall()
        }

    deleted_locally = 0
    for item in short_items:
        if item["id"] not in staged_ids:
            continue
        path = Path(item["local_path"])
        if path.exists():
            path.unlink()
        with db.get_conn() as conn:
            db.set_video_purged(conn, item["id"])
        deleted_locally += 1
        log_item("detect_short_videos", "staged_and_deleted_locally",
                 item_id=item["id"], filename=item["filename"], album_id=album_id)

    print(
        f"\nDone: {staged} staged into '{_SHORT_VIDEO_ALBUM_TITLE}' and removed locally, "
        f"{unmatched} not found in the cloud (left in place — re-run to retry)."
        "\n\nNEXT STEP — trash from Google Photos (requires Playwright; test on a small album first):"
        "\n  docker compose run -p 6080:6080 delete delete --album=short-videos --no-dry-run --confirm"
    )


def purge_local_videos(dry_run: bool = True) -> None:
    """Delete all video files from library/ locally; Google Photos copies are untouched.

    Run detect-short-videos first so ≤3s videos are already staged for cloud deletion.
    Items already labeled 'short_video' are skipped (they're handled separately).
    """
    db.init_db()

    if not _LIBRARY_DIR.exists():
        print(f"Library directory not found: {_LIBRARY_DIR}")
        return

    video_files = sorted(p for p in _LIBRARY_DIR.rglob("*") if p.is_file() and _is_video(p))
    log_info("purge_local_videos", "Video files found on disk", count=len(video_files))
    print(f"Found {len(video_files)} video file(s) in library/.")

    deleted = 0
    skipped_short = 0

    for path in video_files:
        with db.get_conn() as conn:
            row = conn.execute(
                "SELECT id, label FROM media_items WHERE local_path=?",
                (str(path),),
            ).fetchone()

        if row and row["label"] == "short_video":
            skipped_short += 1
            continue

        if dry_run:
            log_item("purge_local_videos", "would_delete", path=str(path), in_db=bool(row))
            deleted += 1
        else:
            try:
                path.unlink()
                log_item("purge_local_videos", "deleted", path=str(path))
                if row:
                    with db.get_conn() as conn:
                        db.set_video_purged(conn, row["id"])
                deleted += 1
            except OSError as e:
                log_error("purge_local_videos", "delete_failed", path=str(path), error=str(e))

    dry_tag = "[DRY-RUN] " if dry_run else ""
    print(f"\n{dry_tag}purge-local-videos summary:")
    print(f"  {'Would delete' if dry_run else 'Deleted'}:              {deleted} file(s)")
    print(f"  Skipped (short_video label): {skipped_short} file(s)")
    if dry_run and deleted:
        print("\nRe-run with --no-dry-run to delete. Google Photos copies will not be affected.")
    elif not dry_run:
        print("Local videos purged. Their Google Photos copies are untouched.")
