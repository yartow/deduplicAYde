"""Stage detected items into Google Photos albums via the API.

Albums are created on first use and their IDs are persisted in state.db.
"""
from . import auth, db, photos_api
from .logger import log_info, log_item

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


def stage_items(
    creds,
    purpose: str,
    album_id: str,
    media_item_ids: list[str],
    dry_run: bool = True,
) -> int:
    """Add items to the given album. Returns count staged."""
    if not media_item_ids:
        return 0

    if dry_run:
        log_info(
            "staging",
            f"[DRY-RUN] Would stage {len(media_item_ids)} items into {purpose} album",
            album_id=album_id,
        )
        for mid in media_item_ids:
            log_item("staging", "would_stage", media_item_id=mid, purpose=purpose)
        return len(media_item_ids)

    photos_api.batch_add_to_album(creds, album_id, media_item_ids)

    with db.get_conn() as conn:
        for mid in media_item_ids:
            db.set_staged(conn, mid, album_id)
            log_item("staging", "staged", media_item_id=mid, purpose=purpose, album_id=album_id)

    return len(media_item_ids)
