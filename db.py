"""SQLite-слой LiVPN: пользователи, подписки, узлы, кэш переименований."""
from __future__ import annotations

import secrets
import sqlite3
import string
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterable

from config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    tg_id       INTEGER PRIMARY KEY,
    username    TEXT,
    first_name  TEXT,
    created_at  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    channel_ok  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS subs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id           INTEGER NOT NULL UNIQUE,
    token           TEXT NOT NULL UNIQUE,
    name            TEXT NOT NULL,
    provider        TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    upload          INTEGER NOT NULL DEFAULT 0,
    download        INTEGER NOT NULL DEFAULT 0,
    total           INTEGER NOT NULL DEFAULT 0,
    upstream_url    TEXT NOT NULL DEFAULT '',
    client_uuid     TEXT NOT NULL DEFAULT '',
    client_email    TEXT NOT NULL DEFAULT '',
    last_refresh    TEXT NOT NULL DEFAULT '',
    last_attempt    TEXT NOT NULL DEFAULT '',
    refresh_failures INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS nodes (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    sub_id  INTEGER NOT NULL,
    uri     TEXT NOT NULL,
    name    TEXT NOT NULL,
    idx     INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_nodes_sub ON nodes(sub_id, idx);

CREATE TABLE IF NOT EXISTS name_cache (
    src TEXT PRIMARY KEY,
    dst TEXT NOT NULL
);
"""

# Колонки, которых может не быть в базе от старой версии.
SUB_COLUMNS = {
    "upstream_url": "TEXT NOT NULL DEFAULT ''",
    "client_uuid": "TEXT NOT NULL DEFAULT ''",
    "client_email": "TEXT NOT NULL DEFAULT ''",
    "last_refresh": "TEXT NOT NULL DEFAULT ''",
    "last_attempt": "TEXT NOT NULL DEFAULT ''",
    "refresh_failures": "INTEGER NOT NULL DEFAULT 0",
    "last_error": "TEXT NOT NULL DEFAULT ''",
}

ALPHABET = string.ascii_lowercase + string.digits


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


@contextmanager
def session():
    """Соединение с гарантированным commit/rollback и close.

    `with sqlite3.connect(...)` только коммитит, но не закрывает файл.
    """
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with session() as conn:
        conn.executescript(SCHEMA)
        have = {row["name"] for row in conn.execute("PRAGMA table_info(subs)")}
        for name, decl in SUB_COLUMNS.items():
            if name not in have:
                conn.execute(f"ALTER TABLE subs ADD COLUMN {name} {decl}")


# ── Пользователи ──────────────────────────────────────────────────────────────

def touch_user(tg_id: int, username: str | None, first_name: str | None) -> None:
    ts = now()
    with session() as conn:
        conn.execute(
            """INSERT INTO users (tg_id, username, first_name, created_at, last_seen)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(tg_id) DO UPDATE SET
                   username=excluded.username,
                   first_name=excluded.first_name,
                   last_seen=excluded.last_seen""",
            (tg_id, username, first_name, ts, ts),
        )


def set_channel_ok(tg_id: int, ok: bool) -> None:
    with session() as conn:
        conn.execute("UPDATE users SET channel_ok=? WHERE tg_id=?", (1 if ok else 0, tg_id))


def get_user(tg_id: int) -> sqlite3.Row | None:
    with session() as conn:
        return conn.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,)).fetchone()


# ── Подписки ──────────────────────────────────────────────────────────────────

def new_token(length: int = 20) -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(length))


def get_sub(sub_id: int) -> sqlite3.Row | None:
    with session() as conn:
        return conn.execute("SELECT * FROM subs WHERE id=?", (sub_id,)).fetchone()


def get_sub_by_user(tg_id: int) -> sqlite3.Row | None:
    with session() as conn:
        return conn.execute("SELECT * FROM subs WHERE tg_id=?", (tg_id,)).fetchone()


def get_sub_by_token(token: str) -> sqlite3.Row | None:
    with session() as conn:
        return conn.execute("SELECT * FROM subs WHERE token=?", (token,)).fetchone()


def create_sub(tg_id: int, name: str, provider: str, token_len: int = 20) -> sqlite3.Row:
    ts = now()
    with session() as conn:
        while True:
            token = new_token(token_len)
            try:
                conn.execute(
                    """INSERT INTO subs (tg_id, token, name, provider, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (tg_id, token, name, provider, ts, ts),
                )
                break
            except sqlite3.IntegrityError as exc:
                if "subs.token" in str(exc):
                    continue
                if "subs.tg_id" in str(exc):
                    row = conn.execute(
                        "SELECT * FROM subs WHERE tg_id=?", (tg_id,)
                    ).fetchone()
                    if row:
                        return row
                raise
        return conn.execute("SELECT * FROM subs WHERE tg_id=?", (tg_id,)).fetchone()


def rename_sub(sub_id: int, name: str) -> None:
    with session() as conn:
        conn.execute("UPDATE subs SET name=? WHERE id=?", (name, sub_id))


def set_traffic(sub_id: int, upload: int, download: int, total: int) -> None:
    with session() as conn:
        conn.execute(
            "UPDATE subs SET upload=?, download=?, total=? WHERE id=?",
            (int(upload), int(download), int(total), sub_id),
        )


def set_upstream(sub_id: int, url: str) -> None:
    with session() as conn:
        conn.execute("UPDATE subs SET upstream_url=? WHERE id=?", (url, sub_id))


def set_provider(sub_id: int, provider: str) -> None:
    with session() as conn:
        conn.execute(
            "UPDATE subs SET provider=?, updated_at=? WHERE id=?",
            (provider, now(), sub_id),
        )


def set_client(sub_id: int, uuid: str, email: str) -> None:
    with session() as conn:
        conn.execute(
            "UPDATE subs SET client_uuid=?, client_email=? WHERE id=?",
            (uuid, email, sub_id),
        )


def mark_refresh_ok(sub_id: int) -> None:
    ts = now()
    with session() as conn:
        conn.execute(
            """UPDATE subs SET last_refresh=?, last_attempt=?, updated_at=?,
                   refresh_failures=0, last_error=''
               WHERE id=?""",
            (ts, ts, ts, sub_id),
        )


def mark_refresh_error(sub_id: int, error: str) -> None:
    with session() as conn:
        conn.execute(
            """UPDATE subs SET last_attempt=?,
                   refresh_failures=refresh_failures + 1,
                   last_error=?
               WHERE id=?""",
            (now(), (error or "")[:500], sub_id),
        )


def list_subs() -> list[sqlite3.Row]:
    with session() as conn:
        return conn.execute("SELECT * FROM subs ORDER BY id").fetchall()


def delete_sub(sub_id: int) -> None:
    with session() as conn:
        conn.execute("DELETE FROM nodes WHERE sub_id=?", (sub_id,))
        conn.execute("DELETE FROM subs WHERE id=?", (sub_id,))


# ── Узлы ──────────────────────────────────────────────────────────────────────

def get_nodes(sub_id: int) -> list[sqlite3.Row]:
    with session() as conn:
        return conn.execute(
            "SELECT * FROM nodes WHERE sub_id=? ORDER BY idx, id", (sub_id,)
        ).fetchall()


def replace_nodes(sub_id: int, nodes: Iterable[tuple[str, str]]) -> None:
    """nodes: последовательность (uri, отображаемое_имя)."""
    with session() as conn:
        conn.execute("DELETE FROM nodes WHERE sub_id=?", (sub_id,))
        conn.executemany(
            "INSERT INTO nodes (sub_id, uri, name, idx) VALUES (?, ?, ?, ?)",
            [(sub_id, uri, name, i) for i, (uri, name) in enumerate(nodes)],
        )


# ── Кэш переименований ────────────────────────────────────────────────────────

def cache_get_many(srcs: Iterable[str]) -> dict[str, str]:
    srcs = list(srcs)
    if not srcs:
        return {}
    placeholders = ",".join("?" * len(srcs))
    with session() as conn:
        rows = conn.execute(
            f"SELECT src, dst FROM name_cache WHERE src IN ({placeholders})", srcs
        ).fetchall()
    return {r["src"]: r["dst"] for r in rows}


def cache_put_many(pairs: dict[str, str]) -> None:
    if not pairs:
        return
    with session() as conn:
        conn.executemany(
            "INSERT INTO name_cache (src, dst) VALUES (?, ?) "
            "ON CONFLICT(src) DO UPDATE SET dst=excluded.dst",
            list(pairs.items()),
        )


def clear_name_cache() -> None:
    with session() as conn:
        conn.execute("DELETE FROM name_cache")


def stats() -> dict[str, Any]:
    with session() as conn:
        users = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
        subs = conn.execute("SELECT COUNT(*) c FROM subs").fetchone()["c"]
        nodes = conn.execute("SELECT COUNT(*) c FROM nodes").fetchone()["c"]
        failures = conn.execute(
            "SELECT COUNT(*) c FROM subs WHERE refresh_failures > 0"
        ).fetchone()["c"]
    return {"users": users, "subs": subs, "nodes": nodes, "failing": failures}
