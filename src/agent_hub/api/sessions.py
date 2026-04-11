from __future__ import annotations

import httpx
from fastapi import APIRouter, HTTPException, Query, Request

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


@router.delete("/sessions/{session_id}")
async def delete_session(request: Request, session_id: str):
    conn = request.app.state.db
    deleted = await db.delete_session(conn, session_id)
    if not deleted:
        raise HTTPException(
            status_code=400,
            detail="Session not found or not in stopped status",
        )
    return {"ok": True}


@router.post("/sessions/{session_id}/approve")
async def approve_session(request: Request, session_id: str):
    """Send 'y' + Enter to the tmux session where this Claude Code is running."""
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

    terminal_url = request.app.state.config.terminal_url
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{terminal_url}/api/terminals/{tmux_name}/send",
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

    return {"ok": True, "tmux_session": tmux_name}
