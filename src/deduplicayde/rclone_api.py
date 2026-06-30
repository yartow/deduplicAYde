"""rclone-based Google Photos library enumeration.

rclone uses its own pre-registered OAuth client credentials (registered before
the March 2024 Photos Library API deprecation), so it can call mediaItems.list
even for users whose own Cloud projects cannot.

Requires rclone to be installed and a Google Photos remote named 'gphotos'
configured in /secrets/rclone.conf.  Run:
    docker compose run --rm -p 53682:53682 cli rclone-setup
"""
import json
import os
import subprocess
from typing import Iterator

_CONFIG = os.path.join(os.environ.get("SECRETS_DIR", "/secrets"), "rclone.conf")
_REMOTE = "gphotos:media/all"
_TIMEOUT_SECS = 1800  # 30 min ceiling for very large libraries


def iter_media_items() -> Iterator[dict]:
    """Yield API-compatible item dicts for every photo/video in the library.

    Each yielded dict has the same shape as a Google Photos mediaItems.list
    response item: {id, filename, mimeType, mediaMetadata: {creationTime}}.
    The 'id' value IS the Google Photos mediaItemId and can be used directly
    with album staging calls.
    """
    result = subprocess.run(
        [
            "rclone", "lsjson",
            "--config", _CONFIG,
            "--recursive",
            _REMOTE,
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=_TIMEOUT_SECS,
    )
    for ritem in json.loads(result.stdout):
        yield {
            "id": ritem["ID"],
            "filename": ritem["Name"],
            "mimeType": ritem.get("MimeType", ""),
            "mediaMetadata": {"creationTime": ritem["ModTime"]},
        }


def list_all_media_item_ids() -> set[str]:
    """Return the set of all current mediaItemIds (for Round 3 reconciliation)."""
    return {item["id"] for item in iter_media_items()}
