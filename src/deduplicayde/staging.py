"""Create the Google Photos review albums used by locate_stage.py.

Album creation still works via the API (fresh, app-owned content). Adding
pre-existing items to an album does not (see CLAUDE.md) — that part moved to
locate_stage.py, which drives the album's "Add to album" UI via Playwright.
"""
from . import db, photos_api
from .logger import log_info

_ALBUM_TITLES = {
    "receipt": "deduplicAYde – Receipts",
    "vague": "deduplicAYde – Vague",
}


def get_or_create_albums(creds) -> dict[str, str]:
    """Return {purpose: album_id} for receipts and vague albums."""
    album_ids: dict[str, str] = {}

    with db.get_conn() as conn:
        for purpose, title in _ALBUM_TITLES.items():
            row = conn.execute(
                "SELECT album_id FROM albums WHERE purpose=?", (purpose,)
            ).fetchone()
            if row:
                album_ids[purpose] = row["album_id"]

    for purpose, title in _ALBUM_TITLES.items():
        if purpose not in album_ids:
            log_info("staging", f"Creating album: {title}")
            album = photos_api.get_or_create_album(creds, title)
            album_id = album["id"]
            album_ids[purpose] = album_id
            with db.get_conn() as conn:
                db.get_or_create_album(conn, album_id, title, purpose)
            log_info("staging", f"Album ready", purpose=purpose, album_id=album_id)

    return album_ids
