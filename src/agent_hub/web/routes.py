from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from agent_hub import db
from agent_hub.services.session_manager import _pane_shows_working, _tmux_capture

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
    """If this session is an active session currently running a tool
    for more than RUNNING_THRESHOLD_SECONDS *and* the tmux pane still
    shows the agent's "(N s · esc to interrupt)" marker, annotate with
    `running_tool` and `running_elapsed_label`.

    The pane-state check is the ground-truth guard: without it, a
    user interrupt (Esc / Ctrl-C) during tool execution leaves the
    last event stuck at PreToolUse forever — no PostToolUse ever
    follows — and elapsed would grow unbounded, pinning the card in
    a fake "Running Bash (7m 33s)" state indefinitely.
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

    # Ground-truth guard against stale "running" labels: if the tmux
    # pane no longer shows the "(... esc to interrupt ...)" status
    # line, the tool has stopped running (user interrupted, or the
    # agent completed its turn but never emitted a PostToolUse due
    # to how the hook is delivered in that flow). Claude and Codex
    # both emit this marker, so one check covers both tools.
    tmux_name = session.get("tmux_session")
    if tmux_name:
        panes = await _tmux_capture(tmux_name)
        if not panes or not any(_pane_shows_working(p) for p in panes):
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


async def _fetch_dashboard(conn, transferred: int) -> list[dict]:
    """Sessions visible on the main dashboard: everything active,
    plus idle sessions the user pinned so they don't fall off into
    /idle. Actives come first, then pinned idle — both sorted by
    last_seen_at DESC within their bucket."""
    actives = await db.get_sessions(
        conn, status="active", limit=100, transferred=transferred,
    )
    pinned_idle = await db.get_sessions(
        conn, status="idle", limit=100, transferred=transferred, pinned=1,
    )
    sessions = actives + pinned_idle
    for s in sessions:
        await _enrich_running(conn, s)
        s["recent_events"] = await db.get_session_events_latest(
            conn, s["session_id"], n=2
        )
    # Float waiting sessions to the top so pending approvals can't hide
    # below the fold. Python's sort is stable, so relative order within
    # the waiting and non-waiting groups is preserved (still last_seen_at
    # DESC within each bucket).
    sessions.sort(key=lambda s: 0 if s.get("pending_tool") else 1)

    # Place subagent cards directly after their parent so the
    # visual indent forms a tree. Any subagent whose parent isn't
    # in the current list falls through to the end.
    parents: list[dict] = []
    children_by_parent: dict[str, list[dict]] = {}
    orphan_subagents: list[dict] = []
    for s in sessions:
        pid = s.get("parent_session_id")
        if pid:
            children_by_parent.setdefault(pid, []).append(s)
        else:
            parents.append(s)
    parent_ids = {p["session_id"] for p in parents}
    for pid, kids in children_by_parent.items():
        if pid not in parent_ids:
            orphan_subagents.extend(kids)
    ordered: list[dict] = []
    for p in parents:
        ordered.append(p)
        ordered.extend(children_by_parent.get(p["session_id"], []))
    ordered.extend(orphan_subagents)
    return ordered


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    conn = request.app.state.db
    sessions_native = await _fetch_dashboard(conn, transferred=0)
    sessions_from_tmux = await _fetch_dashboard(conn, transferred=1)
    stats = await db.get_stats(conn)

    terminal_url = _terminal_url(request)
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "sessions_native": sessions_native,
            "sessions_from_tmux": sessions_from_tmux,
            "stats": stats,
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


@router.get("/agent", response_class=HTMLResponse)
async def agent_chat_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="agent.html",
        context={"current_page": "agent"},
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


@router.get("/partials/session-card/{session_id}")
async def session_card_partial(request: Request, session_id: str) -> dict:
    """Render a single session card as an HTML fragment.

    Used by the dashboard's WebSocket handler to insert a card for a
    session that wasn't present in the initial server-side render.
    """
    conn = request.app.state.db
    session = await db.get_session(conn, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="session not found")
    await _enrich_running(conn, session)
    session["recent_events"] = await db.get_session_events_latest(
        conn, session_id, n=2,
    )
    terminal_url = _terminal_url(request)
    html = templates.env.get_template("_card_fragment.html").render(
        s=session,
        terminal_url=terminal_url,
    )
    return {
        "html": html,
        "transferred": int(session.get("transferred", 0) or 0),
        "status": session.get("status", "active"),
    }
