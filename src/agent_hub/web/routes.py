from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from agent_hub import db

router = APIRouter()

_template_dir = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_template_dir))


def _basename(path: str) -> str:
    """Jinja2 filter: extract last component of a path."""
    return os.path.basename(path) or path


# Register custom filters
templates.env.filters["basename"] = _basename


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    conn = request.app.state.db
    sessions = await db.get_sessions(conn, limit=100)
    stats = await db.get_stats(conn)

    # Get recent events across all sessions
    cursor = await conn.execute(
        "SELECT id, event_uid, session_id, event_type, tool_name, summary, created_at "
        "FROM events ORDER BY created_at DESC LIMIT 20"
    )
    rows = await cursor.fetchall()
    recent_events = [dict(r) for r in rows]

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "sessions": sessions,
            "stats": stats,
            "recent_events": recent_events,
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

    events = await db.get_session_events(conn, session_id, limit=limit, offset=offset)

    return templates.TemplateResponse(
        request=request,
        name="session.html",
        context={
            "session": session,
            "events": events,
            "limit": limit,
            "offset": offset,
        },
    )
