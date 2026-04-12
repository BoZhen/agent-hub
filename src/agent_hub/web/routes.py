from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from agent_hub import db

RUNNING_THRESHOLD_SECONDS = 30


def _format_elapsed(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m}m {s}s"
    h, m = divmod(m, 60)
    return f"{h}h {m}m"


async def _enrich_running(conn, session: dict) -> dict:
    """If this session is an active Claude session currently running a
    tool for more than RUNNING_THRESHOLD_SECONDS, annotate it with
    `running_tool` (tool name) and `running_elapsed_label` (e.g. \"2m 15s\").
    """
    if session.get("status") != "active":
        return session
    if session.get("pending_tool"):
        return session
    last = await db.get_last_event(conn, session["session_id"])
    if not last or last.get("event_type") != "PreToolUse":
        return session
    created_raw = last.get("created_at")
    if not created_raw:
        return session
    try:
        created = datetime.fromisoformat(created_raw)
    except ValueError:
        return session
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    elapsed = (datetime.now(timezone.utc) - created).total_seconds()
    if elapsed < RUNNING_THRESHOLD_SECONDS:
        return session
    session["running_tool"] = last.get("tool_name") or "Tool"
    session["running_elapsed_label"] = _format_elapsed(int(elapsed))
    return session

router = APIRouter()

_template_dir = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_template_dir))


def _basename(path: str) -> str:
    """Jinja2 filter: extract last component of a path."""
    return os.path.basename(path) or path


# Register custom filters
templates.env.filters["basename"] = _basename


def _terminal_url(request: Request) -> str:
    """Derive terminal URL from the host used to access the Hub."""
    host = request.headers.get("host", "localhost:7800").split(":")[0]
    port = request.app.state.config.terminal_port
    return f"http://{host}:{port}"


async def _fetch_active(conn, transferred: int) -> list[dict]:
    sessions = await db.get_sessions(
        conn, status="active", limit=100, transferred=transferred,
    )
    for s in sessions:
        await _enrich_running(conn, s)
    return sessions


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    conn = request.app.state.db
    sessions_native = await _fetch_active(conn, transferred=0)
    sessions_from_tmux = await _fetch_active(conn, transferred=1)
    stats = await db.get_stats(conn)

    # Get recent events across all sessions
    cursor = await conn.execute(
        "SELECT id, event_uid, session_id, event_type, tool_name, summary, created_at "
        "FROM events ORDER BY created_at DESC LIMIT 20"
    )
    rows = await cursor.fetchall()
    recent_events = [dict(r) for r in rows]

    terminal_url = _terminal_url(request)
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "sessions_native": sessions_native,
            "sessions_from_tmux": sessions_from_tmux,
            "stats": stats,
            "recent_events": recent_events,
            "terminal_url": terminal_url,
            "current_page": "dashboard",
        },
    )


@router.get("/stopped", response_class=HTMLResponse)
async def stopped_sessions(request: Request):
    conn = request.app.state.db
    sessions = await db.get_sessions(conn, status="stopped", limit=200)
    terminal_url = _terminal_url(request)
    return templates.TemplateResponse(
        request=request,
        name="stopped.html",
        context={
            "sessions": sessions,
            "terminal_url": terminal_url,
            "current_page": "stopped",
        },
    )


@router.get("/idle", response_class=HTMLResponse)
async def idle_sessions(request: Request):
    conn = request.app.state.db
    sessions = await db.get_sessions(conn, status="idle", limit=200)
    terminal_url = _terminal_url(request)
    return templates.TemplateResponse(
        request=request,
        name="idle.html",
        context={
            "sessions": sessions,
            "terminal_url": terminal_url,
            "current_page": "idle",
        },
    )


@router.get("/tmux", response_class=HTMLResponse)
async def tmux_hub(request: Request):
    terminal_url = _terminal_url(request)
    return templates.TemplateResponse(
        request=request,
        name="tmux_hub.html",
        context={
            "terminal_url": terminal_url,
            "current_page": "tmux",
        },
    )


@router.get("/sessions/{session_id}", response_class=HTMLResponse)
async def session_detail(
    request: Request,
    session_id: str,
    limit: int = 100,
    offset: int = 0,
):
    conn = request.app.state.db
    session = await db.get_session(conn, session_id)
    if not session:
        return HTMLResponse("<h1>Session not found</h1>", status_code=404)
    await _enrich_running(conn, session)

    events = await db.get_session_events(conn, session_id, limit=limit, offset=offset)

    terminal_url = _terminal_url(request)
    return templates.TemplateResponse(
        request=request,
        name="session.html",
        context={
            "session": session,
            "events": events,
            "limit": limit,
            "offset": offset,
            "terminal_url": terminal_url,
            "current_page": "dashboard",
        },
    )
