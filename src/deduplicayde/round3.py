"""Round 3: Mirror confirmed cloud deletions to local files.

deletion.py already writes deletion_status='deleted' the moment a Playwright
trash action succeeds — that write IS the authoritative record of what's
actually been removed from Google Photos, so there's nothing left to
reconcile against the cloud (the API can't enumerate the library to diff
against anyway — see CLAUDE.md). Round 3 is now a fast, local-only pass over
every row with deletion_status='deleted':
  - label='receipt': moved out of library/ into /data/receipts/ (flat, with an
    id-suffix on filename collisions) instead of being deleted — receipts are
    kept locally forever even though the cloud copy is gone. This is the only
    code path allowed to write into /data/receipts/.
  - everything else (vague, duplicates, ...): local copy is deleted, as before.
A row whose local_path already resolves under /data/receipts/ (i.e. a receipt
already archived by a prior run) is left untouched either way.

Run:
    docker compose run cli round3 --dry-run
    docker compose run cli round3
"""
import os
import shutil
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
            SELECT id, filename, local_path, label
            FROM media_items
            WHERE deletion_status='deleted' AND local_path IS NOT NULL
            """
        ).fetchall()

    log_info(_ROUND, "Confirmed-deleted rows found", count=len(rows))

    if not rows:
        print("Nothing to do — no items marked deleted since last run.")
        return

    deleted_locally = 0
    moved_to_receipts = 0
    already_gone = 0
    already_archived = 0
    errors = 0

    for row in rows:
        local_path = Path(row["local_path"])

        # Already archived by a prior run — permanent, never re-touched.
        try:
            local_path.resolve().relative_to(_RECEIPTS_DIR.resolve())
            log_item(_ROUND, "already_archived", item_id=row["id"], path=str(local_path))
            already_archived += 1
            continue
        except ValueError:
            pass  # not under receipts dir — proceed

        if not local_path.exists():
            log_item(_ROUND, "already_gone", item_id=row["id"], path=str(local_path))
            already_gone += 1
            continue

        if row["label"] == "receipt":
            dest = _RECEIPTS_DIR / local_path.name
            if dest.exists():
                dest = _RECEIPTS_DIR / f"{local_path.stem}_{row['id']}{local_path.suffix}"

            if dry_run:
                log_item(
                    _ROUND, "would_move_to_receipts",
                    item_id=row["id"], src=str(local_path), dest=str(dest),
                )
                moved_to_receipts += 1
            else:
                try:
                    _RECEIPTS_DIR.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(local_path), str(dest))
                    with db.get_conn() as inner_conn:
                        db.update_local_path(inner_conn, row["id"], str(dest))
                    log_item(
                        _ROUND, "moved_to_receipts",
                        item_id=row["id"], src=str(local_path), dest=str(dest),
                    )
                    moved_to_receipts += 1
                except OSError as e:
                    log_error(
                        _ROUND, "move_failed",
                        item_id=row["id"], path=str(local_path), error=str(e),
                    )
                    errors += 1
            continue

        if dry_run:
            log_item(
                _ROUND, "would_delete_local",
                item_id=row["id"], path=str(local_path), filename=row["filename"],
            )
            deleted_locally += 1
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
    move_label = "Would move to receipts/:     " if dry_run else "Moved to receipts/:          "
    delete_label = "Would delete locally:        " if dry_run else "Deleted locally:             "
    print(f"\n{dry_tag}Round 3 local cleanup:")
    print(f"  Confirmed deleted in Photos:  {len(rows)}")
    print(f"  Already archived (receipts):  {already_archived}")
    print(f"  Already gone locally:         {already_gone}")
    print(f"  {move_label}{moved_to_receipts}")
    print(f"  {delete_label}{deleted_locally}")
    if dry_run:
        print("\nRe-run without --dry-run to move receipts and delete the rest.")
    else:
        print(f"  Errors:                       {errors}")
