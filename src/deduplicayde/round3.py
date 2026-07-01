"""Round 3: Mirror confirmed cloud deletions to local files.

deletion.py already writes deletion_status='deleted' the moment a Playwright
trash action succeeds — that write IS the authoritative record of what's
actually been removed from Google Photos, so there's nothing left to
reconcile against the cloud (the API can't enumerate the library to diff
against anyway — see CLAUDE.md). Round 3 is now a fast, local-only pass:
delete the local copy for anything already marked deleted, except files
under /data/receipts/, which are permanent and never touched here.

Run:
    docker compose run cli round3 --dry-run
    docker compose run cli round3
"""
import os
from pathlib import Path

from . import db
from .logger import log_info, log_item, log_error

_DATA_DIR = os.environ.get("DATA_DIR", "/data")
_RECEIPTS_DIR = Path(_DATA_DIR) / "receipts"
_ROUND = "round3"


def run(dry_run: bool = True) -> None:
    db.init_db()
    log_info(_ROUND, "Starting Round 3: local cleanup of confirmed cloud deletions", dry_run=dry_run)

    with db.get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, filename, local_path
            FROM media_items
            WHERE deletion_status='deleted' AND local_path IS NOT NULL
            """
        ).fetchall()

    log_info(_ROUND, "Confirmed-deleted rows found", count=len(rows))

    if not rows:
        print("Nothing to do — no items marked deleted since last run.")
        return

    deleted_locally = 0
    already_gone = 0
    skipped_receipts = 0
    errors = 0

    for row in rows:
        local_path = Path(row["local_path"])

        # Never touch the receipts folder — permanent, excluded from this sync.
        try:
            local_path.resolve().relative_to(_RECEIPTS_DIR.resolve())
            log_item(_ROUND, "skipped_receipts_dir", item_id=row["id"], path=str(local_path))
            skipped_receipts += 1
            continue
        except ValueError:
            pass  # not under receipts dir — proceed

        if not local_path.exists():
            log_item(_ROUND, "already_gone", item_id=row["id"], path=str(local_path))
            already_gone += 1
            continue

        if dry_run:
            log_item(
                _ROUND, "would_delete_local",
                item_id=row["id"], path=str(local_path), filename=row["filename"],
            )
        else:
            try:
                local_path.unlink()
                log_item(_ROUND, "deleted_local", item_id=row["id"], path=str(local_path))
                deleted_locally += 1
            except OSError as e:
                log_error(
                    _ROUND, "delete_failed",
                    item_id=row["id"], path=str(local_path), error=str(e),
                )
                errors += 1

    if not dry_run:
        with db.get_conn() as conn:
            db.mark_round_complete(conn, _ROUND)

    dry_tag = "[DRY-RUN] " if dry_run else ""
    print(f"\n{dry_tag}Round 3 local cleanup:")
    print(f"  Confirmed deleted in Photos: {len(rows)}")
    print(f"  Skipped (in receipts dir):   {skipped_receipts}")
    print(f"  Already gone locally:        {already_gone}")
    if dry_run:
        to_delete = len(rows) - skipped_receipts - already_gone
        print(f"  Would delete locally:        {to_delete}")
        print("\nRe-run without --dry-run to delete local copies.")
    else:
        print(f"  Deleted locally:             {deleted_locally}")
        print(f"  Errors:                      {errors}")
