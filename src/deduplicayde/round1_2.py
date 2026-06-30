"""Rounds 1 & 2: detect receipts and vague photos, then stage them.

Both rounds do the same work: process all local files that haven't been
labeled yet.  The 'half' distinction matches which Takeout batch you imported:

  Import Takeout batch 1  ->  run round1
  Import Takeout batch 2  ->  run round2

Run:
    docker compose run cli round1 --dry-run         # preview
    docker compose run cli round1 --no-dry-run      # actually stage
    docker compose run cli round2 --no-dry-run
"""
import os
from pathlib import Path

from tqdm import tqdm

from . import auth, db, staging
from .detection import analyze
from .logger import log_info, log_item, log_error

_CHECKPOINT_EVERY = 50


def run(half: int, dry_run: bool = True) -> None:
    round_name = f"round{half}"
    db.init_db()
    log_info(round_name, f"Starting Round {half}", dry_run=dry_run)

    # Process all items with a local path that haven't been labeled yet
    with db.get_conn() as conn:
        rows = conn.execute(
            """
            SELECT media_item_id, local_path
            FROM media_items
            WHERE local_path IS NOT NULL
              AND label IS NULL
              AND deletion_status IS NULL
            ORDER BY creation_time ASC
            """
        ).fetchall()

    total = len(rows)
    if total == 0:
        print(
            "No unprocessed local files found.\n"
            "Make sure round0 has run and your Takeout files are in /data/library/."
        )
        return

    log_info(round_name, "Items to detect", count=total)

    creds = auth.get_credentials()
    album_ids = staging.get_or_create_albums(creds)

    pending_receipts: list[str] = []
    pending_vague: list[str] = []
    processed = 0
    errors = 0

    with tqdm(rows, desc=f"Round {half}: detecting", unit=" files") as bar:
        for row in bar:
            media_item_id = row["media_item_id"]
            local_path = Path(row["local_path"])

            if not local_path.exists():
                log_item(
                    round_name, "file_missing",
                    media_item_id=media_item_id, path=str(local_path),
                )
                bar.update(1)
                continue

            try:
                result = analyze(local_path)
            except Exception as e:
                log_error(round_name, "detection_error", media_item_id=media_item_id, error=str(e))
                errors += 1
                bar.update(1)
                continue

            if result.error:
                log_item(
                    round_name, "skipped",
                    media_item_id=media_item_id, reason=result.error,
                )
                bar.update(1)
                continue

            with db.get_conn() as conn:
                db.set_detection_result(
                    conn,
                    media_item_id,
                    result.blur_score,
                    result.edge_density,
                    result.ocr_text_density,
                    result.label,
                )

            log_item(
                round_name,
                "detected",
                media_item_id=media_item_id,
                path=str(local_path),
                label=result.label,
                blur=round(result.blur_score, 2),
                edge=round(result.edge_density, 4),
                ocr=round(result.ocr_text_density, 6),
            )
            bar.set_postfix(label=result.label, refresh=False)

            if result.label == "receipt":
                pending_receipts.append(media_item_id)
            elif result.label == "vague":
                pending_vague.append(media_item_id)

            processed += 1

            if len(pending_receipts) >= _CHECKPOINT_EVERY:
                staging.stage_items(
                    creds, "receipt", album_ids["receipt"], pending_receipts, dry_run=dry_run
                )
                pending_receipts.clear()

            if len(pending_vague) >= _CHECKPOINT_EVERY:
                staging.stage_items(
                    creds, "vague", album_ids["vague"], pending_vague, dry_run=dry_run
                )
                pending_vague.clear()

    # Flush remaining
    if pending_receipts:
        staging.stage_items(creds, "receipt", album_ids["receipt"], pending_receipts, dry_run=dry_run)
    if pending_vague:
        staging.stage_items(creds, "vague", album_ids["vague"], pending_vague, dry_run=dry_run)

    with db.get_conn() as conn:
        label_rows = conn.execute(
            "SELECT label, COUNT(*) as n FROM media_items WHERE label IS NOT NULL GROUP BY label"
        ).fetchall()
        db.mark_round_complete(conn, round_name)

    label_summary = {r["label"]: r["n"] for r in label_rows}
    dry_tag = " [DRY-RUN]" if dry_run else ""
    print(f"\nRound {half} done{dry_tag}: {processed} files processed, {errors} errors")
    for label, count in label_summary.items():
        print(f"  {label}: {count}")

    if dry_run:
        print(
            "\nRe-run with --no-dry-run to actually stage items into Google Photos albums."
        )
    else:
        print(
            "\nItems staged. For receipts: run 'delete --album=receipts --no-dry-run --confirm'."
            "\nFor vague: review the 'deduplicAYde – Vague' album in Google Photos first."
        )
