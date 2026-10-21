from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Query, Request

from agent_hub.services import event_processor

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/events")
async def receive_event(
    request: Request,
    host: str = Query(default="unknown"),
    tmux_session: str = Query(default=""),
    tool: str = Query(default="claude"),
):
    payload = await request.json()

    session_id = payload.get("session_id")
    hook_event_name = payload.get("hook_event_name")
    if not session_id or not hook_event_name:
        return {"ok": False, "error": "missing session_id or hook_event_name"}

    # Inject tmux_session from query param into payload for processing
    if tmux_session:
        payload["_tmux_session"] = tmux_session
    # Inject CLI tool identifier ("claude" default; "codex" when the codex
    # hook bridge posts here). Downstream session_manager.ensure_session uses
    # this to skip Claude-specific startup heuristics for codex.
    payload["_tool"] = tool

    db = request.app.state.db
    hub_id = request.app.state.config.hub_id

    task = asyncio.create_task(
        _process_safe(db, payload, hub_id, host)
    )
    # Prevent task from being garbage collected and log exceptions
    task.add_done_callback(_task_done)

    return {"ok": True}


# One lock per session, with the number of tasks holding or waiting for
# it. A CLI sends its hooks one after another, but each is processed in
# its own task, and a fast one can overtake a slow one: a Stop processed
# before the PostToolUse ahead of it leaves the finished session marked
# active, and the phone is never told. Taking the session's lock before
# the task's first await keeps processing in arrival order, because
# tasks start in the order they were created and asyncio.Lock is FIFO.
_session_locks: dict[str, list] = {}


async def _process_safe(db, payload, hub_id, hostname):
    sid = payload.get("session_id")
    entry = _session_locks.get(sid)
    if entry is None:
        entry = _session_locks[sid] = [asyncio.Lock(), 0]
    entry[1] += 1
    try:
        async with entry[0]:
            await event_processor.process_event(db, payload, hub_id, hostname)
    except Exception:
        logger.exception(
            "Failed to process event: session=%s type=%s",
            payload.get("session_id"),
            payload.get("hook_event_name"),
        )
    finally:
        entry[1] -= 1
        if entry[1] == 0:
            _session_locks.pop(sid, None)


def _task_done(task: asyncio.Task):
    if task.cancelled():
        return
    exc = task.exception()
    if exc:
        logger.error("Background event task failed: %s", exc)
