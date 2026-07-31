"""Fill EXIF gaps left after `reorganize` so Lightroom shows correct capture
dates and map pins without needing the Takeout JSON sidecars present.

This is a gap-fill pass, not a correction pass: `reorganize` already trusts
EXIF DateTimeOriginal over the sidecar whenever EXIF has it (see
reorganize.py's module docstring for why), so existing EXIF values are never
touched here — only files where EXIF is missing something get written to:
  - DateTimeOriginal, when absent, using the same date/time reorganize
    resolved (EXIF if present already skips this file entirely; otherwise the
    sidecar's photoTakenTime converted to the resolved timezone).
  - GPS tags, when EXIF has none AND the sidecar has non-zero geoData/
    geoDataExif (measured live: only ~36% of a sampled batch had EXIF GPS
    already, so this is a real, common gap, not a rare edge case).

Per-file exiftool invocation is far too slow at this library's scale (~180k
files), so this builds one CSV of only the files needing a write, then applies
it in a single `exiftool -csv=... -overwrite_original_in_place` pass — an
order of magnitude fewer process spawns than one exiftool call per file.

Dry-run by default (CLAUDE.md rule 3): with dry_run=True (the default), only
the CSV is written to DATA_DIR/logs/exif_backfill.csv for inspection; no image
file is touched. Requires the `exiftool` binary (`libimage-exiftool-perl` on
Debian, `brew install exiftool` on macOS) — not needed for the dry-run CSV
build, only for --no-dry-run.

Run:
    docker compose run cli exif-backfill --limit=200   # dry run, small sample
    docker compose run cli exif-backfill               # dry run, full CSV
    docker compose run cli exif-backfill --no-dry-run  # apply via exiftool
"""
import csv
import logging
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from tqdm import tqdm

from . import db, sidecars
from .logger import log_info, log_item, log_error

# See round0.py / reorganize.py for why this is silenced.
logging.getLogger("exifread").setLevel(logging.ERROR)

_ROUND = "exif_backfill"
_DATA_DIR = os.environ.get("DATA_DIR", "/data")
_LIBRARY_DIR = Path(_DATA_DIR) / "library"
_LOGS_DIR = Path(_DATA_DIR) / "logs"
_ACCOUNT_TZ = ZoneInfo(os.environ.get("ACCOUNT_TIMEZONE", "Europe/Amsterdam"))

_EXIF_WRITABLE_SUFFIXES = {".jpg", ".jpeg", ".tif", ".tiff", ".png", ".heic", ".heif"}

_tf = None


def _timezone_finder():
    global _tf
    if _tf is None:
        from timezonefinder import TimezoneFinder
        _tf = TimezoneFinder()
    return _tf


def _has_exif_datetime_original(path: Path) -> bool:
    try:
        import exifread
        with open(path, "rb") as f:
            tags = exifread.process_file(f, stop_tag="EXIF DateTimeOriginal", details=False)
        if tags.get("EXIF DateTimeOriginal") or tags.get("Image DateTime"):
            return True
    except Exception:
        pass
    try:
        from PIL import Image
        from PIL.ExifTags import TAGS
        img = Image.open(path)
        exif = getattr(img, "_getexif", lambda: None)()
        if exif:
            for tag_id, value in exif.items():
                if TAGS.get(tag_id) == "DateTimeOriginal":
                    return True
    except Exception:
        pass
    return False


def _has_exif_gps(path: Path) -> bool:
    try:
        import exifread
        with open(path, "rb") as f:
            tags = exifread.process_file(f, details=False)
        if any(str(k).startswith("GPS ") for k in tags):
            return True
    except Exception:
        pass
    try:
        from PIL import Image
        from PIL.ExifTags import TAGS
        img = Image.open(path)
        exif = getattr(img, "_getexif", lambda: None)()
        if exif:
            for tag_id in exif:
                if TAGS.get(tag_id) == "GPSInfo":
                    return True
    except Exception:
        pass
    return False


def _sidecar_datetime_and_geo(path: Path) -> tuple[datetime | None, tuple[float, float] | None]:
    sidecar_path = sidecars.find_sidecar(path)
    if not sidecar_path:
        return None, None
    data = sidecars.read_sidecar_json(sidecar_path)
    if not data:
        return None, None
    dt = None
    ts = data.get("photoTakenTime", {}).get("timestamp")
    if ts:
        utc_dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
        geo = None
        for key in ("geoData", "geoDataExif"):
            g = data.get(key) or {}
            lat, lon = g.get("latitude"), g.get("longitude")
            if lat and lon and (lat != 0 or lon != 0):
                geo = (lat, lon)
                break
        tz = _ACCOUNT_TZ
        if geo:
            tzname = _timezone_finder().timezone_at(lat=geo[0], lng=geo[1])
            if tzname:
                tz = ZoneInfo(tzname)
        dt = utc_dt.astimezone(tz).replace(tzinfo=None)
    else:
        geo = None
    return dt, geo


def _iter_candidate_files() -> list[Path]:
    if not _LIBRARY_DIR.exists():
        return []
    return [
        p for p in _LIBRARY_DIR.rglob("*")
        if p.is_file() and p.suffix.lower() in _EXIF_WRITABLE_SUFFIXES
    ]


