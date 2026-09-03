from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
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
_static_dir = Path(__file__).parent / "static"
templates = Jinja2Templates(directory=str(_template_dir))


def _basename(path: str) -> str:
    """Jinja2 filter: extract last component of a path."""
    return os.path.basename(path) or path


def _static_version() -> str:
    """Cache-busting token for /static assets: the newest mtime in the
    directory. A handful of stat calls per page render, and a deploy can
    never leave a browser on a stale hub.css."""
    try:
        return str(int(max(p.stat().st_mtime for p in _static_dir.iterdir() if p.is_file())))
    except (ValueError, OSError):
        return "0"


# Register custom filters / globals
templates.env.filters["basename"] = _basename
templates.env.globals["static_v"] = _static_version


def _terminal_url(request: Request) -> str:
    """Derive terminal URL from the host used to access the Hub."""
    host = request.headers.get("host", "localhost:7800").split(":")[0]
    port = request.app.state.config.terminal_port
    return f"http://{host}:{port}"


STOPPED_LIMIT = 100


async def _fetch_dashboard(conn) -> list[dict]:
    """Every session the sidebar shows, in one list: active, idle and the
    most recent ended ones. The browser groups them by project and sorts
    by state inside each project. Waiting sessions come first so the
    initial selection lands on the one that needs attention."""
    actives = await db.get_sessions(conn, status="active", limit=100)
    idles = await db.get_sessions(conn, status="idle", limit=100)
    stopped = await db.get_sessions(conn, status="stopped", limit=STOPPED_LIMIT)
    for s in actives:
        await _enrich_running(conn, s)
    for s in actives + idles:
        s["recent_events"] = await db.get_session_events_latest(
            conn, s["session_id"], n=1
        )
    for s in stopped:
        s["recent_events"] = []
    sessions = actives + idles + stopped
    sessions.sort(key=lambda s: 0 if s.get("pending_tool") else 1)

    # Place subagent rows directly after their parent so the visual
    # indent forms a tree. Any subagent whose parent isn't in the
    # current list falls through to the end.
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
    sessions = await _fetch_dashboard(conn)
    stats = await db.get_stats(conn)

    terminal_url = _terminal_url(request)
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "sessions": sessions,
            "stats": stats,
            "terminal_url": terminal_url,
            "current_page": "dashboard",
        },
    )


# The old list pages are part of the workspace sidebar now. Keep the
# URLs working for bookmarks and the README.
@router.get("/idle")
async def idle_sessions():
    return RedirectResponse("/", status_code=302)


@router.get("/stopped")
async def stopped_sessions():
    return RedirectResponse("/", status_code=302)


@router.get("/tmux")
async def tmux_hub():
    return RedirectResponse("/", status_code=302)


@router.get("/agent", response_class=HTMLResponse)
async def agent_chat_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="agent.html",
        context={"current_page": "agent", "terminal_url": _terminal_url(request)},
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
    """Render a single session row as an HTML fragment.

    Used by the dashboard's WebSocket handler to insert a row for a
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
