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
    # Inject CLI tool identifier ("claude" default; "codex" when omx hook
    # wrapper posts here). Downstream session_manager.ensure_session uses
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


async def _process_safe(db, payload, hub_id, hostname):
    try:
        await event_processor.process_event(db, payload, hub_id, hostname)
    except Exception:
        logger.exception(
            "Failed to process event: session=%s type=%s",
            payload.get("session_id"),
            payload.get("hook_event_name"),
        )


def _task_done(task: asyncio.Task):
    if task.cancelled():
        return
    exc = task.exception()
    if exc:
        logger.error("Background event task failed: %s", exc)
