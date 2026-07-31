"""Reorganize DATA_DIR/library into one flat folder per correct local capture
date ("yyyy-mm-dd"), replacing the nested Takeout export structure (album
folders, "Photos from YYYY" folders, and inconsistently-named date folders).

Why EXIF wins over the sidecar when both exist (this is the crux of the whole
module, verified live against this exact library before writing any code):
sampling 191 files that had both EXIF DateTimeOriginal and a sidecar
photoTakenTime, converting the latter's UTC epoch to Europe/Amsterdam and
comparing against EXIF showed a 22% mismatch rate, split into two distinct
causes:
  - ~10%: genuine travel photos, EXIF is correctly foreign-camera-local time
    and the naive UTC->Amsterdam conversion is simply wrong for that day.
  - ~12%: Google itself stored the camera's *local* wall-clock time in
    photoTakenTime and labeled it UTC (confirmed: the raw epoch value, taken
    as a naive local timestamp, equals EXIF exactly). Converting UTC->local
    on these applies the timezone offset a SECOND time, shifting the photo by
    1-2 hours and sometimes across midnight onto the wrong calendar day. This
    is very likely what misfiled these particular photos in the first place,
    so blindly trusting the sidecar would re-inflict the same bug.
EXIF DateTimeOriginal has no timezone info and is always already local capture
time (same assumption round0.py makes), so it's authoritative whenever present
(~78% of JPGs). The sidecar is used only as a fallback, with its geo data (if
present and non-zero) resolved to a real timezone via timezonefinder, falling
back to ACCOUNT_TIMEZONE otherwise (most files have no geo — measured ~28%
non-zero geo in this library).

Other things measured live in this library that shaped this implementation:
  - Sidecar filename suffixes are truncated up to 10 different ways
    ("...supplemental-metadata.json" down to "...supplemental.json") — see
    sidecars.py, shared with round0.py, which already solves this via a
    per-directory index keyed on the sidecar's internal "title" field.
  - 58,245 JPGs share a basename with another JPG elsewhere in the library,
    and hashing 60 collision groups found ZERO byte-identical pairs — these
    are Takeout's full-size original plus a downsized copy in an album
    folder, or genuine filename reuse across different cameras/dates. Both
    cases are handled the same way: suffix-rename on collision, never
    overwrite, never delete.
  - "Motion Photo" pairs (Pixel camera): "NAME.MP.jpg" (the still) ships next
    to a bare "NAME.MP" (the embedded video) and sometimes a zero-byte
    "NAME.MP.COVER" marker — both share the jpg's own Path.stem (".MP" is
    baked into the jpg's stem, not a separate extension) and must move
    together with it.
  - state.db's 179,921 media_items rows are keyed on local_path; every move
    repoints the matching row via db.update_local_path in the same step, so
    the 66k+ labels and staged rows already recorded survive the move.

Dry-run by default (CLAUDE.md rule 3): with dry_run=True (the default),
nothing is moved, nothing is deleted, and the DB is not touched — only the
manifest/report/journal are written, so you can review exactly what would
happen first.

Run:
    docker compose run cli reorganize --limit=500      # dry run, small sample
    docker compose run cli reorganize                  # dry run, full library
    docker compose run cli reorganize --no-dry-run      # actually move files
"""
import csv
import json
import logging
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from tqdm import tqdm

from . import db, sidecars
from .logger import log_info, log_item, log_error

# See round0.py for why: exifread logs "File format not recognized." /
# "<X> file does not have exif data." at WARNING for every file it can't
# parse (non-JPEG/TIFF/HEIC/PNG-with-exif, videos, etc.) — expected for a
# good fraction of this library, and already handled ourselves via the
# except/return-None fallback below, so silence the noise.
logging.getLogger("exifread").setLevel(logging.ERROR)

_ROUND = "reorganize"
_DATA_DIR = os.environ.get("DATA_DIR", "/data")
_LIBRARY_DIR = Path(_DATA_DIR) / "library"
_LOGS_DIR = Path(_DATA_DIR) / "logs"
_NO_TIMESTAMP_DIR = _LIBRARY_DIR / "_no_timestamp"
_ACCOUNT_TZ = ZoneInfo(os.environ.get("ACCOUNT_TIMEZONE", "Europe/Amsterdam"))

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Every file type Lightroom can import that's actually present in this
# library (measured live), plus video. Sidecars (.json) and Motion Photo
# companions (.MP/.cover) are never processed as top-level entries — they
# always travel alongside a media file found here, via _companions().
_MEDIA_SUFFIXES = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif",
    ".tif", ".tiff", ".bmp", ".dng", ".cr2",
    ".mp4", ".mov", ".avi", ".mkv", ".3gp", ".3gpp",
}

