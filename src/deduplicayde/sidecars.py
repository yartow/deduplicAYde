"""Shared Takeout JSON sidecar lookup.

Extracted from round0.py so every consumer that needs to find or read a
Takeout sidecar — round0's cataloging, the `reorganize` command, and
`exif_backfill` — shares one implementation and can't drift out of sync.
See round0.py's module docstring for the full rationale on sidecar
timestamp handling (ACCOUNT_TIMEZONE, naive local storage).
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

_ACCOUNT_TZ = ZoneInfo(os.environ.get("ACCOUNT_TIMEZONE", "Europe/Amsterdam"))

_SIDECAR_TITLE_INDEX: dict[Path, dict[str, Path]] = {}


def sidecar_dir_index(directory: Path) -> dict[str, Path]:
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


def invalidate_dir_index(directory: Path) -> None:
    """Drop the cached title index for one directory.

    Callers that rewrite a sidecar's "title" field (reorganize.py, after a
    collision rename) or move files in/out of a directory must call this so
    a later lookup in the same process doesn't serve a stale mapping.
    """
    _SIDECAR_TITLE_INDEX.pop(directory, None)


def read_sidecar_ts(sidecar: Path) -> str | None:
    """Return photoTakenTime converted to ACCOUNT_TZ, naive (no "Z"/offset) —
    matching EXIF's format so every local_timestamp is directly comparable to
    what Google Photos displays, regardless of source."""
    try:
        with open(sidecar) as f:
            data = json.load(f)
        ts_str = data.get("photoTakenTime", {}).get("timestamp")
        if ts_str:
            dt = datetime.fromtimestamp(int(ts_str), tz=timezone.utc).astimezone(_ACCOUNT_TZ)
            return dt.strftime("%Y-%m-%dT%H:%M:%S")
    except Exception:
        pass
    return None


def read_sidecar_json(sidecar: Path) -> dict | None:
    """Return the parsed JSON body of a sidecar file, or None."""
    try:
        with open(sidecar) as f:
            return json.load(f)
    except Exception:
        return None


def find_sidecar(path: Path) -> Path | None:
    """Locate path's Takeout JSON sidecar (by file existence only), or None.

    Same precedence round0 has always used: direct "<name>.json" candidates
    first, then the per-directory "title" index for truncated suffixes,
    then the "-edited" original-file fallback (Takeout writes no sidecar for
    in-app edits — the edit doesn't change capture time or geo, so fall back
    to the original's sidecar).

    Unlike round0's own _sidecar_timestamp, this does not require the found
    sidecar to actually contain a usable photoTakenTime — callers here also
    want geoData and the "title" field regardless of timestamp validity.
    Measured live: only ~1% of sidecars lack photoTakenTime, so this doesn't
    change which sidecar file gets matched in practice.
    """
    for sidecar in (path.with_name(path.name + ".json"), path.with_suffix(".json")):
        if sidecar.exists():
            return sidecar

    sidecar = sidecar_dir_index(path.parent).get(path.name.lower())
    if sidecar:
        return sidecar

    if path.stem.endswith("-edited"):
        original = path.with_name(path.stem[: -len("-edited")] + path.suffix)
        if original != path:
            return find_sidecar(original)

    return None
