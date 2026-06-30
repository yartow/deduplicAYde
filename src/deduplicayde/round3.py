"""Round 3: Reconcile the API state against local files.

After deletions in Google Photos, re-pull the full mediaItems list and diff
it against state.db to find what was actually removed.  Mirror those deletions
locally (move to trash or log them), *except* files already in /data/receipts/.

Run:
    docker compose run cli round3 --dry-run
    docker compose run cli round3
"""
import os
import shutil
from pathlib import Path

from . import db, rclone_api
from .logger import log_info, log_item, log_error

_DATA_DIR = os.environ.get("DATA_DIR", "/data")
_RECEIPTS_DIR = Path(_DATA_DIR) / "receipts"
_ROUND = "round3"


def run(dry_run: bool = True) -> None:
    db.init_db()
    log_info(_ROUND, "Starting Round 3: cloud/local reconciliation", dry_run=dry_run)

    # Re-pull all current IDs via rclone
    print("Fetching current library via rclone (may take a few minutes)…")
    live_ids: set[str] = rclone_api.list_all_media_item_ids()
    log_info(_ROUND, "Live IDs fetched via rclone", count=len(live_ids))

    # Find items in our DB that no longer exist in the API (deleted in Photos)
    with db.get_conn() as conn:
        rows = conn.execute(
            """
            SELECT media_item_id, filename, local_path, label, deletion_status
            FROM media_items
            WHERE local_path IS NOT NULL AND deletion_status IS NULL
            """
        ).fetchall()

    confirmed_deleted = []
    for row in rows:
        if row["media_item_id"] not in live_ids:
            confirmed_deleted.append(dict(row))

    log_info(_ROUND, "Items confirmed deleted in Photos", count=len(confirmed_deleted))

    if not confirmed_deleted:
        print("No local files to sync — nothing was deleted in Google Photos since last run.")
        return

    # Mirror deletions locally
    moved = 0
    skipped_receipts = 0
    errors = 0

    for item in confirmed_deleted:
        local_path = Path(item["local_path"])

        # Never touch the receipts folder
        try:
            local_path.resolve().relative_to(_RECEIPTS_DIR.resolve())
            log_item(
                _ROUND, "skipped_receipts_dir",
                media_item_id=item["media_item_id"],
                path=str(local_path),
            )
            skipped_receipts += 1
            # Still mark as deleted in DB so we know
            if not dry_run:
                with db.get_conn() as conn:
                    db.set_deleted(conn, item["media_item_id"])
            continue
        except ValueError:
            pass  # Not under receipts dir — proceed

        if not local_path.exists():
            log_item(
                _ROUND, "already_gone",
                media_item_id=item["media_item_id"],
                path=str(local_path),
            )
            if not dry_run:
                with db.get_conn() as conn:
                    db.set_deleted(conn, item["media_item_id"])
            continue

        if dry_run:
            log_item(
                _ROUND, "would_delete_local",
                media_item_id=item["media_item_id"],
                path=str(local_path),
                filename=item["filename"],
            )
        else:
            try:
                local_path.unlink()
                log_item(
                    _ROUND, "deleted_local",
                    media_item_id=item["media_item_id"],
                    path=str(local_path),
                )
                with db.get_conn() as conn:
                    db.set_deleted(conn, item["media_item_id"])
                moved += 1
            except OSError as e:
                log_error(
                    _ROUND, "delete_failed",
                    media_item_id=item["media_item_id"],
                    path=str(local_path),
                    error=str(e),
                )
                errors += 1

    if not dry_run:
        with db.get_conn() as conn:
            db.mark_round_complete(conn, _ROUND)

    dry_tag = "[DRY-RUN] " if dry_run else ""
    print(f"\n{dry_tag}Round 3 reconciliation:")
    print(f"  Confirmed deleted in Photos: {len(confirmed_deleted)}")
    print(f"  Skipped (in receipts dir):   {skipped_receipts}")
    if dry_run:
        print(f"  Would delete locally:        {len(confirmed_deleted) - skipped_receipts}")
        print("\nRe-run without --dry-run to delete local copies.")
    else:
        print(f"  Deleted locally:             {moved}")
        print(f"  Errors:                      {errors}")
