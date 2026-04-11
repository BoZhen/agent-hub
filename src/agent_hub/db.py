from __future__ import annotations

from datetime import datetime, timezone

import aiosqlite

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id    TEXT PRIMARY KEY,
    hub_id        TEXT NOT NULL,
    hostname      TEXT NOT NULL,
    cwd           TEXT NOT NULL,
    model         TEXT,
    status        TEXT NOT NULL DEFAULT 'active',
    started_at    DATETIME NOT NULL,
    last_seen_at  DATETIME NOT NULL,
    stopped_at    DATETIME,
    metadata      JSON
);

CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    event_uid     TEXT NOT NULL UNIQUE,
    hub_id        TEXT NOT NULL,
    session_id    TEXT NOT NULL REFERENCES sessions(session_id),
    event_type    TEXT NOT NULL,
    tool_name     TEXT,
    summary       TEXT,
    payload       JSON,
    created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_hub ON events(hub_id, id);
CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status);
"""


_MIGRATIONS = [
    "ALTER TABLE sessions ADD COLUMN transcript_path TEXT",
    "ALTER TABLE sessions ADD COLUMN input_tokens INTEGER DEFAULT 0",
    "ALTER TABLE sessions ADD COLUMN output_tokens INTEGER DEFAULT 0",
    "ALTER TABLE sessions ADD COLUMN cache_read_tokens INTEGER DEFAULT 0",
    "ALTER TABLE sessions ADD COLUMN cache_create_tokens INTEGER DEFAULT 0",
]


async def init_db(db_path: str) -> aiosqlite.Connection:
    db = await aiosqlite.connect(db_path)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    await db.executescript(_SCHEMA)
    for migration in _MIGRATIONS:
        try:
            await db.execute(migration)
        except Exception:
            pass  # Column already exists
    await db.commit()
    return db


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Session CRUD ──────────────────────────────────────────────


async def upsert_session(
    db: aiosqlite.Connection,
    *,
    session_id: str,
    hub_id: str,
    hostname: str,
    cwd: str,
    model: str | None = None,
    status: str = "active",
    transcript_path: str | None = None,
) -> None:
    now = _now()
    await db.execute(
        """
        INSERT INTO sessions (session_id, hub_id, hostname, cwd, model, status, started_at, last_seen_at, transcript_path)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(session_id) DO UPDATE SET
            status = excluded.status,
            last_seen_at = excluded.last_seen_at,
            model = COALESCE(excluded.model, sessions.model),
            cwd = excluded.cwd,
            transcript_path = COALESCE(excluded.transcript_path, sessions.transcript_path)
        """,
        (session_id, hub_id, hostname, cwd, model, status, now, now, transcript_path),
    )
    await db.commit()


async def update_session_usage(
    db: aiosqlite.Connection,
    session_id: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_create_tokens: int,
) -> None:
    await db.execute(
        """
        UPDATE sessions
        SET input_tokens = ?, output_tokens = ?, cache_read_tokens = ?, cache_create_tokens = ?
        WHERE session_id = ?
        """,
        (input_tokens, output_tokens, cache_read_tokens, cache_create_tokens, session_id),
    )
    await db.commit()


async def update_session_model(
    db: aiosqlite.Connection, session_id: str, model: str
) -> None:
    await db.execute(
        "UPDATE sessions SET model = ? WHERE session_id = ?",
        (model, session_id),
    )
    await db.commit()


async def update_session_activity(db: aiosqlite.Connection, session_id: str) -> None:
    await db.execute(
        "UPDATE sessions SET last_seen_at = ?, status = 'active' WHERE session_id = ?",
        (_now(), session_id),
    )
    await db.commit()


async def update_session_status(
    db: aiosqlite.Connection, session_id: str, status: str
) -> None:
    params: tuple
    if status == "stopped":
        params = (status, _now(), session_id)
        await db.execute(
            "UPDATE sessions SET status = ?, stopped_at = ? WHERE session_id = ?",
            params,
        )
    else:
        await db.execute(
            "UPDATE sessions SET status = ? WHERE session_id = ?",
            (status, session_id),
        )
    await db.commit()


async def get_stale_sessions(
    db: aiosqlite.Connection, cutoff: datetime
) -> list[dict]:
    cursor = await db.execute(
        "SELECT * FROM sessions WHERE status IN ('active', 'idle') AND last_seen_at < ?",
        (cutoff.isoformat(),),
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def get_sessions(
    db: aiosqlite.Connection,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    if status and status != "all":
        cursor = await db.execute(
            "SELECT * FROM sessions WHERE status = ? ORDER BY last_seen_at DESC LIMIT ? OFFSET ?",
            (status, limit, offset),
        )
    else:
        cursor = await db.execute(
            "SELECT * FROM sessions ORDER BY last_seen_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def get_session(
    db: aiosqlite.Connection, session_id: str
) -> dict | None:
    cursor = await db.execute(
        "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def delete_session(db: aiosqlite.Connection, session_id: str) -> bool:
    cursor = await db.execute(
        "SELECT status FROM sessions WHERE session_id = ?", (session_id,)
    )
    row = await cursor.fetchone()
    if not row or row["status"] != "stopped":
        return False
    await db.execute("DELETE FROM events WHERE session_id = ?", (session_id,))
    await db.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
    await db.commit()
    return True


# ── Event CRUD ────────────────────────────────────────────────


async def insert_event(
    db: aiosqlite.Connection,
    *,
    hub_id: str,
    session_id: str,
    event_type: str,
    tool_name: str | None = None,
    summary: str | None = None,
    payload: str | None = None,
) -> int:
    """Insert event with a placeholder event_uid, then update it with hub_id:id."""
    cursor = await db.execute(
        """
        INSERT INTO events (event_uid, hub_id, session_id, event_type, tool_name, summary, payload, created_at)
        VALUES ('_pending', ?, ?, ?, ?, ?, ?, ?)
        """,
        (hub_id, session_id, event_type, tool_name, summary, payload, _now()),
    )
    row_id = cursor.lastrowid
    event_uid = f"{hub_id}:{row_id}"
    await db.execute(
        "UPDATE events SET event_uid = ? WHERE id = ?", (event_uid, row_id)
    )
    await db.commit()
    return row_id


async def get_session_events(
    db: aiosqlite.Connection,
    session_id: str,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    cursor = await db.execute(
        "SELECT id, event_uid, session_id, event_type, tool_name, summary, created_at FROM events WHERE session_id = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (session_id, limit, offset),
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def get_session_events_latest(
    db: aiosqlite.Connection, session_id: str, n: int = 10
) -> list[dict]:
    return await get_session_events(db, session_id, limit=n)


# ── Stats ─────────────────────────────────────────────────────


async def get_stats(db: aiosqlite.Connection) -> dict:
    result: dict = {}
    for status in ("active", "idle", "stopped"):
        cursor = await db.execute(
            "SELECT COUNT(*) FROM sessions WHERE status = ?", (status,)
        )
        row = await cursor.fetchone()
        result[f"{status}_sessions"] = row[0]

    cursor = await db.execute("SELECT COUNT(*) FROM events")
    row = await cursor.fetchone()
    result["total_events"] = row[0]

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cursor = await db.execute(
        "SELECT COUNT(*) FROM events WHERE created_at >= ?", (today,)
    )
    row = await cursor.fetchone()
    result["today_events"] = row[0]

    return result