_tf = None  # lazy timezonefinder.TimezoneFinder() — construction reads a
            # multi-MB shape index, so build it once only if actually needed.


def _timezone_finder():
    global _tf
    if _tf is None:
        from timezonefinder import TimezoneFinder
        _tf = TimezoneFinder()
    return _tf


def _exif_datetime_original(path: Path) -> datetime | None:
    """Same lookup as round0._exif_timestamp, returned as a datetime (not a
    string) since this module needs .strftime("%Y-%m-%d") for foldering."""
    try:
        import exifread
        with open(path, "rb") as f:
            tags = exifread.process_file(f, stop_tag="EXIF DateTimeOriginal", details=False)
        raw = tags.get("EXIF DateTimeOriginal") or tags.get("Image DateTime")
        if raw:
            return datetime.strptime(str(raw).strip(), "%Y:%m:%d %H:%M:%S")
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
                    return datetime.strptime(str(value).strip(), "%Y:%m:%d %H:%M:%S")
    except Exception:
        pass
    return None


def _geo_from_sidecar(data: dict) -> tuple[float, float] | None:
    """Prefer geoData (Google's own resolved location) over geoDataExif,
    matching the sidecar's own field order; either may be present with (0, 0)
    when there's genuinely no location."""
    for key in ("geoData", "geoDataExif"):
        g = data.get(key) or {}
        lat, lon = g.get("latitude"), g.get("longitude")
        if lat and lon and (lat != 0 or lon != 0):
            return lat, lon
    return None


def _resolve_date(path: Path) -> tuple[str | None, str]:
    """Return (yyyy-mm-dd, source) where source is 'exif', 'sidecar', or
    'none'. See module docstring for why EXIF is authoritative when present."""
    exif_dt = _exif_datetime_original(path)
    if exif_dt:
        return exif_dt.strftime("%Y-%m-%d"), "exif"

    sidecar_path = sidecars.find_sidecar(path)
    if sidecar_path:
        data = sidecars.read_sidecar_json(sidecar_path)
        if data:
            ts = data.get("photoTakenTime", {}).get("timestamp")
            if ts:
                utc_dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
                geo = _geo_from_sidecar(data)
                if geo:
                    lat, lon = geo
                    tzname = _timezone_finder().timezone_at(lat=lat, lng=lon)
                    tz = ZoneInfo(tzname) if tzname else _ACCOUNT_TZ
                else:
                    tz = _ACCOUNT_TZ
                return utc_dt.astimezone(tz).strftime("%Y-%m-%d"), "sidecar"

    return None, "none"


def _companions(path: Path) -> list[Path]:
    """Sibling files that must move alongside `path`: its Takeout sidecar and
    any Motion Photo companions (see module docstring).

    The sidecar is only taken along if its internal "title" actually matches
    this file's own name. find_sidecar()'s "-edited" fallback can return a
    *different* file's sidecar (Takeout writes no sidecar for in-app edits,
    so "-edited" copies borrow their original's for date/geo purposes) — that
    sidecar belongs with the original and must not be relocated out from
    under it just because the edited copy happened to be processed first.
    """
    out: list[Path] = []
    sidecar = sidecars.find_sidecar(path)
    if sidecar:
        data = sidecars.read_sidecar_json(sidecar) or {}
        if (data.get("title") or "").lower() == path.name.lower():
            out.append(sidecar)

    # List the directory rather than guessing ".COVER" vs ".cover": macOS/
    # APFS (this library's actual filesystem) is case-insensitive by default,
    # so path.with_name(stem + ".COVER") and path.with_name(stem + ".cover")
    # can both report .exists() == True for what is physically the SAME
    # file — treating them as two distinct companions caused a real bug
    # here (os.replace succeeding on the first, then failing "No such file"
    # on the second, since it had already been moved). Matching against the
    # real directory listing, deduped case-insensitively, sidesteps that.
    stem = path.stem  # e.g. "PXL_x.MP" for "PXL_x.MP.jpg"
    stem_lower = stem.lower()
    try:
        siblings = os.listdir(path.parent)
    except OSError:
        siblings = []
    seen_lower = set()
    for name in siblings:
        if name == path.name:
            continue
        name_lower = name.lower()
        if name_lower == stem_lower or name_lower == stem_lower + ".cover":
            if name_lower in seen_lower:
                continue
            seen_lower.add(name_lower)
            out.append(path.parent / name)
    return out


