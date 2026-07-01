"""Round 4: Perceptual-hash duplicate detection.

Computes phash for all remaining local files that haven't been deleted/staged,
clusters pairs within the Hamming distance threshold, and stores them in
state.db for the review web app.

Run:
    docker compose run cli round4 [--threshold=10]
    docker compose up review   # then open http://localhost:8000
"""
import os
from itertools import combinations
from pathlib import Path

import imagehash
from PIL import Image
from tqdm import tqdm

from . import db
from .logger import log_info, log_item, log_error

_ROUND = "round4"
_HAMMING_THRESHOLD = int(os.environ.get("PHASH_HAMMING_THRESHOLD", "10"))

_IMAGE_SUFFIXES = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif",
    ".tif", ".tiff", ".bmp",
}


def _open_image(path: Path):
    try:
        from pillow_heif import register_heif_opener
        register_heif_opener()
    except ImportError:
        pass
    return Image.open(path)


def _compute_phash(path: Path) -> str | None:
    try:
        img = _open_image(path)
        return str(imagehash.phash(img))
    except Exception:
        return None


def run(threshold: int | None = None) -> None:
    db.init_db()
    hamming_threshold = threshold if threshold is not None else _HAMMING_THRESHOLD
    log_info(_ROUND, "Starting Round 4: phash duplicate detection", threshold=hamming_threshold)

    # Compute phashes for items that don't have one yet
    with db.get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, local_path
            FROM media_items
            WHERE phash IS NULL
              AND deletion_status IS NULL
              AND (label IS NULL OR label = 'ok')
            """
        ).fetchall()

    log_info(_ROUND, "Computing phashes", count=len(rows))

    with tqdm(rows, desc="Computing phashes", unit=" files") as bar:
        for row in bar:
            path = Path(row["local_path"])
            if not path.exists() or path.suffix.lower() not in _IMAGE_SUFFIXES:
                bar.update(1)
                continue

            phash = _compute_phash(path)
            if phash:
                with db.get_conn() as conn:
                    conn.execute(
                        "UPDATE media_items SET phash=?, updated_at=? WHERE id=?",
                        (phash, db.now_iso(), row["id"]),
                    )
            bar.update(1)

    # Load all items with phashes
    with db.get_conn() as conn:
        all_items = conn.execute(
            """
            SELECT id, local_path, phash
            FROM media_items
            WHERE phash IS NOT NULL
              AND deletion_status IS NULL
              AND (label IS NULL OR label = 'ok')
            """
        ).fetchall()

        existing_pairs = {
            (r["item_a_id"], r["item_b_id"])
            for r in conn.execute("SELECT item_a_id, item_b_id FROM duplicate_pairs").fetchall()
        }

    log_info(_ROUND, "Clustering by phash", item_count=len(all_items))

    # Compare all pairs — O(n²) but fine for typical library sizes after filtering
    new_pairs = 0
    items = list(all_items)
    hashes = [(row["id"], imagehash.hex_to_hash(row["phash"]), row["local_path"])
              for row in items]

    with tqdm(total=len(hashes) * (len(hashes) - 1) // 2, desc="Finding pairs", unit=" pairs") as bar:
        for i in range(len(hashes)):
            for j in range(i + 1, len(hashes)):
                id_a, hash_a, _ = hashes[i]
                id_b, hash_b, _ = hashes[j]
                bar.update(1)

                pair_key = (id_a, id_b)
                if pair_key in existing_pairs:
                    continue

                distance = hash_a - hash_b
                if distance <= hamming_threshold:
                    with db.get_conn() as conn:
                        conn.execute(
                            """INSERT OR IGNORE INTO duplicate_pairs
                               (item_a_id, item_b_id, hamming_distance, review_status)
                               VALUES (?,?,?,'pending')""",
                            (id_a, id_b, distance),
                        )
                    existing_pairs.add(pair_key)
                    new_pairs += 1
                    log_item(
                        _ROUND, "pair_found",
                        id_a=id_a, id_b=id_b, distance=distance,
                    )

    with db.get_conn() as conn:
        total_pending = conn.execute(
            "SELECT COUNT(*) FROM duplicate_pairs WHERE review_status='pending'"
        ).fetchone()[0]
        db.mark_round_complete(conn, _ROUND)

    log_info(_ROUND, "Round 4 complete", new_pairs=new_pairs, total_pending=total_pending)
    print(
        f"\nRound 4 done: {new_pairs} new duplicate pairs found, "
        f"{total_pending} total pending review.\n"
        "Start the review app: docker compose up review\n"
        "Then open http://localhost:8000 in your browser."
    )
