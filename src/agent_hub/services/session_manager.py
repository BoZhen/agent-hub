from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import aiosqlite

from agent_hub import db
from agent_hub.api.ws import broadcaster
from agent_hub.services.transcript_reader import read_pending_tool

logger = logging.getLogger(__name__)


async def ensure_session(
    conn: aiosqlite.Connection,
    *,
    session_id: str,
    hub_id: str,
    hostname: str,
    cwd: str,
    transcript_path: str | None = None,
    payload: dict[str, Any],
) -> None:
    """Create session if not exists, or reactivate on resume."""
    event_type = payload.get("hook_event_name", "")
    model = payload.get("model")  # Accept model from any event type

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
            transcript_path=transcript_path,
        )
    elif event_type == "SessionStart":
        await db.upsert_session(
            conn,
            session_id=session_id,
            hub_id=hub_id,
            hostname=hostname,
            cwd=cwd,
            model=model,
            status="active",
            transcript_path=transcript_path,
        )
    elif existing.get("model") is None and model:
        # Backfill model if we learn it from a later event
        await db.upsert_session(
            conn,
            session_id=session_id,
            hub_id=hub_id,
            hostname=hostname,
            cwd=cwd,
            model=model,
            transcript_path=transcript_path,
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


async def periodic_pending_check(conn: aiosqlite.Connection) -> None:
    """Background task: check active sessions for pending tool authorization every 3s."""
    while True:
        try:
            await asyncio.sleep(3)
            sessions = await db.get_sessions(conn, status="active")
            for session in sessions:
                transcript_path = session.get("transcript_path")
                if not transcript_path:
                    continue
                pending = read_pending_tool(transcript_path)
                if pending != session.get("pending_tool"):
                    await db.update_session_pending_tool(
                        conn, session["session_id"], pending
                    )
                    # Count total waiting for stats
                    stats = await db.get_stats(conn)
                    await broadcaster.broadcast({
                        "type": "pending",
                        "session_id": session["session_id"],
                        "pending_tool": pending,
                        "waiting_count": stats["waiting_sessions"],
                    })
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Error checking pending tools")
