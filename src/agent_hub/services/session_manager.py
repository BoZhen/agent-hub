from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import aiosqlite

from agent_hub import db

logger = logging.getLogger(__name__)


async def ensure_session(
    conn: aiosqlite.Connection,
    *,
    session_id: str,
    hub_id: str,
    hostname: str,
    cwd: str,
    payload: dict[str, Any],
) -> None:
    """Create session if not exists, or reactivate on resume."""
    event_type = payload.get("hook_event_name", "")
    model = payload.get("model") if event_type == "SessionStart" else None
    source = payload.get("source", "") if event_type == "SessionStart" else ""

    existing = await db.get_session(conn, session_id)

    if existing is None:
        await db.upsert_session(
            conn,
            session_id=session_id,
            hub_id=hub_id,
            hostname=hostname,
            cwd=cwd,
            model=model,
            status="active",
        )
    elif event_type == "SessionStart" and source == "resume":
        await db.upsert_session(
            conn,
            session_id=session_id,
            hub_id=hub_id,
            hostname=hostname,
            cwd=cwd,
            model=model,
            status="active",
        )


async def update_session_activity(
    conn: aiosqlite.Connection, session_id: str
) -> None:
    await db.update_session_activity(conn, session_id)


async def mark_session_idle(
    conn: aiosqlite.Connection, session_id: str
) -> None:
    await db.update_session_status(conn, session_id, "idle")


async def sweep_stale_sessions(
    conn: aiosqlite.Connection, idle_timeout_minutes: int
) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=idle_timeout_minutes)
    stale = await db.get_stale_sessions(conn, cutoff)
    for session in stale:
        await db.update_session_status(conn, session["session_id"], "stopped")
    if stale:
        logger.info("Swept %d stale sessions to stopped", len(stale))
    return len(stale)


async def periodic_sweep(
    conn: aiosqlite.Connection, idle_timeout_minutes: int
) -> None:
    """Background task: sweep stale sessions every 60 seconds."""
    while True:
        try:
            await asyncio.sleep(60)
            await sweep_stale_sessions(conn, idle_timeout_minutes)
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Error in session sweep")
