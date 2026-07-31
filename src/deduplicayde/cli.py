"""Main CLI entry point.

docker compose run cli <command> [options]

Commands:
  auth                   Authenticate with Google Photos API (run once, opens port 8080)
  login                  One-time: log into Google in a plain browser window (use delete service)
  round0                 Catalog local files (filename, EXIF/sidecar timestamp, path)
  reorganize             Sort library/ into yyyy-mm-dd date folders (EXIF-first, sidecar fallback)
  exif-backfill          Fill missing EXIF DateTimeOriginal/GPS from sidecars, for Lightroom import
  round1                 Detect receipts/vague (first half of library)
  round2                 Detect receipts/vague (second half)
  round3                 Locally delete files whose cloud copy was trashed
  round4                 Compute phashes, find duplicate pairs
  vague-threshold        Preview/apply a different blur/edge cutoff for 'vague', reusing stored scores
  quality-score          AI no-reference image-quality scoring (native or Docker; use scripts/run_quality_score.sh)
  stage --purpose=...    Locate detected items on photos.google.com, add to review album (use delete service)
  detect-short-videos    Find ≤3s videos: stage for cloud deletion, delete locally
  purge-local-videos     Delete all video files locally (Google Photos copies kept)
  delete --album=...     Trash staged items via Playwright (use delete service)
  status                 Print per-round progress summary
"""
import argparse
import sys
import os

from . import db


def cmd_auth(_args) -> None:
    print(
        "Starting OAuth flow. Watch for a 'Please visit this URL' line below and "
        "open THAT URL (accounts.google.com) — not localhost:8080 directly. "
        "localhost:8080 is just the callback the browser gets redirected to "
        "after you approve access on Google's page."
    )
    from . import auth
    auth.get_credentials()
    print("Authenticated successfully. Token saved.")


def cmd_login(_args) -> None:
    from . import browser
    browser.manual_login()


def cmd_round0(args) -> None:
    from . import round0
    round0.run(limit=args.limit)


def cmd_reorganize(args) -> None:
    from . import reorganize
    try:
        reorganize.run(dry_run=args.dry_run, limit=args.limit)
    except reorganize._AlreadyRunningError as e:
        print(f"\nError: {e}")
        raise SystemExit(1)


def cmd_exif_backfill(args) -> None:
    from . import exif_backfill
    try:
        exif_backfill.run(dry_run=args.dry_run, limit=args.limit)
    except exif_backfill._AlreadyRunningError as e:
        print(f"\nError: {e}")
        raise SystemExit(1)


def cmd_round1(_args) -> None:
    from . import round1_2
    round1_2.run(half=1)


def cmd_round2(_args) -> None:
    from . import round1_2
    round1_2.run(half=2)


def cmd_round3(args) -> None:
    from . import round3
    round3.run(dry_run=args.dry_run)


def cmd_round4(args) -> None:
    from . import round4
    round4.run(threshold=args.threshold)


def cmd_vague_threshold(args) -> None:
    from . import vague_threshold
    vague_threshold.run(
        blur_threshold=args.blur_threshold,
        edge_threshold=args.edge_threshold,
        apply_changes=args.apply,
    )


def cmd_quality_score(args) -> None:
    from . import quality_score
    quality_score.run(
        tier=args.tier, device=args.device, sample_n=args.sample, seed=args.seed,
        assume_yes=args.yes,
    )


def cmd_detect_short_videos(args) -> None:
    from . import video_ops
    video_ops.detect_short_videos(dry_run=args.dry_run, max_duration_secs=args.max_duration)


def cmd_purge_local_videos(args) -> None:
    from . import video_ops
    video_ops.purge_local_videos(dry_run=args.dry_run)


def cmd_delete(args) -> None:
    from . import deletion
    deletion.run(
        album=args.album,
        confirm=args.confirm,
        dry_run=args.dry_run,
    )


def cmd_stage(args) -> None:
    from . import locate_stage
    locate_stage.run(purpose=args.purpose, dry_run=args.dry_run)


