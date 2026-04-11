from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import aiosqlite

from agent_hub import db
from agent_hub.api.ws import broadcaster
from agent_hub.services.telegram_bot import notify_pending as tg_notify_pending
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


async def _tmux_session_alive(name: str) -> bool:
    """Check if a tmux session exists."""
    proc = await asyncio.create_subprocess_exec(
        "tmux", "has-session", "-t", name,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    return proc.returncode == 0


async def periodic_pending_check(conn: aiosqlite.Connection) -> None:
    """Background task: check active sessions for pending tool authorization every 3s.

    Uses a confirmation delay: a newly detected pending tool must persist for
    at least one extra check cycle before being broadcast.  This filters out
    false positives from auto-approved tools that execute and complete within
    a few seconds.
    """
    # Candidates: session_id -> (tool, detail, first_seen_monotonic)
    candidates: dict[str, tuple[str, str | None, float]] = {}
    CONFIRM_SECS = 5.0

    while True:
        try:
            await asyncio.sleep(3)
            now = time.monotonic()
            sessions = await db.get_sessions(conn, status="active")
            seen_ids: set[str] = set()

            for session in sessions:
                sid = session["session_id"]
                seen_ids.add(sid)

                # Check tmux liveness — if tmux died, clear pending but don't
                # mark stopped (Claude might still be running, or tmux could
                # be on a different server socket).
                tmux_name = session.get("tmux_session")
                if tmux_name and not await _tmux_session_alive(tmux_name):
                    if session.get("pending_tool"):
                        logger.info(
                            "tmux '%s' not reachable, clearing pending for %s",
                            tmux_name, sid[:12],
                        )
                        await db.update_session_pending_tool(conn, sid, None, None)
                        candidates.pop(sid, None)
                        stats = await db.get_stats(conn)
                        await broadcaster.broadcast({
                            "type": "pending",
                            "session_id": sid,
                            "pending_tool": None,
                            "pending_detail": None,
                            "tmux_session": tmux_name,
                            "waiting_count": stats["waiting_sessions"],
                        })
                    continue

                transcript_path = session.get("transcript_path")
                if not transcript_path:
                    continue

                result = read_pending_tool(transcript_path)
                pending_tool = result.name if result else None
                pending_detail = result.detail if result else None
                db_tool = session.get("pending_tool")

                if pending_tool:
                    prev = candidates.get(sid)
                    if prev is None or prev[0] != pending_tool or prev[1] != pending_detail:
                        # New candidate — start confirmation timer
                        candidates[sid] = (pending_tool, pending_detail, now)
                        continue
                    # Same candidate — check if confirmed
                    if now - prev[2] < CONFIRM_SECS:
                        continue  # not confirmed yet

                    # Confirmed pending — broadcast if DB state differs
                    if pending_tool != db_tool or pending_detail != session.get("pending_detail"):
                        await db.update_session_pending_tool(
                            conn, sid, pending_tool, pending_detail,
                        )
                        stats = await db.get_stats(conn)
                        await broadcaster.broadcast({
                            "type": "pending",
                            "session_id": sid,
                            "pending_tool": pending_tool,
                            "pending_detail": pending_detail,
                            "tmux_session": session.get("tmux_session"),
                            "waiting_count": stats["waiting_sessions"],
                        })
                        await tg_notify_pending(
                            sid, result, session,
                        )
                else:
                    # No longer pending — clear candidate and DB
                    candidates.pop(sid, None)
                    if db_tool is not None:
                        await db.update_session_pending_tool(conn, sid, None, None)
                        stats = await db.get_stats(conn)
                        await broadcaster.broadcast({
                            "type": "pending",
                            "session_id": sid,
                            "pending_tool": None,
                            "pending_detail": None,
                            "tmux_session": session.get("tmux_session"),
                            "waiting_count": stats["waiting_sessions"],
                        })
                        await tg_notify_pending(sid, None, session)

            # Clean up candidates for sessions no longer active
            for sid in list(candidates):
                if sid not in seen_ids:
                    candidates.pop(sid)

        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Error checking pending tools")
