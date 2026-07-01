import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

DB_PATH = os.path.join(os.environ.get("DATA_DIR", "/data"), "state.db")

_SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS media_items (
    media_item_id  TEXT PRIMARY KEY,
    filename       TEXT NOT NULL,
    creation_time  TEXT,
    mime_type      TEXT,
    local_path     TEXT,
    file_size      INTEGER,
    phash          TEXT,
    -- detection results
    blur_score     REAL,
    edge_density   REAL,
    ocr_text_density REAL,
    label          TEXT,    -- receipt | vague | ok | NULL (unprocessed)
    -- staging
    staged_album_id TEXT,
    staged_at       TEXT,
    -- manual review (for vague items)
    review_status   TEXT,   -- pending | confirmed_delete | keep
    reviewed_at     TEXT,
    -- deletion
    deleted_at      TEXT,
    deletion_status TEXT,   -- deleted | failed
    -- local file timestamp (never use mtime/ctime — Takeout corrupts these)
    local_timestamp        TEXT,   -- resolved true timestamp (EXIF or sidecar)
    local_timestamp_source TEXT,   -- exif | sidecar | none
    -- bookkeeping
    scanned_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS albums (
    album_id   TEXT PRIMARY KEY,
    title      TEXT NOT NULL,
    purpose    TEXT NOT NULL,  -- receipts | vague | duplicates
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS duplicate_pairs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    item_a_id        TEXT NOT NULL REFERENCES media_items(media_item_id),
    item_b_id        TEXT NOT NULL REFERENCES media_items(media_item_id),
    hamming_distance INTEGER NOT NULL,
    review_status    TEXT NOT NULL DEFAULT 'pending',  -- pending | keep_a | keep_b | keep_both | skip
    reviewed_at      TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS round_progress (
    round_name      TEXT PRIMARY KEY,
    started_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    completed_at    TEXT,
    items_processed INTEGER NOT NULL DEFAULT 0,
    items_total     INTEGER NOT NULL DEFAULT 0,
    last_page_token TEXT
);
"""


def init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(_SCHEMA)
    _migrate(conn)
    conn.commit()
    conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after the initial schema so existing DBs stay compatible."""
    existing = {r[1] for r in conn.execute("PRAGMA table_info(media_items)").fetchall()}
    for col, col_type in [
        ("local_timestamp", "TEXT"),
        ("local_timestamp_source", "TEXT"),
        ("local_video_purged_at", "TEXT"),
    ]:
        if col not in existing:
            conn.execute(f"ALTER TABLE media_items ADD COLUMN {col} {col_type}")


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def upsert_media_item(conn: sqlite3.Connection, api_item: dict) -> None:
    """Insert or refresh a media item from an API response dict."""
    meta = api_item.get("mediaMetadata", {})
    conn.execute(
        """
        INSERT INTO media_items (media_item_id, filename, creation_time, mime_type, updated_at)
        VALUES (:id, :filename, :creation_time, :mime_type, :now)
        ON CONFLICT(media_item_id) DO UPDATE SET
            filename      = excluded.filename,
            creation_time = excluded.creation_time,
            mime_type     = excluded.mime_type,
            updated_at    = excluded.updated_at
        """,
        {
            "id": api_item["id"],
            "filename": api_item["filename"],
            "creation_time": meta.get("creationTime"),
            "mime_type": api_item.get("mimeType"),
            "now": now_iso(),
        },
    )


def set_local_path(
    conn: sqlite3.Connection,
    media_item_id: str,
    path: str,
    size: int,
    local_timestamp: str | None = None,
    local_timestamp_source: str | None = None,
) -> None:
    conn.execute(
        """UPDATE media_items SET
               local_path=?, file_size=?,
               local_timestamp=?, local_timestamp_source=?,
               updated_at=?
           WHERE media_item_id=?""",
        (path, size, local_timestamp, local_timestamp_source, now_iso(), media_item_id),
    )


def set_detection_result(
    conn: sqlite3.Connection,
    media_item_id: str,
    blur_score: float,
    edge_density: float,
    ocr_text_density: float,
    label: str,
) -> None:
    conn.execute(
        """UPDATE media_items SET
            blur_score=?, edge_density=?, ocr_text_density=?,
            label=?, updated_at=?
           WHERE media_item_id=?""",
        (blur_score, edge_density, ocr_text_density, label, now_iso(), media_item_id),
    )


def set_staged(conn: sqlite3.Connection, media_item_id: str, album_id: str) -> None:
    conn.execute(
        "UPDATE media_items SET staged_album_id=?, staged_at=?, updated_at=? WHERE media_item_id=?",
        (album_id, now_iso(), now_iso(), media_item_id),
    )


def get_catalog_items(conn: sqlite3.Connection) -> list[dict]:
    """Return all media_items as API-shaped dicts for round0 local matching."""
    rows = conn.execute(
        "SELECT media_item_id, filename, creation_time, mime_type FROM media_items"
    ).fetchall()
    return [
        {
            "id": r["media_item_id"],
            "filename": r["filename"],
            "mediaMetadata": {"creationTime": r["creation_time"]},
            "mimeType": r["mime_type"],
        }
        for r in rows
    ]


def set_video_purged(conn: sqlite3.Connection, media_item_id: str) -> None:
    """Null out local_path and record purge time for a locally-deleted video (cloud copy kept)."""
    conn.execute(
        "UPDATE media_items SET local_path=NULL, local_video_purged_at=?, updated_at=? WHERE media_item_id=?",
        (now_iso(), now_iso(), media_item_id),
    )


def set_deleted(conn: sqlite3.Connection, media_item_id: str, status: str = "deleted") -> None:
    conn.execute(
        "UPDATE media_items SET deleted_at=?, deletion_status=?, updated_at=? WHERE media_item_id=?",
        (now_iso(), status, now_iso(), media_item_id),
    )


def get_or_create_album(conn: sqlite3.Connection, album_id: str, title: str, purpose: str) -> None:
    conn.execute(
        """INSERT INTO albums (album_id, title, purpose)
           VALUES (?,?,?)
           ON CONFLICT(album_id) DO NOTHING""",
        (album_id, title, purpose),
    )


def save_page_token(conn: sqlite3.Connection, round_name: str, token: str | None) -> None:
    conn.execute(
        """INSERT INTO round_progress (round_name, last_page_token)
           VALUES (?,?)
           ON CONFLICT(round_name) DO UPDATE SET last_page_token=excluded.last_page_token""",
        (round_name, token),
    )


def load_page_token(conn: sqlite3.Connection, round_name: str) -> str | None:
    row = conn.execute(
        "SELECT last_page_token FROM round_progress WHERE round_name=?", (round_name,)
    ).fetchone()
    return row["last_page_token"] if row else None


def increment_progress(conn: sqlite3.Connection, round_name: str, delta: int = 1) -> None:
    conn.execute(
        """INSERT INTO round_progress (round_name, items_processed)
           VALUES (?,?)
           ON CONFLICT(round_name) DO UPDATE SET items_processed = items_processed + ?""",
        (round_name, delta, delta),
    )


def mark_round_complete(conn: sqlite3.Connection, round_name: str) -> None:
    conn.execute(
        """INSERT INTO round_progress (round_name, completed_at)
           VALUES (?,?)
           ON CONFLICT(round_name) DO UPDATE SET completed_at=excluded.completed_at""",
        (round_name, now_iso()),
    )