def _unique_path(candidate: Path, reserved: set[str]) -> Path:
    """Append _1, _2, ... before the suffix until the path doesn't collide,
    checking both the real filesystem and `reserved` — paths this run has
    already decided to place somewhere but not yet (or never, in dry-run)
    physically moved. Without the `reserved` check, a dry run can't detect
    two source files landing on the same destination name, since neither
    move has actually happened on disk yet to make the second one collide.
    Never overwrites an existing file. Adds the chosen path to `reserved`."""
    def taken(p: Path) -> bool:
        return p.exists() or str(p) in reserved

    if not taken(candidate):
        reserved.add(str(candidate))
        return candidate
    stem, suffix, parent = candidate.stem, candidate.suffix, candidate.parent
    n = 1
    while True:
        alt = parent / f"{stem}_{n}{suffix}"
        if not taken(alt):
            reserved.add(str(alt))
            return alt
        n += 1


def _rewrite_sidecar_title(sidecar_path: Path, new_title: str) -> None:
    """Update a moved sidecar's internal "title" field to the media file's
    new (possibly collision-suffixed) name. sidecar_dir_index keys on this
    field, so skipping this would silently break sidecar lookup for this file
    on every future round0/reorganize run."""
    try:
        data = sidecars.read_sidecar_json(sidecar_path) or {}
        if data.get("title") == new_title:
            return
        data["title"] = new_title
        with open(sidecar_path, "w") as f:
            json.dump(data, f)
    except Exception as e:
        log_error(_ROUND, "sidecar_title_rewrite_failed", path=str(sidecar_path), error=str(e))


def _album_info(directory: Path) -> tuple[str, str]:
    """(title, date) describing the source folder, from its metadata.json if
    present, else the folder's own name with no date."""
    meta = directory / "metadata.json"
    if meta.exists():
        data = sidecars.read_sidecar_json(meta) or {}
        title = data.get("title") or directory.name
        date_str = ""
        ts = (data.get("date") or {}).get("timestamp")
        if ts:
            try:
                date_str = datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d")
            except Exception:
                pass
        return title, date_str
    return directory.name, ""


def _iter_media_files() -> list[Path]:
    if not _LIBRARY_DIR.exists():
        log_info(_ROUND, "Library directory not found", path=str(_LIBRARY_DIR))
        return []
    return [
        p for p in _LIBRARY_DIR.rglob("*")
        if p.is_file() and p.suffix.lower() in _MEDIA_SUFFIXES
    ]


def _db_local_path(path: Path) -> str:
    """media_items.local_path is always stored /data/-prefixed (see
    CLAUDE.md tech-stack bullet 1) — this module runs inside the cli
    container where DATA_DIR IS /data, so path strings already match with no
    re-rooting needed."""
    return str(path)


_LOCK_PATH = _LOGS_DIR / ".reorganize.lock"


class _AlreadyRunningError(RuntimeError):
    pass


def _acquire_lock() -> None:
    """Refuse to start a second concurrent run against the same library.

    Confirmed live: a previous run's host-side `docker compose` process
    exited (session ended) while its *container* kept executing detached;
    starting what looked like a fresh run later added a second mover racing
    the first over the same files. os.replace() overwrites its destination
    unconditionally, so two processes resolving the same free destination
    name before either moves can silently destroy one of them — the
    in-process `reserved` set in _unique_path can't protect against another
    process. O_CREAT | O_EXCL is atomic even over the virtiofs bind mount
    this runs on, unlike a check-then-write pattern.
    """
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
            f"Another reorganize run appears to be in progress (lock held by: {holder}). "
            f"If that run has actually exited, remove {_LOCK_PATH} by hand and try again."
        )
    with os.fdopen(fd, "w") as f:
        f.write(f"pid={os.getpid()} started={datetime.now(timezone.utc).isoformat()}\n")


def _release_lock() -> None:
    try:
        _LOCK_PATH.unlink()
    except OSError:
        pass


