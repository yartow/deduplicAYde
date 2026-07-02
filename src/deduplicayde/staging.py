"""Create the Google Photos review albums used by locate_stage.py.

Album creation still works via the API (fresh, app-owned content). Adding
pre-existing items to an album does not (see CLAUDE.md) — that part moved to
locate_stage.py, which drives the album's "Add to album" UI via Playwright.

albums.list 403s under this app's restricted scope (see photos_api.py), so
there's no way to ask Google "does an album with this title already exist?".
get_or_create_album is the single source of truth for cross-run idempotency
instead: check the local `albums` table first, only call the API if this
purpose has never been created before. Every caller that needs a
purpose-tagged album (this module, video_ops.py) must go through it rather
than calling photos_api.create_album directly, or repeated runs will create
duplicate albums on Google's side.
"""
from . import db, photos_api
from .logger import log_info

_ALBUM_TITLES = {
    "receipt": "deduplicAYde – Receipts",
    "vague": "deduplicAYde – Vague",
}


def get_or_create_album(creds, purpose: str, title: str) -> str:
    """Return the album_id for purpose, creating it via the API (and recording
    it locally) only if this purpose has never been created before."""
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT album_id FROM albums WHERE purpose=?", (purpose,)
        ).fetchone()
    if row:
        return row["album_id"]

    log_info("staging", f"Creating album: {title}")
    album = photos_api.create_album(creds, title)
    album_id = album["id"]
    with db.get_conn() as conn:
        db.get_or_create_album(conn, album_id, title, purpose)
    log_info("staging", "Album ready", purpose=purpose, album_id=album_id)
    return album_id


def get_or_create_albums(creds) -> dict[str, str]:
    """Return {purpose: album_id} for receipts and vague albums."""
    return {
        purpose: get_or_create_album(creds, purpose, title)
        for purpose, title in _ALBUM_TITLES.items()
    }
