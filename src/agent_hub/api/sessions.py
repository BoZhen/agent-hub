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
    """Send approval to the tmux session where the agent is running.

    Dispatch by tool:
    - Claude: option 1 via Web Terminal `y\\n`, option 2 via tmux
      `Down Enter` (tmux send-keys can't send single keys for Claude's
      approval navigation without Web Terminal mediation).
    - Codex: `Enter` confirms option 1 (default highlighted); `Down
      Enter` navigates to option 2 and confirms. We originally tried
      the single-key shortcuts `(y)` / `(p)` codex advertises, but
      that left the key char echoing in codex's prompt input after
      the UI dismissed. `Enter`-based navigation leaves no residue
      and mirrors the Claude Always path, so both tools share the
      same key sequence for option 2.

    The `always=True` path is guarded against stale state: if the
    session has no active `pending_always_label`, we 400. This
    protects both Claude and Codex from Telegram inline buttons
    arriving after the user already resolved the prompt manually.
    """
    conn = request.app.state.db
    session = await db.get_session(conn, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    tmux_name = session.get("tmux_session")
    if not tmux_name:
        raise HTTPException(
            status_code=400,
            detail="No tmux session associated with this session. The agent must be running inside tmux.",
        )

    if always and not session.get("pending_always_label"):
        raise HTTPException(
            status_code=400,
            detail="No active 'always' option on this session",
        )

    tool = session.get("tool") or "claude"

    if tool == "codex":
        # Option 1 is highlighted by default in both Bash and MCP
        # approval UIs; Enter confirms it.
        #
        # For Always, the key sequence depends on which UI variant
        # is up:
        # - Bash 3-option: Always is option 2 → 1 Down + Enter
        # - MCP 4-option:  Always is option 3 → 2 Downs + Enter
        #   (Allow / Allow for this session / Always allow / Cancel)
        #
        # We derive the down count from `pending_tool`, which the
        # parser set to "Bash" or "MCP" when it recognized the UI.
        # No new DB column — the discriminator is already in the
        # existing pending-state fields.
        if always:
            down_count = 2 if session.get("pending_tool") == "MCP" else 1
            keys: tuple[str, ...] = tuple(["Down"] * down_count) + ("Enter",)
        else:
            keys = ("Enter",)
        proc = await asyncio.create_subprocess_exec(
            "tmux", "send-keys", "-t", f"{tmux_name}:", *keys,
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
        return {"ok": True, "tmux_session": tmux_name, "always": always}

    if always:
        # Claude option 2: Down arrow + Enter, via direct tmux send-keys.
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
        # Claude option 1: send 'y' + Enter via Web Terminal API
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


class PinRequest(BaseModel):
    pinned: bool


@router.post("/sessions/{session_id}/pin")
async def pin_session(request: Request, session_id: str, body: PinRequest):
    """Pin or unpin a session to the main dashboard.

    Pinned idle sessions stay visible on `/` instead of only
    showing up on `/idle`. Stopped sessions cannot be pinned —
    a dead tmux means Claude is gone and the pin would be meaningless.
    Going stopped also auto-unpins (handled in update_session_status).
    """
    conn = request.app.state.db
    session = await db.get_session(conn, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if session["status"] == "stopped":
        raise HTTPException(
            status_code=400,
            detail="Cannot pin a stopped session",
        )
    await db.set_session_pinned(conn, session_id, body.pinned)
    return {"ok": True, "pinned": body.pinned}


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