def _write_manifest_from_log() -> Path:
    """(Re)build albums_manifest.csv from every moved_*/quarantined line in
    this round's jsonl logs, instead of accumulating rows in memory across
    the whole run.

    Building it only at the very end (in memory) meant a stop mid-run — even
    a clean one, e.g. to apply a fix like this lock — lost every row
    recorded so far, with no way to recover which album folders the moved
    files had come from once those folders are later deleted in cleanup.
    The jsonl log already carries src/dest/renamed/date_source for every
    successful move (log_item calls above), across *all* runs/restarts on
    this library, so rebuilding from it instead is both crash-safe and
    strictly more complete.
    """
    manifest_path = _LOGS_DIR / "albums_manifest.csv"
    rows: dict[str, dict] = {}  # keyed on old_path; entries are applied in
                                 # chronological (file-then-line) order, so a
                                 # later real moved_*/quarantined outcome
                                 # naturally supersedes an earlier dry-run
                                 # would_move_*/would_quarantine preview for
                                 # the same source path.
    for log_path in sorted(_LOGS_DIR.glob(f"{_ROUND}_*.jsonl")):
        with open(log_path) as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                outcome = entry.get("outcome", "")
                if outcome in ("quarantined", "would_quarantine"):
                    date_source = "none"
                elif outcome.startswith("moved_"):
                    date_source = outcome.removeprefix("moved_")
                elif outcome.startswith("would_move_"):
                    date_source = outcome.removeprefix("would_move_")
                else:
                    continue
                src = entry.get("src")
                if not src:
                    continue
                album_title, album_date = _album_info(Path(src).parent)
                rows[src] = {
                    "old_path": src,
                    "album_title": album_title,
                    "album_date": album_date,
                    "new_path": entry.get("dest", ""),
                    "renamed": entry.get("renamed", False),
                    "date_source": date_source,
                }

    with open(manifest_path, "w", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=["old_path", "album_title", "album_date", "new_path", "renamed", "date_source"]
        )
        writer.writeheader()
        writer.writerows(rows.values())
    return manifest_path


def run(dry_run: bool = True, limit: int | None = None) -> None:
    # Only the real, file-moving run needs the cross-process lock: a dry run
    # touches nothing on disk or in the DB, so it's safe (and useful) to run
    # concurrently with an in-progress real run to check progress.
    if not dry_run:
        _acquire_lock()
    try:
        _run(dry_run=dry_run, limit=limit)
    finally:
        if not dry_run:
            _release_lock()


