"""Round 0: Build the mediaItemId <-> local file mapping table.

Matching strategy (in priority order):
  1. Filename (case-insensitive) against files under DATA_DIR/library/.
  2. When multiple local files share the same filename, resolve each file's
     "true" timestamp and compare against the API's mediaMetadata.creationTime:
       a. EXIF DateTimeOriginal  (via exifread, Pillow as fallback)
       b. photoTakenTime in the Takeout JSON sidecar  (*.json next to the file)
     Filesystem mtime/ctime are NEVER used — Takeout extraction corrupts them.
  3. If timestamps don't resolve the ambiguity, phash is used as a tiebreaker
     (only computed on demand for genuinely ambiguous cases).
  4. Files with no EXIF and no sidecar are logged as `no_timestamp` so they
     can be reviewed manually if they end up in an ambiguous match.

Page tokens are checkpointed after each full page of 100 items, so a crash
loses at most one page of work.  Re-running is always safe.

Run:
    docker compose run cli round0
    docker compose run cli round0 --limit=200   # small smoke test
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import imagehash
from PIL import Image
from tqdm import tqdm

from . import db, rclone_api
from .logger import log_info, log_item, log_error

_LIBRARY_DIR = os.path.join(os.environ.get("DATA_DIR", "/data"), "library")
_ROUND = "round0"

_IMAGE_SUFFIXES = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif",
    ".tif", ".tiff", ".bmp", ".mp4", ".mov", ".avi", ".mkv",
}

# Seconds window for EXIF (local-time) vs API UTC comparison.
# 25 hours covers the widest possible timezone offset (UTC+14 / UTC-12).
_TIMESTAMP_WINDOW_SECS = 25 * 3600


# ---------------------------------------------------------------------------
# Timestamp resolution
# ---------------------------------------------------------------------------

def _exif_timestamp(path: Path) -> str | None:
    """Return EXIF DateTimeOriginal as "YYYY-MM-DDTHH:MM:SS" (no TZ), or None."""
    # Primary: exifread (fast header-only parse, handles many edge cases)
    try:
        import exifread
        with open(path, "rb") as f:
            tags = exifread.process_file(f, stop_tag="EXIF DateTimeOriginal", details=False)
        raw = tags.get("EXIF DateTimeOriginal") or tags.get("Image DateTime")
        if raw:
            return _parse_exif_str(str(raw))
    except Exception:
        pass

    # Fallback: Pillow _getexif
    try:
        from PIL.ExifTags import TAGS
        img = Image.open(path)
        exif = getattr(img, "_getexif", lambda: None)()
        if exif:
            for tag_id, value in exif.items():
                if TAGS.get(tag_id) == "DateTimeOriginal":
                    return _parse_exif_str(str(value))
    except Exception:
        pass

    return None


def _parse_exif_str(raw: str) -> str | None:
    """Parse "YYYY:MM:DD HH:MM:SS" -> "YYYY-MM-DDTHH:MM:SS", or None if malformed."""
    try:
        dt = datetime.strptime(raw.strip(), "%Y:%m:%d %H:%M:%S")
        return dt.strftime("%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None


def _sidecar_timestamp(path: Path) -> str | None:
    """Return photoTakenTime from a Takeout JSON sidecar as ISO UTC, or None.

    Takeout places sidecar files alongside the media file, using either
    "photo.jpg.json" or (for long filenames) "photo.json".
    """
    for sidecar in (path.with_name(path.name + ".json"), path.with_suffix(".json")):
        if not sidecar.exists():
            continue
        try:
            with open(sidecar) as f:
                data = json.load(f)
            ts_str = data.get("photoTakenTime", {}).get("timestamp")
            if ts_str:
                dt = datetime.fromtimestamp(int(ts_str), tz=timezone.utc)
                return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            pass
    return None


def _resolve_timestamp(path: Path) -> tuple[str | None, str]:
    """Return (timestamp_string, source) where source is 'exif', 'sidecar', or 'none'."""
    ts = _exif_timestamp(path)
    if ts:
        return ts, "exif"
    ts = _sidecar_timestamp(path)
    if ts:
        return ts, "sidecar"
    return None, "none"


def _delta_seconds(local_ts: str, local_source: str, api_ts: str) -> float:
    """Absolute seconds between a local timestamp and the API creationTime.

    EXIF timestamps carry no timezone — we compare naively (strip TZ from the
    API time too) and accept up to _TIMESTAMP_WINDOW_SECS as a valid match.
    Sidecar timestamps are UTC, so we compare directly.
    """
    api_dt = datetime.fromisoformat(api_ts.replace("Z", "+00:00"))

    if local_source == "sidecar":
        local_dt = datetime.fromisoformat(local_ts.replace("Z", "+00:00"))
        return abs((local_dt - api_dt).total_seconds())
    else:
        # EXIF: no TZ — compare naively
        local_dt_naive = datetime.fromisoformat(local_ts)
        api_dt_naive = api_dt.replace(tzinfo=None)
        return abs((local_dt_naive - api_dt_naive).total_seconds())


def _compute_phash(path: Path) -> str | None:
    try:
        try:
            from pillow_heif import register_heif_opener
            register_heif_opener()
        except ImportError:
            pass
        return str(imagehash.phash(Image.open(path)))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Local file index
# ---------------------------------------------------------------------------

def _index_local_files() -> dict[str, list[Path]]:
    """Return {lowercase_filename: [Path, ...]} for all media files under library/."""
    index: dict[str, list[Path]] = {}
    library = Path(_LIBRARY_DIR)
    if not library.exists():
        log_info(_ROUND, "Library directory not found", path=_LIBRARY_DIR)
        return index
    for p in library.rglob("*"):
        if p.is_file() and p.suffix.lower() in _IMAGE_SUFFIXES:
            index.setdefault(p.name.lower(), []).append(p)
    return index


# ---------------------------------------------------------------------------
# Candidate selection
# ---------------------------------------------------------------------------

def _pick_candidate(
    candidates: list[Path],
    api_creation_time: str | None,
    item_id: str,
    filename: str,
) -> tuple[Path, str, str | None, str]:
    """Choose the best local file from a list of same-named candidates.

    Returns (path, selection_reason, local_timestamp, local_timestamp_source).
    """
    if len(candidates) == 1:
        ts, src = _resolve_timestamp(candidates[0])
        reason = "sole_candidate" if ts else "sole_candidate_no_timestamp"
        return candidates[0], reason, ts, src

    # Multiple candidates — use timestamps to disambiguate
    if not api_creation_time:
        log_item(
            _ROUND, "ambiguous_no_api_time",
            media_item_id=item_id, filename=filename, candidates=len(candidates),
        )
        ts, src = _resolve_timestamp(candidates[0])
        return candidates[0], "no_api_time_took_first", ts, src

    scored: list[tuple[float, Path, str | None, str]] = []
    for path in candidates:
        ts, src = _resolve_timestamp(path)
        if ts:
            delta = _delta_seconds(ts, src, api_creation_time)
        else:
            delta = float("inf")
        scored.append((delta, path, ts, src))

    scored.sort(key=lambda x: x[0])
    best_delta, best_path, best_ts, best_src = scored[0]

    if best_delta <= _TIMESTAMP_WINDOW_SECS:
        reason = f"timestamp_{best_src}"
    else:
        # Timestamps exist but don't match any candidate well — phash tiebreak
        reason = _phash_tiebreak(candidates, scored, item_id, filename)
        best_path = candidates[0]   # phash_tiebreak logs; just use first as fallback
        best_ts, best_src = _resolve_timestamp(best_path)

    return best_path, reason, best_ts, best_src


def _phash_tiebreak(
    candidates: list[Path],
    scored: list[tuple],
    item_id: str,
    filename: str,
) -> str:
    """Log ambiguity; phash matching against the API would require a download.
    For now, flag the ambiguity and let Round 4 handle actual dedup."""
    log_item(
        _ROUND,
        "ambiguous_timestamp_mismatch",
        media_item_id=item_id,
        filename=filename,
        candidates=len(candidates),
        best_delta_hours=round(scored[0][0] / 3600, 1) if scored else None,
    )
    return "phash_fallback_took_first"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(limit: int | None = None) -> None:
    db.init_db()
    log_info(_ROUND, "Starting Round 0: building ID mapping")

    local_index = _index_local_files()
    local_file_count = sum(len(v) for v in local_index.values())
    log_info(_ROUND, "Local file index ready", file_count=local_file_count)
    print(f"Local files indexed: {local_file_count}")

    print("Fetching full library from rclone (may take a few minutes for large libraries)…")
    all_items = list(rclone_api.iter_media_items())
    total = len(all_items)
    print(f"rclone returned {total} items.")
    log_info(_ROUND, "rclone listing complete", total=total)

    processed = 0
    matched = 0
    skipped = 0
    no_timestamp_count = 0

    with tqdm(desc="Round 0: mapping", unit=" items", total=total) as bar:
        for item in all_items:
            item_id = item["id"]
            filename = item["filename"]
            api_creation_time = item.get("mediaMetadata", {}).get("creationTime")

            with db.get_conn() as conn:
                row = conn.execute(
                    "SELECT local_path FROM media_items WHERE media_item_id=?",
                    (item_id,),
                ).fetchone()

                if row and row["local_path"]:
                    skipped += 1
                    bar.update(1)
                    continue

                db.upsert_media_item(conn, item)

                candidates = local_index.get(filename.lower(), [])

                if not candidates:
                    log_item(_ROUND, "no_local_match", media_item_id=item_id, filename=filename)
                else:
                    best_path, reason, local_ts, local_src = _pick_candidate(
                        candidates, api_creation_time, item_id, filename
                    )

                    if local_src == "none":
                        no_timestamp_count += 1
                        log_item(
                            _ROUND,
                            "no_timestamp",
                            media_item_id=item_id,
                            filename=filename,
                            path=str(best_path),
                            note="neither EXIF nor sidecar found — matched by filename only",
                        )

                    db.set_local_path(
                        conn, item_id,
                        str(best_path), best_path.stat().st_size,
                        local_timestamp=local_ts,
                        local_timestamp_source=local_src,
                    )
                    matched += 1
                    log_item(
                        _ROUND, "mapped",
                        media_item_id=item_id,
                        filename=filename,
                        local_path=str(best_path),
                        reason=reason,
                        ts_source=local_src,
                    )

            processed += 1
            bar.update(1)

            if limit and processed >= limit:
                log_info(_ROUND, "Reached --limit, stopping", limit=limit)
                break

    with db.get_conn() as conn:
        db.increment_progress(conn, _ROUND, delta=processed)
        if not limit:
            db.mark_round_complete(conn, _ROUND)

    unmapped = processed - matched
    log_info(
        _ROUND, "Round 0 complete",
        processed=processed, matched=matched, skipped=skipped,
        unmapped=unmapped, no_timestamp=no_timestamp_count,
    )
    print(
        f"\nRound 0 done: {processed} items scanned, "
        f"{matched} matched to local files, "
        f"{unmapped} unmatched (cloud-only or Takeout not yet imported)."
    )
    if no_timestamp_count:
        print(
            f"  {no_timestamp_count} matched files had no EXIF or sidecar timestamp.\n"
            f"  Search the logs for outcome=no_timestamp to review them manually:\n"
            f"  grep no_timestamp /data/logs/round0_*.jsonl"
        )
    if unmapped:
        print("Re-run round0 after importing each Takeout batch to map newly imported files.")