def build_csv(limit: int | None = None) -> tuple[Path, int]:
    """Scan the library for gap-fill candidates and write exif_backfill.csv.
    Returns (csv_path, row_count). Never touches any image file."""
    _LOGS_DIR.mkdir(parents=True, exist_ok=True)
    csv_path = _LOGS_DIR / "exif_backfill.csv"

    files = _iter_candidate_files()
    log_info(_ROUND, "Candidate scan complete", file_count=len(files))

    rows = []
    checked = 0
    with tqdm(desc="exif-backfill scan", unit=" files", total=len(files)) as bar:
        for path in files:
            bar.update(1)
            needs_dt = not _has_exif_datetime_original(path)
            needs_gps = not _has_exif_gps(path)
            if not needs_dt and not needs_gps:
                continue

            dt, geo = _sidecar_datetime_and_geo(path)
            row = {"SourceFile": str(path)}
            wrote_anything = False

            if needs_dt and dt:
                row["DateTimeOriginal"] = dt.strftime("%Y:%m:%d %H:%M:%S")
                wrote_anything = True
            if needs_gps and geo:
                lat, lon = geo
                row["GPSLatitude"] = abs(lat)
                row["GPSLatitudeRef"] = "N" if lat >= 0 else "S"
                row["GPSLongitude"] = abs(lon)
                row["GPSLongitudeRef"] = "E" if lon >= 0 else "W"
                wrote_anything = True

            if wrote_anything:
                rows.append(row)
                log_item(_ROUND, "candidate", path=str(path), **{k: v for k, v in row.items() if k != "SourceFile"})

            checked += 1
            if limit and checked >= limit:
                log_info(_ROUND, "Reached --limit, stopping scan", limit=limit)
                break

    fieldnames = ["SourceFile", "DateTimeOriginal", "GPSLatitude", "GPSLatitudeRef", "GPSLongitude", "GPSLongitudeRef"]
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    return csv_path, len(rows)


_LOCK_PATH = _LOGS_DIR / ".exif_backfill.lock"


class _AlreadyRunningError(RuntimeError):
    pass


def _acquire_lock() -> None:
    """Same rationale as reorganize.py's lock: refuse to start a second
    concurrent apply pass. Only guards --no-dry-run, since the dry-run CSV
    build touches no image file and is safe to run alongside anything."""
    _LOGS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(_LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        holder = "unknown"
        try:
            holder = _LOCK_PATH.read_text().strip()
        except OSError:
            pass
        raise _AlreadyRunningError(
            f"Another exif-backfill run appears to be in progress (lock held by: {holder}). "
            f"If that run has actually exited, remove {_LOCK_PATH} by hand and try again."
        )
    with os.fdopen(fd, "w") as f:
        from datetime import datetime as _dt
        f.write(f"pid={os.getpid()} started={_dt.now(timezone.utc).isoformat()}\n")


def _release_lock() -> None:
    try:
        _LOCK_PATH.unlink()
    except OSError:
        pass


def run(dry_run: bool = True, limit: int | None = None) -> None:
    if not dry_run:
        _acquire_lock()
    try:
        _run(dry_run=dry_run, limit=limit)
    finally:
        if not dry_run:
            _release_lock()


def _run(dry_run: bool, limit: int | None) -> None:
    db.init_db()
    log_info(_ROUND, "Starting exif-backfill", dry_run=dry_run)

    csv_path, row_count = build_csv(limit=limit)
    print(f"\nexif-backfill: {row_count} file(s) need a gap filled.")
    print(f"CSV written to: {csv_path}")

    if row_count == 0:
        return

    if dry_run:
        print("\nInspect the CSV, then re-run with --no-dry-run to apply it via exiftool.")
        return

    if not shutil.which("exiftool"):
        print(
            "\nError: exiftool not found on PATH. Install it "
            "(apt: libimage-exiftool-perl, brew: exiftool) and re-run with --no-dry-run."
        )
        return

    # exiftool's -csv=FILE mode only supplies tag *values*; it still needs the
    # target files as explicit arguments to know what to process, matched
    # against the CSV's SourceFile column. Pass them via a -@ argfile (one
    # path per line) rather than argv, both to avoid "argument list too long"
    # at this file count and to sidestep shell-quoting issues with the
    # spaces/special characters present in this library's folder names.
    # Re-read the CSV rather than reusing build_csv()'s in-memory list so any
    # manual edits made to the CSV between the dry-run and this apply step
    # are respected.
    filelist_path = _LOGS_DIR / "exif_backfill_files.txt"
    with open(csv_path) as f, open(filelist_path, "w") as out:
        for row in csv.DictReader(f):
            out.write(row["SourceFile"] + "\n")

    print("\nApplying via exiftool (-overwrite_original_in_place, no backup copies kept)...")
    result = subprocess.run(
        ["exiftool", f"-csv={csv_path}", "-overwrite_original_in_place", "-@", str(filelist_path)],
        text=True, capture_output=True,
    )
    print(result.stdout)
    if result.returncode != 0:
        log_error(_ROUND, "exiftool_failed", stderr=result.stderr, returncode=result.returncode)
        print(result.stderr)
    else:
        log_info(_ROUND, "exif-backfill applied", rows=row_count)
        with db.get_conn() as conn:
            db.mark_round_complete(conn, _ROUND)
        print(f"\nDone: applied EXIF gap-fill to {row_count} file(s).")