def _run(dry_run: bool, limit: int | None) -> None:
    db.init_db()
    log_info(_ROUND, "Starting reorganize: sorting library into date folders", dry_run=dry_run)

    files = _iter_media_files()
    total = len(files)
    log_info(_ROUND, "Media file scan complete", file_count=total)
    print(f"Media files found: {total}")

    _LOGS_DIR.mkdir(parents=True, exist_ok=True)

    counts = {
        "already_organized": 0, "moved_exif": 0, "moved_sidecar": 0,
        "quarantined": 0, "renamed": 0, "errors": 0,
    }
    # Destination paths this run has already claimed but not necessarily
    # moved yet (always true in dry-run; briefly true in a real run between
    # a companion move and its media file's own move). See _unique_path.
    reserved: set[str] = set()
    processed = 0

    # One long-lived connection for the whole run instead of open+commit+
    # close per file (db.get_conn()'s normal per-call pattern): at this
    # library's scale that meant one fsync-ing commit per file over the
    # virtiofs bind mount, a real contributor to the run's slowness. Commit
    # every 500 moves instead (CLAUDE.md rule 7's checkpoint interval) plus
    # once more at the very end.
    db_conn = sqlite3.connect(db.DB_PATH, check_same_thread=False) if not dry_run else None
    if db_conn is not None:
        db_conn.row_factory = sqlite3.Row
        db_conn.execute("PRAGMA foreign_keys=ON")

    with tqdm(desc="reorganize", unit=" files", total=total) as bar:
        for path in files:
            bar.update(1)
            if not path.exists():
                continue  # moved by a prior interrupted run, or a companion of an earlier file

            date_str, source = _resolve_date(path)
            dest_dir = _NO_TIMESTAMP_DIR if source == "none" else _LIBRARY_DIR / date_str

            if path.parent == dest_dir:
                counts["already_organized"] += 1
                log_item(_ROUND, "already_organized", path=str(path))
                continue

            orig_parent = path.parent
            companions = _companions(path)
            dest_media = _unique_path(dest_dir / path.name, reserved)
            renamed = dest_media.name != path.name

            if dry_run:
                outcome = "would_quarantine" if source == "none" else f"would_move_{source}"
                log_item(
                    _ROUND, outcome, src=str(path), dest=str(dest_media),
                    renamed=renamed, companions=[str(c) for c in companions],
                )
            else:
                try:
                    dest_dir.mkdir(parents=True, exist_ok=True)
                    for comp in companions:
                        comp_dest = _unique_path(
                            dest_dir / (comp.name.replace(path.stem, dest_media.stem, 1)
                                        if renamed else comp.name),
                            reserved,
                        )
                        os.replace(comp, comp_dest)
                        if comp.suffix.lower() == ".json":
                            _rewrite_sidecar_title(comp_dest, dest_media.name)
                            # Surgical index update, not invalidate_dir_index:
                            # a full rebuild re-parses every *.json in the
                            # source directory (13k-38k files in this
                            # library's largest folders) on every single
                            # move, which is what actually caused the ETA to
                            # blow up past 130 hours. See sidecars.py.
                            sidecars.drop_from_dir_index(orig_parent, path.name)
                            sidecars.add_to_dir_index(dest_dir, dest_media.name, comp_dest)
                    os.replace(path, dest_media)

                    row = db_conn.execute(
                        "SELECT id FROM media_items WHERE local_path=?",
                        (_db_local_path(path),),
                    ).fetchone()
                    if row:
                        db.update_local_path(db_conn, row["id"], _db_local_path(dest_media))

                    outcome = "quarantined" if source == "none" else f"moved_{source}"
                    log_item(
                        _ROUND, outcome, src=str(path), dest=str(dest_media),
                        renamed=renamed, db_row_updated=bool(row),
                    )
                except OSError as e:
                    log_error(_ROUND, "move_failed", path=str(path), error=str(e))
                    counts["errors"] += 1
                    continue

            if source == "none":
                counts["quarantined"] += 1
            else:
                counts[f"moved_{source}"] += 1
            if renamed:
                counts["renamed"] += 1

            processed += 1
            if db_conn is not None and processed % 500 == 0:
                db_conn.commit()
            if limit and processed >= limit:
                log_info(_ROUND, "Reached --limit, stopping", limit=limit)
                break

    if db_conn is not None:
        db_conn.commit()
        db_conn.close()

    manifest_path = _write_manifest_from_log()

    folders_deleted = 0
    if not dry_run:
        folders_deleted = _cleanup_empty_folders()

    report_lines = [
        f"{'[DRY-RUN] ' if dry_run else ''}reorganize report",
        f"Media files scanned:        {total}",
        f"Already organized (skip):   {counts['already_organized']}",
        f"Moved via EXIF date:        {counts['moved_exif']}",
        f"Moved via sidecar date:     {counts['moved_sidecar']}",
        f"Quarantined (no timestamp): {counts['quarantined']}",
        f"Collision-renamed:          {counts['renamed']}",
        f"Errors:                     {counts['errors']}",
        f"Folders deleted:            {folders_deleted}",
        f"Manifest written to:        {manifest_path}",
    ]
    report_path = _LOGS_DIR / "reorganize_report.txt"
    with open(report_path, "w") as f:
        f.write("\n".join(report_lines) + "\n")

    log_info(_ROUND, "reorganize complete", **counts, folders_deleted=folders_deleted)
    print("\n" + "\n".join(report_lines))
    if dry_run:
        print("\nRe-run with --no-dry-run to actually move files and clean up folders.")


def _cleanup_empty_folders() -> int:
    """Delete every folder under library/ that (a) is not library/ itself,
    (b) is not _no_timestamp, (c) is not a direct child of library/ whose
    name matches yyyy-mm-dd (the canonical destination folders — including
    any that happened to already sit there before this run), and (d) now
    contains nothing but metadata.json/.DS_Store, once all its real content
    has moved elsewhere. This covers the "Takeout"/"Google Photos" wrapper
    folders, every "Photos from YYYY" folder, every album folder, and every
    *nested* date-named folder (e.g. "Takeout/Google Photos/2015-06-20") —
    that nested folder is a source location, not the canonical flat
    destination, so it is cleaned up like any other once drained, even
    though its own name looks like a date.
    """
    deleted = 0
    for dirpath, dirnames, filenames in os.walk(_LIBRARY_DIR, topdown=False):
        d = Path(dirpath)
        if d == _LIBRARY_DIR or d == _NO_TIMESTAMP_DIR:
            continue
        if d.parent == _LIBRARY_DIR and _DATE_RE.match(d.name):
            continue
        # subdirectories are only still present here if they weren't deleted
        # (i.e. not empty) — leave this directory alone too, in that case.
        if any((d / sub).is_dir() for sub in dirnames):
            continue
        leftovers = [f for f in filenames if f not in ("metadata.json", ".DS_Store")]
        if leftovers:
            continue
        try:
            for f in filenames:
                (d / f).unlink()
            d.rmdir()
            log_item(_ROUND, "folder_deleted", path=str(d))
            deleted += 1
        except OSError as e:
            log_error(_ROUND, "folder_delete_failed", path=str(d), error=str(e))
    return deleted
