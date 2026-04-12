from __future__ import annotations

import asyncio

import httpx
from fastapi import APIRouter, HTTPException, Query, Request

from pydantic import BaseModel

from agent_hub import db
from agent_hub.models import EventResponse, SessionResponse, StatsResponse

router = APIRouter()


@router.get("/sessions", response_model=list[SessionResponse])
async def list_sessions(
    request: Request,
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    conn = request.app.state.db
    rows = await db.get_sessions(conn, status=status, limit=limit, offset=offset)
    return rows


@router.get("/sessions/{session_id}", response_model=SessionResponse)
async def get_session(request: Request, session_id: str):
    conn = request.app.state.db
    row = await db.get_session(conn, session_id)
    if not row:
        raise HTTPException(status_code=404, detail="Session not found")
    return row


@router.get("/sessions/{session_id}/events", response_model=list[EventResponse])
async def list_session_events(
    request: Request,
    session_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    conn = request.app.state.db
    session = await db.get_session(conn, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    rows = await db.get_session_events(conn, session_id, limit=limit, offset=offset)
    return rows


@router.get(
    "/sessions/{session_id}/events/latest", response_model=list[EventResponse]
)
async def latest_session_events(
    request: Request,
    session_id: str,
    n: int = Query(default=10, ge=1, le=100),
):
    conn = request.app.state.db
    rows = await db.get_session_events_latest(conn, session_id, n=n)
    return rows


@router.get("/stats", response_model=StatsResponse)
async def get_stats(request: Request):
    conn = request.app.state.db
    return await db.get_stats(conn)


@router.post("/sessions/clear-stopped")
async def clear_stopped_sessions(request: Request):
    """Bulk-delete every session with status='stopped' (and its events)."""
    conn = request.app.state.db
    count = await db.delete_stopped_sessions(conn)
    return {"ok": True, "deleted": count}


@router.delete("/sessions/{session_id}")
async def delete_session(request: Request, session_id: str):
    """Delete a session and its events. For idle sessions with a
    live tmux, also kill the tmux (terminates Claude) so the delete
    is final — otherwise the session would reappear on the next
    hook event."""
    conn = request.app.state.db
    session = await db.get_session(conn, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if session["status"] == "active":
        raise HTTPException(
            status_code=400,
            detail="Cannot delete an active session — wait until idle",
        )

    tmux_name = session.get("tmux_session")
    if session["status"] == "idle" and tmux_name:
        proc = await asyncio.create_subprocess_exec(
            "tmux", "kill-session", "-t", f"{tmux_name}:",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.communicate()
        # Non-zero return is fine — tmux may already be gone.

    deleted = await db.delete_session(conn, session_id)
    if not deleted:
        raise HTTPException(status_code=500, detail="Failed to delete session")
    return {"ok": True}


@router.post("/sessions/{session_id}/approve")
async def approve_session(
    request: Request,
    session_id: str,
    always: bool = Query(default=False),
):
    """Send approval to the tmux session where this Claude Code is running.

    If always=False (default): select option 1 "Yes" (approve once).
    If always=True: select option 2 "Yes, allow for this session".
    """
    conn = request.app.state.db
    session = await db.get_session(conn, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    tmux_name = session.get("tmux_session")
    if not tmux_name:
        raise HTTPException(
            status_code=400,
            detail="No tmux session associated with this session. Claude Code must be running inside tmux.",
        )

    if always:
        # Select option 2: Down arrow + Enter, via direct tmux send-keys.
        # Trailing colon forces session-level target (avoids pane lookup errors).
        proc = await asyncio.create_subprocess_exec(
            "tmux", "send-keys", "-t", f"{tmux_name}:", "Down", "Enter",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            err_msg = stderr.decode().strip() if stderr else "unknown error"
            raise HTTPException(
                status_code=400,
                detail=f"tmux send-keys failed: {err_msg}",
            )
    else:
        # Select option 1: send 'y' + Enter via Web Terminal API
        terminal_port = request.app.state.config.terminal_port
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"http://127.0.0.1:{terminal_port}/api/terminals/{tmux_name}/send",
                    json={"keys": "y\n"},
                    timeout=5.0,
                )
                if resp.status_code == 404:
                    raise HTTPException(
                        status_code=400,
                        detail=f"tmux session '{tmux_name}' not found on terminal server",
                    )
                resp.raise_for_status()
        except httpx.HTTPError as e:
            raise HTTPException(status_code=502, detail=f"Terminal server error: {e}")

    return {"ok": True, "tmux_session": tmux_name, "always": always}


class NotifyRequest(BaseModel):
    message: str


@router.post("/notify")
async def send_notification(request: Request, body: NotifyRequest):
    """Send a message to the configured Telegram chat."""
    from agent_hub.services.telegram_bot import _bot_instance

    if not _bot_instance or not _bot_instance.chat_id:
        raise HTTPException(
            status_code=503,
            detail="Telegram bot not configured or no chat_id",
        )
    try:
        await _bot_instance.app.bot.send_message(
            chat_id=_bot_instance.chat_id,
            text=body.message,
            parse_mode="Markdown",
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Telegram send failed: {e}")
    return {"ok": True}
