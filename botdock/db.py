"""SQLite storage. One connection shared across threads, guarded by a lock."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS bots (
    id               TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    name_lower       TEXT NOT NULL UNIQUE,
    description      TEXT NOT NULL DEFAULT '',
    entrypoint       TEXT,
    args             TEXT NOT NULL DEFAULT '',
    extra_packages   TEXT NOT NULL DEFAULT '',
    restart_policy   TEXT NOT NULL DEFAULT 'on-failure',
    autostart        INTEGER NOT NULL DEFAULT 1,
    desired_state    TEXT NOT NULL DEFAULT 'stopped',
    deps_fingerprint TEXT,
    created_at       REAL NOT NULL,
    updated_at       REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS env_vars (
    bot_id     TEXT NOT NULL REFERENCES bots(id) ON DELETE CASCADE,
    key        TEXT NOT NULL,
    value_enc  BLOB NOT NULL,
    length     INTEGER NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (bot_id, key)
);
"""

SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS bot_sources (
    bot_id          TEXT PRIMARY KEY REFERENCES bots(id) ON DELETE CASCADE,
    repo            TEXT NOT NULL,
    branch          TEXT NOT NULL,
    subdir          TEXT NOT NULL DEFAULT '',
    auto_update     INTEGER NOT NULL DEFAULT 1,
    commit_sha      TEXT,
    commit_message  TEXT,
    commit_date     TEXT,
    latest_sha      TEXT,
    last_checked_at REAL,
    last_synced_at  REAL,
    last_error      TEXT,
    etag            TEXT,
    manifest        TEXT NOT NULL DEFAULT '{}'
);
"""
SCHEMA_VERSION = 3
SOURCE_COLUMNS = {
    "repo", "branch", "subdir", "auto_update", "commit_sha", "commit_message", "commit_date", "latest_sha",
    "last_checked_at", "last_synced_at", "last_error", "etag", "manifest", "keep_paths",
}

BOT_COLUMNS = (
    "id", "name", "description", "entrypoint", "args", "extra_packages", "restart_policy",
    "autostart", "desired_state", "deps_fingerprint", "created_at", "updated_at",
)
UPDATABLE_BOT_COLUMNS = {
    "name", "description", "entrypoint", "args", "extra_packages", "restart_policy",
    "autostart", "desired_state", "deps_fingerprint",
}


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.execute("PRAGMA secure_delete = ON")
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            self._conn.executescript(SCHEMA)  # v1 tables (IF NOT EXISTS)
            if version < 2:
                # v2: GitHub-linked bots. Purely additive, so a v1 database upgrades in place.
                self._conn.executescript(SCHEMA_V2)
            if version < 3:
                # v3: per-bot keep_paths (glob patterns a sync never overwrites/deletes)
                cols = {r[1] for r in self._conn.execute("PRAGMA table_info(bot_sources)").fetchall()}
                if "keep_paths" not in cols:
                    self._conn.execute("ALTER TABLE bot_sources ADD COLUMN keep_paths TEXT NOT NULL DEFAULT '[]'")
            self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # Generic ------------------------------------------------------------------------
    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def one(self, sql: str, params: tuple | dict = ()) -> dict | None:
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return dict(row) if row is not None else None

    def all(self, sql: str, params: tuple | dict = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    # kv -----------------------------------------------------------------------------
    def kv_get(self, key: str) -> str | None:
        row = self.one("SELECT value FROM kv WHERE key = ?", (key,))
        return row["value"] if row else None

    def kv_set(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    # bots ---------------------------------------------------------------------------
    def get_bot(self, bot_id: str) -> dict | None:
        return self.one("SELECT * FROM bots WHERE id = ?", (bot_id,))

    def list_bots(self) -> list[dict]:
        return self.all("SELECT * FROM bots ORDER BY name_lower")

    def bot_name_taken(self, name: str, exclude_id: str | None = None) -> bool:
        row = self.one("SELECT id FROM bots WHERE name_lower = ?", (name.lower(),))
        return row is not None and row["id"] != exclude_id

    def insert_bot(self, bot: dict[str, Any]) -> None:
        cols = [c for c in BOT_COLUMNS if c in bot]
        values = [bot[c] for c in cols]
        cols.append("name_lower")
        values.append(bot["name"].lower())
        self.execute(
            f"INSERT INTO bots({', '.join(cols)}) VALUES({', '.join('?' for _ in cols)})", tuple(values)
        )

    def update_bot(self, bot_id: str, fields: dict[str, Any], touch: bool = True) -> None:
        fields = dict(fields)
        unknown = set(fields) - UPDATABLE_BOT_COLUMNS
        if unknown:
            raise ValueError(f"unknown columns {unknown}")
        if "name" in fields:
            fields["name_lower"] = fields["name"].lower()
        if touch:
            fields["updated_at"] = time.time()
        if not fields:
            return
        assignments = ", ".join(f"{k} = ?" for k in fields)
        self.execute(f"UPDATE bots SET {assignments} WHERE id = ?", (*fields.values(), bot_id))

    def delete_bot(self, bot_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM env_vars WHERE bot_id = ?", (bot_id,))
            self._conn.execute("DELETE FROM bot_sources WHERE bot_id = ?", (bot_id,))
            self._conn.execute("DELETE FROM bots WHERE id = ?", (bot_id,))

    # env ----------------------------------------------------------------------------
    def env_rows(self, bot_id: str) -> list[dict]:
        return self.all("SELECT key, value_enc, length, updated_at FROM env_vars WHERE bot_id = ? ORDER BY key", (bot_id,))

    def env_get(self, bot_id: str, key: str) -> dict | None:
        return self.one("SELECT key, value_enc, length, updated_at FROM env_vars WHERE bot_id = ? AND key = ?", (bot_id, key))

    def env_keys(self, bot_id: str) -> set[str]:
        return {r["key"] for r in self.all("SELECT key FROM env_vars WHERE bot_id = ?", (bot_id,))}

    def env_count(self, bot_id: str) -> int:
        row = self.one("SELECT COUNT(*) AS n FROM env_vars WHERE bot_id = ?", (bot_id,))
        return int(row["n"]) if row else 0

    def env_set(self, bot_id: str, key: str, value_enc: bytes, length: int, ts: float) -> None:
        self.execute(
            "INSERT INTO env_vars(bot_id, key, value_enc, length, updated_at) VALUES(?, ?, ?, ?, ?) "
            "ON CONFLICT(bot_id, key) DO UPDATE SET value_enc = excluded.value_enc, "
            "length = excluded.length, updated_at = excluded.updated_at",
            (bot_id, key, value_enc, length, ts),
        )

    def env_delete(self, bot_id: str, key: str) -> bool:
        cur = self.execute("DELETE FROM env_vars WHERE bot_id = ? AND key = ?", (bot_id, key))
        return cur.rowcount > 0

    # sources (v2) ---------------------------------------------------------------------
    def get_source(self, bot_id: str) -> dict | None:
        return self.one("SELECT * FROM bot_sources WHERE bot_id = ?", (bot_id,))

    def list_sources(self) -> list[dict]:
        return self.all("SELECT * FROM bot_sources ORDER BY bot_id")

    def upsert_source(self, bot_id: str, fields: dict[str, Any]) -> None:
        unknown = set(fields) - SOURCE_COLUMNS
        if unknown:
            raise ValueError(f"unknown columns {unknown}")
        with self._lock:
            exists = self._conn.execute("SELECT 1 FROM bot_sources WHERE bot_id = ?", (bot_id,)).fetchone()
            if exists:
                if fields:
                    assignments = ", ".join(f"{k} = ?" for k in fields)
                    self._conn.execute(f"UPDATE bot_sources SET {assignments} WHERE bot_id = ?",
                                       (*fields.values(), bot_id))
            else:
                cols = ["bot_id", *fields]
                self._conn.execute(
                    f"INSERT INTO bot_sources({', '.join(cols)}) VALUES({', '.join('?' for _ in cols)})",
                    (bot_id, *fields.values()))

    def update_source(self, bot_id: str, fields: dict[str, Any]) -> None:
        unknown = set(fields) - SOURCE_COLUMNS
        if unknown:
            raise ValueError(f"unknown columns {unknown}")
        if fields:
            assignments = ", ".join(f"{k} = ?" for k in fields)
            self.execute(f"UPDATE bot_sources SET {assignments} WHERE bot_id = ?", (*fields.values(), bot_id))

    def delete_source(self, bot_id: str) -> None:
        self.execute("DELETE FROM bot_sources WHERE bot_id = ?", (bot_id,))

    def kv_delete(self, key: str) -> None:
        self.execute("DELETE FROM kv WHERE key = ?", (key,))
