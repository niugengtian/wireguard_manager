from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from flask import current_app, g


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL COLLATE NOCASE UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin', 'user')),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    device_quota INTEGER NOT NULL CHECK (device_quota BETWEEN 0 AND 100),
    session_version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS devices (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    name TEXT NOT NULL,
    client_type TEXT NOT NULL CHECK (client_type IN ('windows','macos','linux','ios','android')),
    static_ip TEXT NOT NULL UNIQUE,
    public_key TEXT NOT NULL UNIQUE,
    client_allowed_ips TEXT NOT NULL DEFAULT '0.0.0.0/0',
    policy_revision INTEGER NOT NULL DEFAULT 1 CHECK (policy_revision > 0),
    delivered_policy_revision INTEGER NOT NULL DEFAULT 1 CHECK (delivered_policy_revision > 0),
    key_generation INTEGER NOT NULL DEFAULT 1 CHECK (key_generation > 0),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, name)
);

CREATE TABLE IF NOT EXISTS installers (
    id TEXT PRIMARY KEY,
    platform TEXT NOT NULL CHECK (platform IN ('windows','macos','linux','ios','android')),
    architecture TEXT NOT NULL,
    version TEXT NOT NULL,
    original_filename TEXT NOT NULL,
    stored_filename TEXT NOT NULL UNIQUE,
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
    size_bytes INTEGER NOT NULL CHECK (size_bytes > 0),
    media_type TEXT NOT NULL,
    license_name TEXT NOT NULL,
    license_source_url TEXT NOT NULL,
    redistribution_confirmed INTEGER NOT NULL CHECK (redistribution_confirmed = 1),
    uploaded_by INTEGER REFERENCES users(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    actor_user_id INTEGER REFERENCES users(id) ON DELETE RESTRICT,
    actor_kind TEXT NOT NULL CHECK (actor_kind IN ('web', 'cli', 'system')),
    action TEXT NOT NULL,
    object_type TEXT NOT NULL,
    object_id TEXT,
    outcome TEXT NOT NULL CHECK (outcome IN ('success', 'denied', 'failed')),
    source_hash TEXT,
    details_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS login_throttle (
    login_key TEXT PRIMARY KEY,
    failures INTEGER NOT NULL,
    window_started INTEGER NOT NULL,
    blocked_until INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS devices_user_idx ON devices(user_id);
CREATE INDEX IF NOT EXISTS audit_created_idx ON audit_events(id DESC);
CREATE INDEX IF NOT EXISTS installers_platform_idx ON installers(platform, architecture);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path), timeout=15, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 15000")
    return connection


def initialize(path: str | Path, *, default_client_allowed_ips: str = "0.0.0.0/0") -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    connection = connect(path)
    try:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        connection.executescript(SCHEMA)
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(users)")}
        if "session_version" not in columns:
            connection.execute("ALTER TABLE users ADD COLUMN session_version INTEGER NOT NULL DEFAULT 1")
        device_columns = {row["name"] for row in connection.execute("PRAGMA table_info(devices)")}
        if "client_allowed_ips" not in device_columns:
            connection.execute("ALTER TABLE devices ADD COLUMN client_allowed_ips TEXT NOT NULL DEFAULT ''")
        if "policy_revision" not in device_columns:
            connection.execute("ALTER TABLE devices ADD COLUMN policy_revision INTEGER NOT NULL DEFAULT 1")
        if "delivered_policy_revision" not in device_columns:
            connection.execute(
                "ALTER TABLE devices ADD COLUMN delivered_policy_revision INTEGER NOT NULL DEFAULT 1"
            )
        connection.execute(
            "UPDATE devices SET client_allowed_ips = ? WHERE client_allowed_ips = ''",
            (default_client_allowed_ips,),
        )
    finally:
        connection.close()
    path.chmod(0o600)


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE"])
    return g.db


def close_db(_error: BaseException | None = None) -> None:
    connection = g.pop("db", None)
    if connection is not None:
        connection.close()


@contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False):
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield connection
    except BaseException:
        connection.rollback()
        raise
    else:
        try:
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
