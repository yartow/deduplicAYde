"""Round 0: Catalog local files.

Google's March 2024 Photos Library API policy change restricts our OAuth scope
to items the app itself uploaded, so there is no way to enumerate the existing
cloud library via the API anymore (see CLAUDE.md). Round 0 is therefore local-only:
it walks DATA_DIR/library/, resolves each file's true capture timestamp, and
records it in state.db. Identity is the local file path — later rounds locate
the corresponding cloud item directly via Playwright when they need to.

Timestamp resolution (in priority order):
  1. EXIF DateTimeOriginal  (via exifread, Pillow as fallback)
  2. photoTakenTime in the Takeout JSON sidecar  (*.json next to the file)
  Filesystem mtime/ctime are NEVER used — Takeout extraction corrupts them.
  Files with neither source are logged as `no_timestamp` for manual review.

Run:
    docker compose run cli round0
    docker compose run cli round0 --limit=200   # small smoke test
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from tqdm import tqdm

from . import db
from .logger import log_info, log_item

_LIBRARY_DIR = os.path.join(os.environ.get("DATA_DIR", "/data"), "library")
_ROUND = "round0"

_IMAGE_SUFFIXES = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif",
    ".tif", ".tiff", ".bmp", ".mp4", ".mov", ".avi", ".mkv",
}


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
        from PIL import Image
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


def _read_sidecar_ts(sidecar: Path) -> str | None:
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


_SIDECAR_TITLE_INDEX: dict[Path, dict[str, Path]] = {}


def _sidecar_dir_index(directory: Path) -> dict[str, Path]:
    """Map {lowercase original filename: sidecar path} for one directory.

    Takeout truncates ".supplemental-metadata.json" when the combined path
    would exceed its length limit (e.g. "...jpg.supplemental-metada.json"),
    so filename-guessing misses those. Every sidecar's JSON body still carries
    the untruncated original filename in "title", so scan once per directory
    and index by that instead.
    """
    if directory in _SIDECAR_TITLE_INDEX:
        return _SIDECAR_TITLE_INDEX[directory]
    index: dict[str, Path] = {}
    for jf in directory.glob("*.json"):
        try:
            with open(jf) as f:
                data = json.load(f)
            title = data.get("title")
            if title:
                index[title.lower()] = jf
        except Exception:
            continue
    _SIDECAR_TITLE_INDEX[directory] = index
    return index


def _sidecar_timestamp(path: Path) -> str | None:
    """Return photoTakenTime from a Takeout JSON sidecar as ISO UTC, or None.

    Takeout places sidecar files alongside the media file, using either
    "photo.jpg.json" or (for long filenames) "photo.json" — or, when the
    ".supplemental-metadata.json" suffix itself is too long, a truncated
    name that has to be matched via the sidecar's internal "title" field.
    """
    for sidecar in (path.with_name(path.name + ".json"), path.with_suffix(".json")):
        if sidecar.exists():
            ts = _read_sidecar_ts(sidecar)
            if ts:
                return ts

    sidecar = _sidecar_dir_index(path.parent).get(path.name.lower())
    if sidecar:
        return _read_sidecar_ts(sidecar)

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


# ---------------------------------------------------------------------------
# Local file index
# ---------------------------------------------------------------------------

def _iter_local_files() -> list[Path]:
    library = Path(_LIBRARY_DIR)
    if not library.exists():
        log_info(_ROUND, "Library directory not found", path=_LIBRARY_DIR)
        return []
    return [
        p for p in library.rglob("*")
        if p.is_file() and p.suffix.lower() in _IMAGE_SUFFIXES
    ]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(limit: int | None = None) -> None:
    db.init_db()
    log_info(_ROUND, "Starting Round 0: cataloging local files")

    files = _iter_local_files()
    total = len(files)
    log_info(_ROUND, "Local file scan complete", file_count=total)
    print(f"Local files found: {total}")

    with db.get_conn() as conn:
        already_cataloged = {
            r["local_path"] for r in conn.execute("SELECT local_path FROM media_items")
        }

    processed = 0
    skipped = 0
    no_timestamp_count = 0

    with tqdm(desc="Round 0: cataloging", unit=" files", total=total) as bar:
        for path in files:
            if str(path) in already_cataloged:
                skipped += 1
                bar.update(1)
                continue

            local_ts, local_src = _resolve_timestamp(path)

            if local_src == "none":
                no_timestamp_count += 1
                log_item(
                    _ROUND,
                    "no_timestamp",
                    filename=path.name,
                    path=str(path),
                    note="neither EXIF nor sidecar found",
                )

            with db.get_conn() as conn:
                db.upsert_local_item(
                    conn,
                    local_path=str(path),
                    filename=path.name,
                    file_size=path.stat().st_size,
                    local_timestamp=local_ts,
                    local_timestamp_source=local_src,
                )

            log_item(
                _ROUND, "cataloged",
                filename=path.name,
                local_path=str(path),
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

    log_info(
        _ROUND, "Round 0 complete",
        processed=processed, skipped=skipped, no_timestamp=no_timestamp_count,
    )
    print(f"\nRound 0 done: {processed} local files cataloged, {skipped} already up to date.")
    if no_timestamp_count:
        print(
            f"  {no_timestamp_count} files had no EXIF or sidecar timestamp.\n"
            f"  Search the logs for outcome=no_timestamp to review them manually:\n"
            f"  grep no_timestamp /data/logs/round0_*.jsonl"
        )
    if limit and processed >= limit:
        print("Re-run round0 without --limit (or with a higher one) to catalog the rest.")
