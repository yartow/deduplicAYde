import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

DB_PATH = os.path.join(os.environ.get("DATA_DIR", "/data"), "state.db")

# NOTE: Google's March 2024 Photos Library API policy change restricts OAuth
# tokens (even rclone's pre-registered client) to `photoslibrary.readonly
# .appcreateddata` — an app can only see media items it uploaded itself. Since
# this app has never uploaded anything, the API can never enumerate the
# existing library, so `media_items` can no longer be keyed on a Google
# `mediaItemId` (there's no way to obtain one for a pre-existing item). Identity
# is now anchored to the local file path instead; the cloud side is something
# Playwright locates and acts on directly, never something read via the API.
# The schema below is a one-time breaking cut (not an additive migration) —
# safe because state.db had zero rows under the old schema at the time of the
# rewrite.
_SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS media_items (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    local_path     TEXT NOT NULL UNIQUE,
    filename       TEXT NOT NULL,
    file_size      INTEGER,
    phash          TEXT,
    -- local file timestamp (never use mtime/ctime — Takeout corrupts these)
    local_timestamp        TEXT,   -- resolved true timestamp (EXIF or sidecar)
    local_timestamp_source TEXT,   -- exif | sidecar | none
    -- best-effort cloud reference; the API can no longer be used to discover
    -- this for pre-existing items, so it generally stays NULL
    cloud_media_item_id TEXT,
    -- detection results
    blur_score     REAL,
    edge_density   REAL,
    ocr_text_density REAL,
    label          TEXT,    -- receipt | vague | ok | NULL (unprocessed)
    -- staging (Playwright-confirmed add-to-album)
    staged_album_id TEXT,
    staged_at       TEXT,
    -- manual review (for vague items)
    review_status   TEXT,   -- pending | confirmed_delete | keep
    reviewed_at     TEXT,
    -- deletion
    deleted_at      TEXT,
    deletion_status TEXT,   -- deleted | failed
    -- local-only video purge bookkeeping (cloud copy kept; local_path stays
    -- populated as the row's identity even after the physical file is gone —
    -- consumers already check path existence, not path presence)
    local_video_purged_at TEXT,
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
    item_a_id        INTEGER NOT NULL REFERENCES media_items(id),
    item_b_id        INTEGER NOT NULL REFERENCES media_items(id),
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

# One-time breaking cut: the old media_items/duplicate_pairs shape (keyed on
# Google mediaItemId) is incompatible with the new local-path-keyed shape.
# Drop and recreate rather than ALTER, since there was no salvageable data
# under the old schema. Safe to remove this block in a later commit once the
# new schema has shipped.
_DROP_LEGACY_SCHEMA = """
DROP TABLE IF EXISTS duplicate_pairs;
DROP TABLE IF EXISTS media_items;
"""


def init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    existing_cols = {r[1] for r in conn.execute("PRAGMA table_info(media_items)").fetchall()}
    if "media_item_id" in existing_cols:
        # Old cloud-ID-keyed schema found; drop it (see _DROP_LEGACY_SCHEMA docstring above).
        conn.executescript(_DROP_LEGACY_SCHEMA)
    conn.executescript(_SCHEMA)
    _migrate(conn)
    conn.commit()
    conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after the initial schema so existing DBs stay compatible."""
    existing = {r[1] for r in conn.execute("PRAGMA table_info(media_items)").fetchall()}
    for col, col_type in []:
        if col not in existing:
            conn.execute(f"ALTER TABLE media_items ADD COLUMN {col} {col_type}")
    _ = existing  # no pending additive migrations right now


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


def upsert_local_item(
    conn: sqlite3.Connection,
    local_path: str,
    filename: str,
    file_size: int,
    local_timestamp: str | None = None,
    local_timestamp_source: str | None = None,
    phash: str | None = None,
) -> int:
    """Insert or refresh a media item discovered on disk. Returns the row id."""
    conn.execute(
        """
        INSERT INTO media_items
            (local_path, filename, file_size, local_timestamp, local_timestamp_source, phash, updated_at)
        VALUES (:local_path, :filename, :file_size, :local_timestamp, :local_timestamp_source, :phash, :now)
        ON CONFLICT(local_path) DO UPDATE SET
            filename                = excluded.filename,
            file_size               = excluded.file_size,
            local_timestamp         = excluded.local_timestamp,
            local_timestamp_source  = excluded.local_timestamp_source,
            phash                   = COALESCE(excluded.phash, media_items.phash),
            updated_at              = excluded.updated_at
        """,
        {
            "local_path": local_path,
            "filename": filename,
            "file_size": file_size,
            "local_timestamp": local_timestamp,
            "local_timestamp_source": local_timestamp_source,
            "phash": phash,
            "now": now_iso(),
        },
    )
    row = conn.execute(
        "SELECT id FROM media_items WHERE local_path=?", (local_path,)
    ).fetchone()
    return row["id"]


def set_detection_result(
    conn: sqlite3.Connection,
    item_id: int,
    blur_score: float,
    edge_density: float,
    ocr_text_density: float,
    label: str,
) -> None:
    conn.execute(
        """UPDATE media_items SET
            blur_score=?, edge_density=?, ocr_text_density=?,
            label=?, updated_at=?
           WHERE id=?""",
        (blur_score, edge_density, ocr_text_density, label, now_iso(), item_id),
    )


def set_staged(conn: sqlite3.Connection, item_id: int, album_id: str) -> None:
    conn.execute(
        "UPDATE media_items SET staged_album_id=?, staged_at=?, updated_at=? WHERE id=?",
        (album_id, now_iso(), now_iso(), item_id),
    )


def set_video_purged(conn: sqlite3.Connection, item_id: int) -> None:
    """Record purge time for a locally-deleted video (cloud copy kept). local_path
    is left in place — it's the row's identity now, not just a pointer, and every
    consumer already checks file existence rather than path presence."""
    conn.execute(
        "UPDATE media_items SET local_video_purged_at=?, updated_at=? WHERE id=?",
        (now_iso(), now_iso(), item_id),
    )


def set_deleted(conn: sqlite3.Connection, item_id: int, status: str = "deleted") -> None:
    conn.execute(
        "UPDATE media_items SET deleted_at=?, deletion_status=?, updated_at=? WHERE id=?",
        (now_iso(), status, now_iso(), item_id),
    )


def update_local_path(conn: sqlite3.Connection, item_id: int, new_local_path: str) -> None:
    """Repoint a row at a file's new location after it's been moved on disk
    (e.g. round3.py archiving a confirmed-deleted receipt into receipts/)."""
    conn.execute(
        "UPDATE media_items SET local_path=?, updated_at=? WHERE id=?",
        (new_local_path, now_iso(), item_id),
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
