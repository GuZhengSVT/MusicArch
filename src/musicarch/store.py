"""SQLite storage and stable business keys."""

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path


class MusicArchError(Exception):
    """An error suitable for the command-line user."""


SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    platform TEXT NOT NULL,
    account_id TEXT NOT NULL,
    alias TEXT NOT NULL,
    PRIMARY KEY (platform, account_id),
    UNIQUE (platform, alias)
);
CREATE TABLE IF NOT EXISTS recordings (id TEXT PRIMARY KEY NOT NULL);
CREATE TABLE IF NOT EXISTS sources (
    platform TEXT NOT NULL,
    track_id TEXT NOT NULL,
    recording_id TEXT NOT NULL REFERENCES recordings ON DELETE RESTRICT,
    url TEXT,
    status TEXT NOT NULL CHECK (status IN ('available', 'unresolved', 'unavailable')),
    metadata TEXT NOT NULL,
    field_sources TEXT NOT NULL,
    title_key TEXT,
    artist_key TEXT,
    isrc TEXT,
    PRIMARY KEY (platform, track_id)
);
CREATE INDEX IF NOT EXISTS source_title_artist ON sources (title_key, artist_key);
CREATE INDEX IF NOT EXISTS source_isrc ON sources (isrc);
CREATE INDEX IF NOT EXISTS source_recording ON sources (recording_id);
CREATE TABLE IF NOT EXISTS track_tag_checks (
    platform TEXT NOT NULL,
    track_id TEXT NOT NULL,
    checked_at INTEGER NOT NULL,
    PRIMARY KEY (platform, track_id),
    FOREIGN KEY (platform, track_id) REFERENCES sources
);
CREATE TABLE IF NOT EXISTS collections (
    platform TEXT NOT NULL,
    account_id TEXT NOT NULL,
    collection_key TEXT NOT NULL,
    type TEXT NOT NULL CHECK (type IN ('liked', 'owned', 'subscribed', 'specified')),
    name TEXT NOT NULL,
    visibility TEXT NOT NULL CHECK (visibility IN ('public', 'private', 'unknown')),
    status TEXT NOT NULL CHECK (status IN ('active', 'missing')),
    playlist_id TEXT,
    PRIMARY KEY (platform, account_id, collection_key),
    FOREIGN KEY (platform, account_id) REFERENCES accounts
);
CREATE TABLE IF NOT EXISTS memberships (
    platform TEXT NOT NULL,
    account_id TEXT NOT NULL,
    collection_key TEXT NOT NULL,
    position INTEGER NOT NULL CHECK (position >= 0),
    track_id TEXT NOT NULL,
    added_at TEXT,
    PRIMARY KEY (platform, account_id, collection_key, position),
    FOREIGN KEY (platform, account_id, collection_key) REFERENCES collections,
    FOREIGN KEY (platform, track_id) REFERENCES sources
);
CREATE TABLE IF NOT EXISTS overrides (
    recording_id TEXT NOT NULL REFERENCES recordings,
    field TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (recording_id, field)
);
CREATE TABLE IF NOT EXISTS tags (
    recording_id TEXT NOT NULL REFERENCES recordings,
    dimension TEXT NOT NULL,
    norm TEXT NOT NULL,
    label TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('add', 'remove')),
    PRIMARY KEY (recording_id, dimension, norm)
);
CREATE TABLE IF NOT EXISTS separations (
    left_platform TEXT NOT NULL,
    left_track TEXT NOT NULL,
    right_platform TEXT NOT NULL,
    right_track TEXT NOT NULL,
    PRIMARY KEY (left_platform, left_track, right_platform, right_track),
    FOREIGN KEY (left_platform, left_track) REFERENCES sources,
    FOREIGN KEY (right_platform, right_track) REFERENCES sources
);
CREATE TABLE IF NOT EXISTS redirects (
    old_id TEXT PRIMARY KEY NOT NULL,
    recording_id TEXT NOT NULL REFERENCES recordings
);
"""


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


@contextmanager
def open_db(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, isolation_level=None, timeout=15)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(SCHEMA)
        yield connection
    finally:
        connection.close()


@contextmanager
def transaction(db, *, read=False):
    db.execute("BEGIN" if read else "BEGIN IMMEDIATE")
    try:
        yield
        db.commit()
    except BaseException:
        db.rollback()
        raise


def check_account(db, platform, account_id, alias):
    row = db.execute(
        "SELECT account_id, alias FROM accounts WHERE platform = ? AND (alias = ? OR account_id = ?)",
        (platform, alias, account_id),
    ).fetchone()
    if row and (row['account_id'], row['alias']) != (account_id, alias):
        raise MusicArchError(f"账号身份冲突 {platform}:{alias}。请检查授权账号和别名。")


def bind_account(db, platform, account_id, alias):
    check_account(db, platform, account_id, alias)
    db.execute("INSERT OR IGNORE INTO accounts VALUES (?, ?, ?)", (platform, account_id, alias))


def collection_selector(platform, alias, key):
    return f"{platform}:{alias}:{key}"
