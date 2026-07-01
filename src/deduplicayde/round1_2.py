"""Rounds 1 & 2: detect receipts and vague photos.

Both rounds do the same work: process all local files that haven't been
labeled yet.  The 'half' distinction matches which Takeout batch you imported:

  Import Takeout batch 1  ->  run round1
  Import Takeout batch 2  ->  run round2

This is pure local detection (OpenCV blur/edge, Tesseract OCR) — no cloud
interaction, so it runs in the plain `cli` service. Staging detected items
into a Google Photos review album is a separate step (`stage --purpose=...`,
via the Playwright-capable `delete` service — see locate_stage.py) since
Google's API can no longer be used to add pre-existing items to an album
(see CLAUDE.md for why).

Run:
    docker compose run cli round1      # detect (first half)
    docker compose run cli round2      # detect (second half)
"""
from pathlib import Path

from tqdm import tqdm

from . import db
from .detection import analyze
from .logger import log_info, log_item, log_error


def run(half: int) -> None:
    round_name = f"round{half}"
    db.init_db()
    log_info(round_name, f"Starting Round {half}")

    # Process all items with a local path that haven't been labeled yet
    with db.get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, local_path
            FROM media_items
            WHERE label IS NULL
              AND deletion_status IS NULL
            ORDER BY local_timestamp ASC
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

    processed = 0
    errors = 0

    with tqdm(rows, desc=f"Round {half}: detecting", unit=" files") as bar:
        for row in bar:
            item_id = row["id"]
            local_path = Path(row["local_path"])

            if not local_path.exists():
                log_item(
                    round_name, "file_missing",
                    item_id=item_id, path=str(local_path),
                )
                bar.update(1)
                continue

            try:
                result = analyze(local_path)
            except Exception as e:
                log_error(round_name, "detection_error", item_id=item_id, error=str(e))
                errors += 1
                bar.update(1)
                continue

            if result.error:
                log_item(
                    round_name, "skipped",
                    item_id=item_id, reason=result.error,
                )
                bar.update(1)
                continue

            with db.get_conn() as conn:
                db.set_detection_result(
                    conn,
                    item_id,
                    result.blur_score,
                    result.edge_density,
                    result.ocr_text_density,
                    result.label,
                )

            log_item(
                round_name,
                "detected",
                item_id=item_id,
                path=str(local_path),
                label=result.label,
                blur=round(result.blur_score, 2),
                edge=round(result.edge_density, 4),
                ocr=round(result.ocr_text_density, 6),
            )
            bar.set_postfix(label=result.label, refresh=False)
            processed += 1

    with db.get_conn() as conn:
        label_rows = conn.execute(
            "SELECT label, COUNT(*) as n FROM media_items WHERE label IS NOT NULL GROUP BY label"
        ).fetchall()
        db.mark_round_complete(conn, round_name)

    label_summary = {r["label"]: r["n"] for r in label_rows}
    print(f"\nRound {half} done: {processed} files processed, {errors} errors")
    for label, count in label_summary.items():
        print(f"  {label}: {count}")

    print(
        "\nRun 'stage --purpose=receipt --dry-run' / 'stage --purpose=vague --dry-run'"
        " (via the delete service) to locate and stage detected items into a review album."
    )
