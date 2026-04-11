from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import aiosqlite

from agent_hub import db
from agent_hub.api.ws import broadcaster
from agent_hub.services import event_processor
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
    await db.update_session_pending_tool(conn, session_id, None, None)


async def sweep_stale_sessions(
    conn: aiosqlite.Connection, idle_timeout_minutes: int
) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=idle_timeout_minutes)
    stale = await db.get_stale_sessions(conn, cutoff)
    for session in stale:
        await db.update_session_status(conn, session["session_id"], "stopped")
        await db.update_session_pending_tool(conn, session["session_id"], None, None)
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
    CONFIRM_SECS = 6.0

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

                db_tool = session.get("pending_tool")

                # Source 1: event-driven candidates from PreToolUse hook
                evt_candidate = event_processor._pending_candidates.get(sid)

                # Source 2: transcript-based detection
                transcript_path = session.get("transcript_path")
                result = read_pending_tool(transcript_path) if transcript_path else None
                tx_tool = result.name if result else None
                tx_detail = result.detail if result else None

                # Merge: prefer event candidate (covers MCP/Bash that
                # don't write to transcript), fall back to transcript
                pending_tool = None
                pending_detail = None
                first_seen = now
                if evt_candidate:
                    pending_tool, pending_detail = evt_candidate[0], evt_candidate[1]
                    first_seen = evt_candidate[2]
                elif tx_tool:
                    pending_tool, pending_detail = tx_tool, tx_detail
                    prev = candidates.get(sid)
                    if prev and prev[0] == tx_tool and prev[1] == tx_detail:
                        first_seen = prev[2]
                    else:
                        candidates[sid] = (tx_tool, tx_detail, now)
                        continue  # new transcript candidate, wait for confirmation

                if pending_tool:
                    # Check confirmation delay
                    if now - first_seen < CONFIRM_SECS:
                        continue

                    # Confirmed — update DB and broadcast if changed
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
                    candidates.pop(sid, None)

            # Clean up candidates for sessions no longer active
            for sid in list(candidates):
                if sid not in seen_ids:
                    candidates.pop(sid)

        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Error checking pending tools")
