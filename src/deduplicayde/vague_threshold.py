"""Trial-and-error tuning of the 'vague' (blurry/low-content) classification.

round1_2.py already computes and stores raw blur_score (Laplacian variance)
and edge_density per item, then labels each one 'vague' with a single
hardcoded cutoff (BLUR_THRESHOLD/EDGE_THRESHOLD env vars) baked in at
analysis time. Trying a different cutoff previously meant re-running OpenCV
detection. Since the raw scores are already in the DB, this module just
re-derives the label from them under a candidate threshold — instant, no
image analysis re-run.

Only items currently labeled 'vague' or 'ok' are ever touched. 'receipt' rows
are left alone: the receipt check runs first in detection._classify and
doesn't depend on the blur/edge thresholds being tuned here, so re-deriving
their label from stored data risks nothing but also changes nothing — they're
excluded purely so a --apply run's diff only reports what the chosen
threshold actually affects. Items already staged or deleted are excluded too
(rule 1: never reprocess/relabel work that's already moved further down the
pipeline).

Usage:
    docker compose run cli vague-threshold --blur-threshold 150 --edge-threshold 0.05
        -> preview only: prints how many items would flip label, no writes.
    docker compose run cli vague-threshold --blur-threshold 150 --edge-threshold 0.05 --apply
        -> writes the new labels and logs each change.
"""
from . import db
from .detection import _BLUR_THRESHOLD, _EDGE_THRESHOLD, _classify
from .logger import log_info, log_item

_ELIGIBLE_WHERE_SQL = """
    WHERE label IN ('vague', 'ok')
      AND blur_score IS NOT NULL
      AND edge_density IS NOT NULL
      AND staged_album_id IS NULL
      AND deletion_status IS NULL
"""
_ELIGIBLE_ROWS_SQL = (
    "SELECT id, blur_score, edge_density, ocr_text_density, label FROM media_items "
    + _ELIGIBLE_WHERE_SQL
)
_ELIGIBLE_COUNT_SQL = "SELECT COUNT(*) FROM media_items " + _ELIGIBLE_WHERE_SQL


def _diff(blur_threshold: float, edge_threshold: float) -> list[dict]:
    with db.get_conn() as conn:
        rows = conn.execute(_ELIGIBLE_ROWS_SQL).fetchall()

    changes = []
    for row in rows:
        new_label = _classify(
            row["blur_score"],
            row["edge_density"],
            row["ocr_text_density"] or 0.0,
            is_receipt_text=False,
            blur_threshold=blur_threshold,
            edge_threshold=edge_threshold,
        )
        if new_label != row["label"]:
            changes.append({
                "id": row["id"],
                "old_label": row["label"],
                "new_label": new_label,
                "blur_score": row["blur_score"],
                "edge_density": row["edge_density"],
            })
    return changes


def preview(blur_threshold: float, edge_density_threshold: float) -> None:
    with db.get_conn() as conn:
        total_eligible = conn.execute(_ELIGIBLE_COUNT_SQL).fetchone()[0]
        current_counts = conn.execute(
            "SELECT label, COUNT(*) as n FROM media_items WHERE label IS NOT NULL GROUP BY label"
        ).fetchall()

    changes = _diff(blur_threshold, edge_density_threshold)
    to_vague = sum(1 for c in changes if c["new_label"] == "vague")
    to_ok = sum(1 for c in changes if c["new_label"] == "ok")

    print(f"\n=== Vague-threshold preview (blur<{blur_threshold}, edge<{edge_density_threshold}) ===\n")
    print(f"  Current labels: " + ", ".join(f"{r['label']}={r['n']}" for r in current_counts))
    print(f"  Eligible for relabel (not yet staged/deleted): {total_eligible}")
    print(f"  Would flip ok -> vague:    {to_vague}")
    print(f"  Would flip vague -> ok:    {to_ok}")
    print(f"  Net change in 'vague' count: {to_vague - to_ok:+d}")

    if changes:
        print("\n  Sample of affected items (up to 10):")
        for c in changes[:10]:
            print(
                f"    id={c['id']:<8} {c['old_label']:>5} -> {c['new_label']:<5}"
                f"  blur={c['blur_score']:.1f} edge={c['edge_density']:.4f}"
            )

    print(
        "\nThis was a preview only — nothing was written. "
        "Re-run with --apply to persist these labels.\n"
    )


def apply(blur_threshold: float, edge_density_threshold: float) -> None:
    changes = _diff(blur_threshold, edge_density_threshold)
    log_info(
        "vague_threshold",
        "Applying new vague/ok labels",
        blur_threshold=blur_threshold,
        edge_threshold=edge_density_threshold,
        changed=len(changes),
    )

    with db.get_conn() as conn:
        for c in changes:
            db.set_label(conn, c["id"], c["new_label"])
            log_item(
                "vague_threshold",
                "relabeled",
                item_id=c["id"],
                old_label=c["old_label"],
                new_label=c["new_label"],
                blur_score=c["blur_score"],
                edge_density=c["edge_density"],
            )

    print(f"\nApplied: {len(changes)} items relabeled at blur<{blur_threshold}, edge<{edge_density_threshold}.")
    print("Run 'status' to see updated label counts, or 'stage --purpose=vague --dry-run' next.\n")


def run(blur_threshold: float | None, edge_threshold: float | None, apply_changes: bool) -> None:
    db.init_db()
    bt = blur_threshold if blur_threshold is not None else _BLUR_THRESHOLD
    et = edge_threshold if edge_threshold is not None else _EDGE_THRESHOLD

    if apply_changes:
        apply(bt, et)
    else:
        preview(bt, et)
