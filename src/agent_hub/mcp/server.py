from __future__ import annotations

import aiosqlite
from fastmcp import FastMCP

from agent_hub import db
from agent_hub.services.transcript_reader import summarize_transcript

mcp = FastMCP(
    "Agent Hub",
    instructions=(
        "Agent Hub tracks all Claude Code CLI sessions on this machine. "
        "Use list_sessions to see what's running, get_session for details "
        "and event timeline, search_events to find specific activity, "
        "and get_dashboard for a global overview."
    ),
)

# DB connection — set by main.py during app startup
_conn: aiosqlite.Connection | None = None


def set_db(conn: aiosqlite.Connection) -> None:
    global _conn
    _conn = conn


def _db() -> aiosqlite.Connection:
    assert _conn is not None, "MCP server DB not initialized"
    return _conn


# ── Tools ────────────────────────────────────────────────────


@mcp.tool()
async def list_sessions(status: str = "all") -> str:
    """List Claude Code sessions tracked by the Hub.

    Args:
        status: Filter by status — "active", "idle", "stopped", or "all" (default).
    """
    sessions = await db.get_sessions(
        _db(), status=status if status != "all" else None, limit=50
    )
    if not sessions:
        return "No sessions found."

    lines: list[str] = []
    for s in sessions:
        pending = f" [WAITING: {s['pending_tool']}]" if s.get("pending_tool") else ""
        model = s.get("model") or "—"
        tokens = ""
        if s.get("output_tokens"):
            tokens = f" | out:{s['output_tokens']:,} cache:{s['cache_read_tokens']:,}"
        lines.append(
            f"- [{s['status'].upper()}{pending}] {s['hostname']}:{s['cwd']} "
            f"(model: {model}{tokens}) "
            f"id:{s['session_id'][:12]} last:{s['last_seen_at'][:19]}"
        )
    return f"{len(sessions)} session(s):\n" + "\n".join(lines)


@mcp.tool()
async def get_session(session_id: str, event_limit: int = 20) -> str:
    """Get session details and recent event timeline.

    Args:
        session_id: Full or partial (prefix) session ID.
        event_limit: Number of recent events to return (default 20).
    """
    conn = _db()

    # Support partial session_id prefix matching
    session = await db.get_session(conn, session_id)
    if session is None:
        # Try prefix match
        sessions = await db.get_sessions(conn, limit=100)
        matches = [s for s in sessions if s["session_id"].startswith(session_id)]
        if len(matches) == 1:
            session = matches[0]
        elif len(matches) > 1:
            ids = [s["session_id"][:12] for s in matches]
            return f"Ambiguous session_id prefix '{session_id}'. Matches: {', '.join(ids)}"
        else:
            return f"Session '{session_id}' not found."

    sid = session["session_id"]
    pending = f"\nPending tool: {session['pending_tool']}" if session.get("pending_tool") else ""
    model = session.get("model") or "—"

    header = (
        f"Session: {session['hostname']}:{session['cwd']}\n"
        f"Status: {session['status']}{pending}\n"
        f"Model: {model}\n"
        f"ID: {sid}\n"
        f"Started: {session['started_at'][:19]}  Last active: {session['last_seen_at'][:19]}"
    )

    if session.get("output_tokens"):
        header += (
            f"\nTokens — in:{session['input_tokens']:,} out:{session['output_tokens']:,} "
            f"cache_read:{session['cache_read_tokens']:,} cache_create:{session['cache_create_tokens']:,}"
        )

    events = await db.get_session_events(conn, sid, limit=event_limit)
    if events:
        event_lines = []
        for e in events:
            ts = e["created_at"][11:19] if len(e["created_at"]) > 19 else e["created_at"]
            event_lines.append(f"  {ts}  {e['event_type']:20s}  {e.get('summary') or '—'}")
        header += "\n\nRecent events:\n" + "\n".join(event_lines)
    else:
        header += "\n\nNo events recorded."

    return header