def cmd_status(_args) -> None:
    db.init_db()
    with db.get_conn() as conn:
        # Overall counts
        total = conn.execute("SELECT COUNT(*) FROM media_items").fetchone()[0]
        mapped = conn.execute(
            "SELECT COUNT(*) FROM media_items WHERE local_path IS NOT NULL"
        ).fetchone()[0]
        labeled = conn.execute(
            "SELECT COUNT(*) FROM media_items WHERE label IS NOT NULL"
        ).fetchone()[0]
        staged = conn.execute(
            "SELECT COUNT(*) FROM media_items WHERE staged_album_id IS NOT NULL"
        ).fetchone()[0]
        deleted = conn.execute(
            "SELECT COUNT(*) FROM media_items WHERE deletion_status='deleted'"
        ).fetchone()[0]
        video_purged = conn.execute(
            "SELECT COUNT(*) FROM media_items WHERE local_video_purged_at IS NOT NULL"
        ).fetchone()[0]

        label_counts = conn.execute(
            "SELECT label, COUNT(*) as n FROM media_items WHERE label IS NOT NULL GROUP BY label"
        ).fetchall()

        pair_counts = conn.execute(
            "SELECT review_status, COUNT(*) as n FROM duplicate_pairs GROUP BY review_status"
        ).fetchall()

        rounds = conn.execute(
            "SELECT round_name, items_processed, items_total, completed_at FROM round_progress"
        ).fetchall()

    print("\n=== deduplicAYde status ===\n")
    print(f"  Media items in DB:   {total}")
    print(f"  Mapped to local:     {mapped}")
    print(f"  Detection complete:  {labeled}")
    print(f"  Staged in albums:    {staged}")
    print(f"  Deleted from Photos: {deleted}")
    print(f"  Videos purged (local only): {video_purged}")

    if label_counts:
        print("\n  Labels:")
        for r in label_counts:
            print(f"    {r['label']:12s} {r['n']}")

    if pair_counts:
        print("\n  Duplicate pairs:")
        for r in pair_counts:
            print(f"    {r['review_status']:12s} {r['n']}")

    if rounds:
        print("\n  Round progress:")
        for r in rounds:
            done = "✓" if r["completed_at"] else " "
            print(f"    [{done}] {r['round_name']:10s}  {r['items_processed']} items")

    print()


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="deduplicayde",
        description="Google Photos cleanup pipeline",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # auth
    sub.add_parser("auth", help="Authenticate with Google Photos API (run once, port 8080)")

    # login
    sub.add_parser(
        "login",
        help="One-time: log into Google in a plain, non-automated browser window (use delete service)",
    )

    # round0
    p0 = sub.add_parser("round0", help="Catalog local files (filename, EXIF/sidecar timestamp, path)")
    p0.add_argument("--limit", type=int, default=None, help="Stop after N files (testing)")

    # reorganize
    pr = sub.add_parser(
        "reorganize",
        help="Sort library/ into yyyy-mm-dd date folders (EXIF-first, sidecar fallback; see reorganize.py)",
    )
    pr.add_argument("--dry-run", action="store_true", default=True)
    pr.add_argument("--no-dry-run", dest="dry_run", action="store_false")
    pr.add_argument("--limit", type=int, default=None, help="Stop after N files (testing)")

    # exif-backfill
    peb = sub.add_parser(
        "exif-backfill",
        help="Fill missing EXIF DateTimeOriginal/GPS from sidecars via exiftool (run after reorganize)",
    )
    peb.add_argument("--dry-run", action="store_true", default=True)
    peb.add_argument("--no-dry-run", dest="dry_run", action="store_false")
    peb.add_argument("--limit", type=int, default=None, help="Stop scanning after N candidate files (testing)")

    # round1
    p1 = sub.add_parser("round1", help="Detect items (first half)")

    # round2
    p2 = sub.add_parser("round2", help="Detect items (second half)")

    # round3
    p3 = sub.add_parser("round3", help="Locally delete files whose cloud copy was trashed")
    p3.add_argument("--dry-run", action="store_true", default=True)
    p3.add_argument("--no-dry-run", dest="dry_run", action="store_false")

    # round4
    p4 = sub.add_parser("round4", help="Compute phashes and find duplicate pairs")
    p4.add_argument("--threshold", type=int, default=None,
                    help="Hamming distance threshold (default: PHASH_HAMMING_THRESHOLD env or 10)")

    # vague-threshold
    pvt = sub.add_parser(
        "vague-threshold",
        help="Preview/apply a different blur/edge cutoff for the 'vague' label, reusing stored scores (no re-detection)",
    )
    pvt.add_argument("--blur-threshold", type=float, default=None,
                     help="Laplacian variance cutoff, lower = stricter (default: BLUR_THRESHOLD env or 100)")
    pvt.add_argument("--edge-threshold", type=float, default=None,
                     help="Edge density cutoff, lower = stricter (default: EDGE_THRESHOLD env or 0.05)")
    pvt.add_argument("--apply", action="store_true", default=False,
                     help="Persist the new labels (default: preview-only, no writes)")

    # quality-score
    pqs = sub.add_parser(
        "quality-score",
        help="AI no-reference image-quality scoring (native or Docker per "
             "QUALITY_SCORING_DOCKER; invoke via scripts/run_quality_score.sh)",
    )
    pqs.add_argument("--tier", choices=["medium", "heavyweight"], default=None,
                     help="Model tier (default: QUALITY_TIER env or 'medium')")
    pqs.add_argument("--sample", type=int, default=None, metavar="N",
                     help="Trial mode: score a random sample of N eligible images, "
                          "write an HTML report to DATA_DIR/logs/, no DB writes. "
                          "Omit for a full run over all unscored items.")
    pqs.add_argument("--device", choices=["auto", "mps", "cpu"], default="auto",
                     help="Inference device (default: auto-detect MPS, fall back to CPU)")
    pqs.add_argument("--seed", type=int, default=None,
                     help="Random seed for --sample reproducibility")
    pqs.add_argument("--yes", "-y", action="store_true", default=False,
                     help="Skip the confirmation prompt shown before any CPU-bound "
                          "(no-GPU) run. Required if stdin isn't a TTY (e.g. scripted).")

    # detect-short-videos
    pdsv = sub.add_parser(
        "detect-short-videos",
        help="Find ≤3s videos: locate + stage for cloud deletion, delete locally (use delete service)",
    )
    pdsv.add_argument("--dry-run", action="store_true", default=True)
    pdsv.add_argument("--no-dry-run", dest="dry_run", action="store_false")
    pdsv.add_argument(
        "--max-duration", type=float, default=3.0,
        help="Max video duration in seconds to flag (default: 3.0)",
    )

    # purge-local-videos
    pplv = sub.add_parser(
        "purge-local-videos",
        help="Delete all video files from library/ locally (Google Photos copies kept)",
    )
    pplv.add_argument("--dry-run", action="store_true", default=True)
    pplv.add_argument("--no-dry-run", dest="dry_run", action="store_false")

    # delete
    pd = sub.add_parser("delete", help="Trash staged items via Playwright (use delete service)")
    pd.add_argument("--album", required=True, choices=["receipts", "vague", "short-videos"],
                    help="Which album to delete from")
    pd.add_argument("--dry-run", action="store_true", default=True)
    pd.add_argument("--no-dry-run", dest="dry_run", action="store_false")
    pd.add_argument("--confirm", action="store_true", default=False,
                    help="Required to actually proceed (without --dry-run)")

    # stage
    ps = sub.add_parser(
        "stage",
        help="Locate detected items on photos.google.com and add to a review album (use delete service)",
    )
    ps.add_argument("--purpose", required=True, choices=["receipt", "vague"],
                    help="Which detection label to stage")
    ps.add_argument("--dry-run", action="store_true", default=True)
    ps.add_argument("--no-dry-run", dest="dry_run", action="store_false")

    # status
    sub.add_parser("status", help="Print progress summary")

    args = parser.parse_args()

    dispatch = {
        "auth": cmd_auth,
        "login": cmd_login,
        "round0": cmd_round0,
        "reorganize": cmd_reorganize,
        "exif-backfill": cmd_exif_backfill,
        "round1": cmd_round1,
        "round2": cmd_round2,
        "round3": cmd_round3,
        "round4": cmd_round4,
        "vague-threshold": cmd_vague_threshold,
        "quality-score": cmd_quality_score,
        "detect-short-videos": cmd_detect_short_videos,
        "purge-local-videos": cmd_purge_local_videos,
        "delete": cmd_delete,
        "stage": cmd_stage,
        "status": cmd_status,
    }

    try:
        dispatch[args.command](args)
    except KeyboardInterrupt:
        print("\nInterrupted. Progress saved — re-run the same command to resume.")
        sys.exit(130)
    except FileNotFoundError as e:
        print(f"\nError: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