@mcp.tool()
async def search_events(
    query: str = "",
    tool_name: str = "",
    session_id: str = "",
    limit: int = 30,
) -> str:
    """Search events by keyword in summary, tool name, or session ID.

    Args:
        query: Text to search in event summaries (fuzzy match).
        tool_name: Exact tool name filter (e.g. "Bash", "Write", "Edit").
        session_id: Limit search to a specific session.
        limit: Max results (default 30).
    """
    events = await db.search_events(
        _db(),
        query=query or None,
        tool_name=tool_name or None,
        session_id=session_id or None,
        limit=limit,
    )
    if not events:
        return "No events found."

    lines: list[str] = []
    for e in events:
        ts = e["created_at"][:19]
        sid = e["session_id"][:8]
        lines.append(f"  {ts}  [{sid}]  {e['event_type']:20s}  {e.get('summary') or '—'}")
    return f"{len(events)} event(s):\n" + "\n".join(lines)


@mcp.tool()
async def get_dashboard() -> str:
    """Get a global dashboard overview: stats, active sessions, and recent events."""
    conn = _db()
    stats = await db.get_stats(conn)
    sessions = await db.get_sessions(conn, limit=20)
    recent = await db.get_recent_events(conn, limit=10)

    lines: list[str] = [
        "=== Agent Hub Dashboard ===",
        f"Active: {stats['active_sessions']}  "
        f"Waiting: {stats['waiting_sessions']}  "
        f"Idle: {stats['idle_sessions']}  "
        f"Stopped: {stats['stopped_sessions']}  "
        f"Total events: {stats['total_events']}",
        "",
    ]

    # Active/idle sessions
    live = [s for s in sessions if s["status"] in ("active", "idle")]
    if live:
        lines.append("--- Sessions ---")
        for s in live:
            pending = f" [WAITING: {s['pending_tool']}]" if s.get("pending_tool") else ""
            model = s.get("model") or "—"
            lines.append(
                f"  {s['status']:7s}{pending}  {s['hostname']}:{s['cwd']}  "
                f"model:{model}  id:{s['session_id'][:12]}"
            )
        lines.append("")

    # Recent events
    if recent:
        lines.append("--- Recent Events ---")
        for e in recent:
            ts = e["created_at"][11:19] if len(e["created_at"]) > 19 else e["created_at"]
            sid = e["session_id"][:8]
            lines.append(f"  {ts}  [{sid}]  {e.get('summary') or '—'}")

    return "\n".join(lines)


@mcp.tool()
async def get_transcript_summary(session_id: str) -> str:
    """Read a session's transcript file and summarize recent activity.

    Shows the last user prompts, tool calls, and assistant responses
    to understand what the session is currently working on.
    Only works for local sessions that have a transcript file.

    Args:
        session_id: Full or partial (prefix) session ID.
    """
    conn = _db()

    session = await db.get_session(conn, session_id)
    if session is None:
        sessions = await db.get_sessions(conn, limit=100)
        matches = [s for s in sessions if s["session_id"].startswith(session_id)]
        if len(matches) == 1:
            session = matches[0]
        elif len(matches) > 1:
            ids = [s["session_id"][:12] for s in matches]
            return f"Ambiguous session_id prefix '{session_id}'. Matches: {', '.join(ids)}"
        else:
            return f"Session '{session_id}' not found."

    transcript_path = session.get("transcript_path")
    if not transcript_path:
        return f"Session {session['session_id'][:12]} has no transcript path (remote session?)."

    summary = summarize_transcript(transcript_path)
    if not summary:
        return f"Could not read transcript at {transcript_path}."

    model = session.get("model") or "—"
    header = (
        f"Session: {session['hostname']}:{session['cwd']}\n"
        f"Status: {session['status']}  Model: {model}\n"
        f"ID: {session['session_id']}\n\n"
    )
    return header + summary
