from __future__ import annotations

import asyncio
import logging
import re as _re
import socket
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import aiosqlite

from agent_hub import db
from agent_hub.api.ws import broadcaster
from agent_hub.services.pane_pipe import get_pipe_manager
from agent_hub.services.telegram_bot import (
    cancel_pending as tg_cancel_pending,
    notify_pending as tg_notify_pending,
)

logger = logging.getLogger(__name__)

_LOCAL_HOSTNAME = socket.gethostname()


# Tmux sessions launched by Hub's `/api/tmux/new` with `command=claude`
# are never "transferred" — they're first-class Claude sessions. We
# track them here so `_detect_transferred` can skip the timing heuristic
# (claude's workspace-trust prompt can delay SessionStart past the 5s
# threshold, otherwise misclassifying them as From Tmux).
_HUB_LAUNCHED_TMUX: dict[str, tuple[float, str]] = {}
_HUB_LAUNCHED_TTL = 120.0


def mark_hub_launched(tmux_name: str, command: str = "") -> None:
    now = time.time()
    for k in [k for k, (ts, _) in _HUB_LAUNCHED_TMUX.items() if now - ts > _HUB_LAUNCHED_TTL]:
        _HUB_LAUNCHED_TMUX.pop(k, None)
    _HUB_LAUNCHED_TMUX[tmux_name] = (now, command)


# Suppression table for Hub-initiated approvals: when the user clicks
# Approve/Always, `approve_session` sends keys to tmux and records the
# approved signature here. The periodic pending-check then skips
# broadcasting if the pane still shows this exact signature within the
# grace window — covers the gap between "Hub sent keys" and "claude/codex
# actually dismissed the approval UI on the pane". Populated by approve
# handler, consumed (and popped) by `periodic_pending_check`.
#
# sid -> ((pending_tool, pending_detail, pending_always_label), until_ts)
_APPROVED_SUPPRESS: dict[str, tuple[tuple[Any, Any, Any], float]] = {}
_APPROVED_SUPPRESS_GRACE_S = 3.0


def mark_approved_suppress(
    session_id: str,
    pending_tool: Any,
    pending_detail: Any,
    pending_always_label: Any,
) -> None:
    """Record that the Hub just approved the given signature. Used by
    `periodic_pending_check` to avoid re-broadcasting the same approval
    before claude/codex has dismissed it from the pane."""
    _APPROVED_SUPPRESS[session_id] = (
        (pending_tool, pending_detail, pending_always_label),
        time.time() + _APPROVED_SUPPRESS_GRACE_S,
    )


_SUBAGENT_START_WINDOW_S = 120.0
_SUBAGENT_SPAWN_EVENT_WINDOW_S = 60.0


async def _match_subagent_parent(
    conn: aiosqlite.Connection,
    *,
    candidate_ids: list[str],
) -> str | None:
    """Pick the parent session_id from ``candidate_ids`` if the caller
    looks like a subagent spawn of one of them.

    The primary signal: a candidate is currently blocked **inside** a
    Task/Agent tool call. That means its most recent Task/Agent
    PreToolUse has no matching PostToolUse — the tool hasn't returned
    yet because the subagent (us) is just now firing its first event.
    This is what distinguishes a subagent spawn from a ``/clear`` — in
    the /clear case the Task tool already completed (or never fired),
    so the PreToolUse-without-PostToolUse guard fails.

    UUIDv7 prefix matching is NOT required: legacy Claude sessions still
    carry v4 session_ids while the Task tool spawns v7 ones, and we
    want detection to work across that boundary.
    """
    if not candidate_ids:
        return None

    now = datetime.now(timezone.utc)
    event_cutoff = (now - timedelta(seconds=_SUBAGENT_SPAWN_EVENT_WINDOW_S)).isoformat()
    placeholders = ",".join("?" * len(candidate_ids))
    cursor = await conn.execute(
        f"""
        SELECT s.session_id
        FROM sessions s
        WHERE s.session_id IN ({placeholders})
          AND s.status IN ('active', 'idle')
          AND EXISTS (
            SELECT 1 FROM events e1
            WHERE e1.session_id = s.session_id
              AND e1.event_type = 'PreToolUse'
              AND e1.tool_name IN ('Task', 'Agent')
              AND e1.created_at >= ?
              AND NOT EXISTS (
                SELECT 1 FROM events e2
                WHERE e2.session_id = s.session_id
                  AND e2.event_type IN ('PostToolUse', 'PostToolUseFailure')
                  AND e2.tool_name IN ('Task', 'Agent')
                  AND e2.created_at > e1.created_at
              )
          )
        ORDER BY s.started_at DESC
        LIMIT 1
        """,
        (*candidate_ids, event_cutoff),
    )
    row = await cursor.fetchone()
    return row["session_id"] if row else None


async def maybe_late_assign_subagent_parent(
    conn: aiosqlite.Connection,
    *,
    session_id: str,
    tmux_session: str | None,
    hub_id: str,
    hostname: str,
) -> str | None:
    """Late-pass subagent detection run from the event pipeline.

    On a fresh session the first event sometimes arrives before the
    parent's `Task` PreToolUse has been written to DB (brief race
    between two concurrent hook HTTP calls). This function re-runs
    detection on every non-creation event while the session is still
    recent and has no parent yet, so we catch the assignment as soon
    as the parent's Task event lands.
    """
    if not tmux_session:
        return None
    row = await db.get_session(conn, session_id)
    if not row or row.get("parent_session_id"):
        return None
    started_raw = row.get("started_at")
    if started_raw:
        try:
            started = datetime.fromisoformat(started_raw)
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - started).total_seconds() > _SUBAGENT_START_WINDOW_S:
                return None
        except ValueError:
            pass
    cursor = await conn.execute(
        "SELECT session_id FROM sessions "
        "WHERE hub_id = ? AND hostname = ? AND tmux_session = ? "
        "AND session_id != ? AND status IN ('active', 'idle')",
        (hub_id, hostname, tmux_session, session_id),
    )
    candidates = [r["session_id"] for r in await cursor.fetchall()]
    parent_id = await _match_subagent_parent(
        conn, candidate_ids=candidates
    )
    if parent_id:
        await db.set_session_parent(conn, session_id, parent_id)
        logger.info(
            "Late-assigned subagent parent: %s → parent=%s (tmux=%s)",
            session_id, parent_id, tmux_session,
        )
    return parent_id


async def ensure_session(
    conn: aiosqlite.Connection,
    *,
    session_id: str,
    hub_id: str,
    hostname: str,
    cwd: str,
    transcript_path: str | None = None,
    tmux_session: str | None = None,
    payload: dict[str, Any],
    tool: str = "claude",
) -> None:
    """Create session if not exists, or reactivate on resume.

    On new-session creation, detect whether this Claude session was
    started inside a pre-existing bare tmux (tmux older than ~30s at
    the moment of the SessionStart hook). If so, mark transferred=1
    so the dashboard can file it under the "From Tmux" tab.

    ``tool`` identifies the CLI driver ("claude" or "codex"). Codex
    sessions skip the workspace-trust timing heuristic because codex
    has no equivalent prompt.
    """
    event_type = payload.get("hook_event_name", "")
    model = payload.get("model")  # Accept model from any event type

    existing = await db.get_session(conn, session_id)

    if existing is None:
        transferred = 0
        parent_session_id: str | None = None
        if tmux_session:
            hub_entry = _HUB_LAUNCHED_TMUX.get(tmux_session)
            if hub_entry is not None:
                _, hub_command = hub_entry
                if hub_command:
                    tool = hub_command

            # Look for an in-flight predecessor in this tmux BEFORE
            # running the timing heuristic. An active/idle row with
            # the same tmux_session means this SessionStart is a
            # `/clear` (new session_id, same tmux) or a resume — in
            # either case we want to inherit the predecessor's
            # `transferred` flag instead of re-running the 5s
            # heuristic, which would spuriously flip a first-class
            # session into "From Tmux" just because tmux is now
            # hours old.
            cursor = await conn.execute(
                "SELECT session_id, transferred FROM sessions "
                "WHERE tmux_session = ? AND session_id != ? "
                "AND status IN ('active', 'idle')",
                (tmux_session, session_id),
            )
            orphan_rows = list(await cursor.fetchall())

            if orphan_rows:
                # Subagent check before orphan retirement: if one of
                # the same-tmux candidates is currently blocked inside
                # a Task/Agent tool call (PreToolUse without matching
                # PostToolUse), this is a Task-tool subagent spawn —
                # link to parent instead of retiring it.
                parent_session_id = await _match_subagent_parent(
                    conn,
                    candidate_ids=[r["session_id"] for r in orphan_rows],
                )
                # Inherit the predecessor's transferred flag whether
                # this is a subagent or a /clear reuse — in both cases
                # the new session lives inside the same tmux lineage.
                transferred = int(orphan_rows[0]["transferred"] or 0)
            elif tool != "codex":
                # Fresh tmux (no same-tmux predecessor) — run the
                # Claude workspace-trust prompt delay heuristic.
                # Codex has no such prompt, so codex hook arrivals
                # stay first-class regardless of tmux age.
                transferred = await _detect_transferred(tmux_session)

            # Retire the orphans only if this is NOT a subagent spawn.
            # Subagent parents must keep running alongside the child.
            if orphan_rows and not parent_session_id:
                mgr = get_pipe_manager()
                for row in orphan_rows:
                    orphan_id = row["session_id"]
                    await db.update_session_status(conn, orphan_id, "stopped")
                    await db.update_session_pending_tool(conn, orphan_id, None, None)
                    if mgr is not None:
                        await mgr.detach(orphan_id)
                logger.info(
                    "Retired %d older session(s) bound to reused tmux %s",
                    len(orphan_rows), tmux_session,
                )
            elif parent_session_id:
                logger.info(
                    "Detected subagent spawn: %s → parent=%s (tmux=%s)",
                    session_id, parent_session_id, tmux_session,
                )
        await db.upsert_session(
            conn,
            session_id=session_id,
            hub_id=hub_id,
            hostname=hostname,
            cwd=cwd,
            model=model,
            status="active",
            transcript_path=transcript_path,
            transferred=transferred,
            tmux_session=tmux_session,
            tool=tool,
        )
        if parent_session_id:
            await db.set_session_parent(conn, session_id, parent_session_id)
        # Attach push-based pane observability for this fresh active
        # session. No-op if no manager is registered (e.g. during
        # tests). Failure to attach falls through to the polling-only
        # fallback in periodic_pending_check.
        mgr = get_pipe_manager()
        if mgr is not None and tmux_session:
            await mgr.attach(session_id, tmux_session)
    elif event_type == "SessionStart":
        # Resume — preserve existing transferred flag (upsert doesn't
        # modify it on conflict).
        await db.upsert_session(
            conn,
            session_id=session_id,
            hub_id=hub_id,
            hostname=hostname,
            cwd=cwd,
            model=model,
            status="active",
            transcript_path=transcript_path,
            tool=tool,
        )
        mgr = get_pipe_manager()
        if mgr is not None and tmux_session:
            await mgr.attach(session_id, tmux_session)
    elif existing.get("model") is None and model:
        # Backfill model if we learn it from a later event
        await db.upsert_session(
            conn,
            session_id=session_id,
            hub_id=hub_id,
            hostname=hostname,
            cwd=cwd,
            model=model,
            transcript_path=transcript_path,
            tool=tool,
        )


async def _detect_transferred(
    tmux_name: str, threshold_seconds: int = 5
) -> int:
    """Return 1 if the tmux session was created well before now — i.e.
    the user had a bare tmux running and then started Claude inside it.
    Returns 0 for fresh tmux sessions or on any error.

    Threshold rationale: ai-tmux creates tmux and launches Claude in a
    single command, so SessionStart fires within ~1-2s of tmux creation.
    Manual flow (create bare tmux -> attach -> type `claude`) takes >=5s
    minimum. 5s separates the two reliably.

    Hub-launched sessions (via /api/tmux/new command=claude) bypass the
    timing heuristic entirely — they may get stuck on the workspace-trust
    prompt and exceed the threshold even though they're first-class.
    """
    if _HUB_LAUNCHED_TMUX.pop(tmux_name, None) is not None:
        return 0
    proc = await asyncio.create_subprocess_exec(
        "tmux", "display-message", "-t", f"{tmux_name}:",
        "-p", "#{session_created}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        return 0
    try:
        tmux_created = int(stdout.decode().strip())
    except ValueError:
        return 0
    import time
    return 1 if time.time() - tmux_created > threshold_seconds else 0


async def update_session_activity(
    conn: aiosqlite.Connection, session_id: str
) -> None:
    await db.update_session_activity(conn, session_id)
    # Any hook event that lands here may have just promoted an idle
    # session back to active (db.update_session_activity unconditionally
    # sets status='active'). Push observability needs the pipe attached
    # in that case — `ensure_session` only attaches on SessionStart, so
    # for other event types this is the only path that reaches the
    # newly-active session.
    mgr = get_pipe_manager()
    if mgr is None or mgr.is_attached(session_id):
        return
    session = await db.get_session(conn, session_id)
    if session and session.get("tmux_session"):
        await mgr.attach(session_id, session["tmux_session"])


async def mark_session_idle(
    conn: aiosqlite.Connection, session_id: str
) -> None:
    await db.update_session_status(conn, session_id, "idle")
    await db.update_session_pending_tool(conn, session_id, None, None)
    mgr = get_pipe_manager()
    if mgr is not None:
        await mgr.detach(session_id)


_INTERRUPT_RE = None  # lazy-compiled regex (see _pane_shows_working)


def _pane_shows_working(pane_text: str) -> bool:
    """Return True if a TUI status line near the pane bottom shows the
    agent is actively running a tool / thinking / streaming.

    Both Claude and Codex emit a parenthesised \"(... esc to interrupt ...)\"
    hint in their status line (Claude: \"(3s esc to interrupt)\", Codex:
    \"(11s · esc to interrupt)\"). The parentheses requirement makes this
    robust against free text that merely mentions the phrase.
    """
    global _INTERRUPT_RE
    if _INTERRUPT_RE is None:
        import re
        _INTERRUPT_RE = re.compile(r"\([^)]*esc to interrupt[^)]*\)")
    lines = pane_text.splitlines()
    tail = "\n".join(lines[-10:])
    return bool(_INTERRUPT_RE.search(tail))


async def _is_claude_thinking(tmux_name: str) -> bool:
    pane_text = await _tmux_capture(tmux_name)
    if pane_text is None:
        return False
    return _pane_shows_working(pane_text)


async def _soft_idle_pass(
    conn: aiosqlite.Connection, cutoff_minutes: int
) -> int:
    """Demote 'active' sessions to 'idle' when they've had no events
    for cutoff_minutes, except when Claude is still working.

    A session is preserved as active when any of these holds:
    - Last event is PreToolUse (a tool is currently running).
    - The pane shows Claude's \"esc to interrupt\" status line
      (thinking, streaming, or running a tool with UI output).
    """
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=cutoff_minutes)
    cursor = await conn.execute(
        "SELECT session_id, tmux_session FROM sessions "
        "WHERE status = 'active' AND last_seen_at < ?",
        (cutoff.isoformat(),),
    )
    rows = await cursor.fetchall()
    marked = 0
    mgr = get_pipe_manager()
    for row in rows:
        sid = row["session_id"]
        tmux_name = row["tmux_session"]
        last = await db.get_last_event(conn, sid)
        if last and last.get("event_type") == "PreToolUse":
            continue  # tool still running
        if tmux_name and await _is_claude_thinking(tmux_name):
            continue  # pane shows Claude is actively working
        await db.update_session_status(conn, sid, "idle")
        await db.update_session_pending_tool(conn, sid, None, None)
        if mgr is not None:
            await mgr.detach(sid)
        marked += 1
    if marked:
        logger.info("Soft-idle: demoted %d active sessions to idle", marked)
    return marked


async def _list_alive_tmux_names() -> set[str]:
    """Return names of currently alive tmux sessions. Empty set if
    tmux server is down or has no sessions (both mean 'no sessions
    alive' from our perspective)."""
    proc = await asyncio.create_subprocess_exec(
        "tmux", "ls", "-F", "#{session_name}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        return set()
    return {
        line.strip()
        for line in stdout.decode("utf-8", errors="replace").splitlines()
        if line.strip()
    }


# ── Codex discovery ─────────────────────────────────────────────
#
# Codex has no hook system like Claude. We discover codex sessions by
# scanning tmux panes for its TUI signature. Activity is measured by
# pane-content hash diff — if the pane has changed since the last tick,
# we touch last_seen_at and reactivate any idle session. Claude uses
# hook events for the same purpose; codex gets the same effect via
# periodic pane snapshots. _soft_idle_pass (tool-agnostic) handles the
# reverse when a codex pane truly sits still for 10 min.

_CODEX_MODEL_RE = _re.compile(r"(gpt-[\w.-]+codex(?:\s+\w+)?)")
_CODEX_PANE_HASH: dict[str, str] = {}


def _hash_pane(text: str) -> str:
    import hashlib
    return hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()


def _is_codex_pane(pane_text: str) -> bool:
    """Detect if a tmux pane is running Codex TUI.

    Two independent signals; either is sufficient. The welcome box
    (`>_ OpenAI Codex (vX.Y.Z)`) is visible while the pane hasn't
    scrolled past it. The status line — `gpt-X-codex ... · weekly N%`
    — is always anchored at the bottom. Claude panes don't carry a
    `weekly` token, so its presence in the tail plus any mention of
    `codex` is a reliable fingerprint.
    """
    if "OpenAI Codex" in pane_text:
        return True
    lines = pane_text.splitlines()
    tail = "\n".join(lines[-8:])
    return "weekly" in tail and "codex" in tail.lower()


def _extract_codex_model(pane_text: str) -> str | None:
    m = _CODEX_MODEL_RE.search(pane_text)
    return m.group(1).strip()[:80] if m else None


async def _tmux_session_info(name: str) -> tuple[str | None, int | None]:
    """Return (cwd, created_unix_ts) for a tmux session."""
    proc = await asyncio.create_subprocess_exec(
        "tmux", "display-message", "-t", f"{name}:",
        "-p", "#{session_path}\t#{session_created}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        return (None, None)
    parts = stdout.decode().strip().split("\t")
    if len(parts) != 2:
        return (None, None)
    try:
        return (parts[0] or None, int(parts[1]))
    except ValueError:
        return (parts[0] or None, None)


async def _claim_for_tmux(
    conn: aiosqlite.Connection, name: str
) -> dict[str, Any] | None:
    """Newest active/idle session bound to this tmux, or None.

    Newest-first matters when a parent + subagent share a tmux: the
    subagent owns the pane-activity bump, the parent will soft-idle.
    """
    cursor = await conn.execute(
        "SELECT session_id, status, tool FROM sessions "
        "WHERE tmux_session = ? AND status IN ('active', 'idle') "
        "ORDER BY started_at DESC LIMIT 1",
        (name,),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def discover_codex_for_tmux(
    conn: aiosqlite.Connection,
    hub_id: str,
    hostname: str,
    name: str,
) -> bool:
    """Run the codex-discovery logic for a single tmux name.

    Shared by:
    - Push: ``/api/internal/tmux-discovered`` endpoint, called from a
      ``session-created`` hook fired by tmux when a new session opens.
    - Polling fallback: ``periodic_codex_discovery`` calls this for
      every alive tmux every 60s.

    Returns True if a NEW codex session row was inserted; False if the
    tmux belongs to a Claude session, isn't a codex pane, was already
    claimed, or only a touch/reactivate happened.
    """
    existing = await _claim_for_tmux(conn, name)
    if existing and existing.get("tool") != "codex":
        # Claude tmux — leave it alone.
        return False

    pane = await _tmux_capture(name)
    if pane is None:
        return False

    # For brand-new tmux (not yet in DB) we need the codex signature
    # to decide whether this is even a codex session. Once recorded in
    # DB as codex we trust that: the welcome-box / `weekly` status-line
    # signature can scroll off in narrow panes with a tall approval UI,
    # and we still want to track activity in that state.
    if existing is None and not _is_codex_pane(pane):
        return False

    new_hash = _hash_pane(pane)

    if existing is None:
        # Reverse race guard: a hook-path codex session may have been
        # inserted between _claim_for_tmux and now.
        pre = await conn.execute(
            "SELECT 1 FROM sessions "
            "WHERE tool='codex' AND tmux_session=? "
            "AND status IN ('active','idle') LIMIT 1",
            (name,),
        )
        if await pre.fetchone():
            return False

        cwd, created_ts = await _tmux_session_info(name)
        if not cwd or not created_ts:
            return False
        session_id = f"codex-{name}-{created_ts}"
        model = _extract_codex_model(pane)
        await db.upsert_session(
            conn,
            session_id=session_id,
            hub_id=hub_id,
            hostname=hostname,
            cwd=cwd,
            model=model,
            status="active",
            tmux_session=name,
            tool="codex",
        )
        _CODEX_PANE_HASH[session_id] = new_hash
        mgr = get_pipe_manager()
        if mgr is not None:
            await mgr.attach(session_id, name)
        logger.info(
            "Discovered codex session: tmux=%s → %s (model=%s)",
            name, session_id, model,
        )
        return True

    sid = existing["session_id"]
    old_hash = _CODEX_PANE_HASH.get(sid)
    if new_hash != old_hash:
        _CODEX_PANE_HASH[sid] = new_hash
        if existing["status"] == "idle":
            await db.update_session_status(conn, sid, "active")
            mgr = get_pipe_manager()
            if mgr is not None:
                await mgr.attach(sid, name)
            logger.info("Reactivated codex session %s (pane changed)", sid)
        await db.touch_session(conn, sid)
    return False


async def _discover_codex_tmux(
    conn: aiosqlite.Connection, hub_id: str, hostname: str
) -> int:
    """Slow-path codex discovery: scan every alive tmux pane.

    Used as the polling fallback behind the push hook (``session-created``
    → ``/api/internal/tmux-discovered``). Push handles new tmuxes within
    ~50 ms of creation; this loop catches anything push misses (hub down
    when tmux was created, hook unset, etc.).
    """
    alive = await _list_alive_tmux_names()
    if not alive:
        return 0
    created = 0
    for name in alive:
        if await discover_codex_for_tmux(conn, hub_id, hostname, name):
            created += 1
    return created


async def _sweep_dead_tmux(conn: aiosqlite.Connection) -> int:
    """Mark active/idle sessions as stopped when their tmux is gone.

    Runs before the soft-idle/stale passes so that sessions whose tmux
    was killed (typically when the user exits Claude and its root-pane
    tmux dies) flip to stopped within one sweep cycle instead of
    waiting for the 30-minute stale cutoff.
    """
    alive = await _list_alive_tmux_names()
    cursor = await conn.execute(
        "SELECT session_id, tmux_session FROM sessions "
        "WHERE status IN ('active', 'idle') AND tmux_session IS NOT NULL"
    )
    rows = await cursor.fetchall()
    marked = 0
    mgr = get_pipe_manager()
    for row in rows:
        if row["tmux_session"] not in alive:
            sid = row["session_id"]
            await db.update_session_status(conn, sid, "stopped")
            await db.update_session_pending_tool(conn, sid, None, None)
            if mgr is not None:
                await mgr.detach(sid)
            marked += 1
    if marked:
        logger.info("Dead-tmux sweep: marked %d sessions stopped", marked)
    return marked


async def sweep_stale_sessions(
    conn: aiosqlite.Connection,
    soft_idle_minutes: int = 10,
) -> int:
    """Two-pass sweep run once per tick.

    1. Dead-tmux → stopped. This is the **only** path to 'stopped' —
       an idle session stays idle indefinitely as long as its tmux is
       alive, so you can resume it days later.
    2. Soft-idle: demote stale active → idle unless Claude is still
       working (PreToolUse in flight or pane shows 'esc to interrupt').
    """
    swept = await _sweep_dead_tmux(conn)
    await _soft_idle_pass(conn, soft_idle_minutes)
    return swept


async def periodic_sweep(conn: aiosqlite.Connection) -> None:
    """Background task: sweep every 60 seconds."""
    while True:
        try:
            await asyncio.sleep(60)
            await sweep_stale_sessions(conn)
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Error in session sweep")


async def _tmux_capture(name: str) -> str | None:
    """Capture tmux pane content. Returns text or None if unavailable."""
    proc = await asyncio.create_subprocess_exec(
        "tmux", "capture-pane", "-t", f"{name}:", "-p",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        return None
    return stdout.decode("utf-8", errors="replace")


_APPROVAL_PATTERNS = [
    "Do you want to proceed?",
    "Do you want to make this edit",
    "Do you want to execute",
    "Do you want to run",
    "Do you want to create",
    "Do you want to delete",
    "Do you want to write",
]

# Tool preview box header → canonical tool name. Matched as a fallback
# when the prompt is generic ("Do you want to proceed?") and the command
# is too long to fit in the 12-line backwards scan window.
_APPROVAL_HEADERS = {
    "Bash command": "Bash",
    "Edit file": "Edit",
    "Write file": "Write",
    "Create file": "Write",
    "Read file": "Read",
    "Delete file": "Delete",
}


_BOX_VERTICAL = "\u2502"   # │
_SELECTOR = "\u276f"       # ❯

_SELECTOR_OPTION_RE = _re.compile(_SELECTOR + r"\s+1\.\s+Yes")
_OPTION2_RE = _re.compile(r"^\s*2\.\s+(Yes.*?)\s*$")


def _parse_approval_prompt(pane_text: str) -> tuple[str, str, str | None] | None:
    """Parse Claude Code approval prompt from tmux pane text.

    The approval UI's first option is always "❯ 1. Yes", rendered either
    inside a bordered box or as plain text depending on the specific
    tool/warning. We use three structural signals to avoid false
    positives from pane text that merely quotes approval phrases:

    1. A line matching "❯ 1. Yes" — selector + fixed option structure.
       Robust against free text that happens to contain either piece.
    2. The selector is near the bottom of the pane (active UI state).
    3. An approval question pattern within ~15 lines above the selector.

    Returns (tool_name, detail, always_label) or None. `always_label` is
    the full text of option 2 ("Yes, ...") if the prompt has three
    options, else None. Claude Code's option 2 scope varies per tool
    (path whitelist, command prefix, or session-wide allow) — we store
    the verbatim text so the UI can show the user what they're actually
    agreeing to before clicking Always.
    """
    lines = pane_text.splitlines()
    total = len(lines)
    if total == 0:
        return None

    # Signal 1: bottom-most line matching "❯ 1. Yes". Works for both
    # boxed ("│ ❯ 1. Yes │") and unboxed (" ❯ 1. Yes") renderings.
    selector_idx = None
    for i in range(total - 1, -1, -1):
        if _SELECTOR_OPTION_RE.search(lines[i]):
            selector_idx = i
            break

    if selector_idx is None:
        return None

    # Signal 2: active prompt is always near the bottom. Reject selectors
    # that are far from the active input position.
    if selector_idx < total - 30:
        return None

    # Signal 3: find the question line within 15 lines above the selector.
    prompt_idx = None
    for i in range(max(0, selector_idx - 15), selector_idx):
        if any(p in lines[i] for p in _APPROVAL_PATTERNS):
            prompt_idx = i
            break

    if prompt_idx is None:
        return None

    # Extract option 2 verbatim. Starts at selector_idx + 1 (line right
    # after "❯ 1. Yes"). Two-option prompts have "2. No" which won't
    # match `r"2\.\s+Yes"`, so always_label stays None for those.
    always_label: str | None = None
    for i in range(selector_idx + 1, min(total, selector_idx + 10)):
        stripped = lines[i].strip().strip(_BOX_VERTICAL).strip()
        m = _OPTION2_RE.match(stripped)
        if m:
            always_label = m.group(1).strip()[:200]
            break

    # Regex match the prompt area (box borders stripped) for tool info.
    import re
    prompt_area = " ".join(
        l.strip().strip(_BOX_VERTICAL).strip()
        for l in lines[max(0, prompt_idx - 2):prompt_idx + 2]
        if l.strip()
    )

    m = re.search(r"make this edit to (.+?)\?", prompt_area)
    if m:
        return ("Edit", m.group(1).strip()[:150], always_label)

    m = re.search(r"execute (.+?)\?", prompt_area)
    if m:
        return ("Bash", m.group(1).strip()[:150], always_label)

    m = re.search(r"create (.+?)\?", prompt_area)
    if m:
        return ("Write", m.group(1).strip()[:150], always_label)

    m = re.search(r"delete (.+?)\?", prompt_area)
    if m:
        return ("Delete", m.group(1).strip()[:150], always_label)

    # Header-based detection: scan up to 25 lines above prompt for a
    # tool preview box header (e.g. "Bash command", "Edit file"). The
    # header sits at the top of the preview, which may be beyond the
    # 12-line backwards scan when the command body is long (e.g. a
    # multi-line Python script passed to bash -c).
    header_idx = None
    header_tool = None
    for i in range(max(0, prompt_idx - 25), prompt_idx):
        stripped = lines[i].strip().strip(_BOX_VERTICAL).strip()
        name = _APPROVAL_HEADERS.get(stripped)
        if name:
            header_idx = i
            header_tool = name
            break

    if header_idx is not None and header_tool is not None:
        # First meaningful content line after header is the detail.
        for j in range(header_idx + 1, prompt_idx):
            stripped = lines[j].strip().strip(_BOX_VERTICAL).strip()
            if not stripped or all(c in "\u256c\u2500\u2501\u2550" for c in stripped):
                continue
            if "evaluates arguments as shell code" in stripped:
                continue
            return (header_tool, stripped[:150], always_label)
        return (header_tool, "", always_label)

    # Scan backwards from prompt for tool info (inside the box — strip
    # box vertical borders before matching).
    detail = ""
    for line in reversed(lines[max(0, prompt_idx - 12):prompt_idx]):
        stripped = line.strip().strip(_BOX_VERTICAL).strip()
        if not stripped or all(c in "\u256c\u2500\u2501\u2550" for c in stripped):
            continue
        if "(MCP)" in stripped:
            return ("MCP", stripped[:150], always_label)
        if stripped.startswith("$ "):
            return ("Bash", stripped[2:150], always_label)
        for t in ("Read", "Write", "Edit", "Glob", "Grep", "Agent", "WebSearch", "WebFetch"):
            if stripped.startswith(t + "(") or stripped.startswith(t + " "):
                return (t, stripped[:150], always_label)
        if not detail:
            detail = stripped

    return ("Tool", detail[:150], always_label)


# ── Codex approval parser ───────────────────────────────────────
#
# Codex's approval UI is structurally parallel to Claude's but uses
# different characters and phrasing. Key differences:
#   - Selector char `›` (U+203A) vs Claude's `❯` (U+276F)
#   - Question "Would you like to run the following command?"
#   - Command shown after a `$ ` prefix (may span continuation lines)
#   - Single-key shortcuts `(y)` / `(p)` / `(esc)` next to each option
#   - Option 2 (when present) is "Yes, and don't ask again for
#     commands that start with `<prefix>`" — a command-prefix allowlist
#
# We require three signals to match before reporting pending:
#   1. `› 1. Yes` anchor in the last 12 lines
#   2. `Press enter to confirm or esc to cancel` footer in same window
#   3. One of the `_CODEX_QUESTION_PATTERNS` phrases within 15 lines
#      above the selector
# The 12-line tail is tighter than Claude's 30 because the codex
# approval block is small and a stale prompt would otherwise risk
# matching from a few lines up.

_CODEX_SELECTOR = "\u203a"  # ›
# Selector anchor: "› 1. Yes" (Bash approval) or "› 1. Allow" (MCP
# tool approval). Both codex approval UIs use the same selector glyph
# but different first-option labels — Phase 3 loosens to cover both.
_CODEX_SELECTOR_OPTION_RE = _re.compile(_CODEX_SELECTOR + r"\s+1\.\s+(Yes|Allow)")
# Footer alternation: Bash uses "Press enter to confirm or esc to
# cancel", MCP uses "enter to submit | esc to cancel".
_CODEX_FOOTER_RE = _re.compile(
    r"(Press enter to confirm or esc to cancel|enter to submit \| esc to cancel)"
)
# Option 2 (Bash Always variant, "Yes, and don't ask again..." /
# MCP "Allow for this session"). Loosened for MCP first word.
_CODEX_OPTION2_RE = _re.compile(r"^\s*2\.\s+((?:Yes|Allow).*)$")
# Option 3 and 4 boundary markers, used to stop wrap-join when
# building up a multi-line option label.
_CODEX_OPTION3_RE = _re.compile(r"^\s*3\.\s")
_CODEX_OPTION4_RE = _re.compile(r"^\s*4\.\s")
# MCP 4-option UI: option 3 is "Always allow", which is the MCP
# analog of Bash's option-2 Always variant.
_CODEX_OPTION3_ALWAYS_RE = _re.compile(r"^\s*3\.\s+(Always.*)$")
_CODEX_OPTION_KEY_HINT_RE = _re.compile(r"\s*\([a-z]\)\s*$")
_CODEX_COL_SPLIT_RE = _re.compile(r"\s{2,}")
# Edit / sandbox-retry approval surfaces a "Reason: ..." subtitle
# instead of a `$ command` line; we surface it as the badge detail.
_CODEX_REASON_RE = _re.compile(r"^\s*Reason:\s*(.+?)\s*$")
# Structural option 2 anchor — any line starting with `  2. <text>`.
# Used as a third detection signal so we can confirm a live option
# list without relying on the title phrase. Independent of Yes/Allow
# wording so it matches every codex UI variant.
_CODEX_OPTION2_ANCHOR_RE = _re.compile(r"^\s*2\.\s+\S")

# (question_phrase, tool_name) — extension point for future codex
# approval UIs. The phrase must be short enough to survive narrow-
# pane word wrap (codex breaks lines at word boundaries, so a 4-5
# word prefix fits on any reasonable terminal width). Bash stays
# first as the common case; matching is sequential.
#
# - "Bash": "Would you like to run the following command?" — sandbox
#   wants permission to execute a shell command.
# - "MCP":  "Allow the <server> MCP server to run tool <x>?" — MCP
#   tool call permission, 4-option UI.
# - "Edit": "Would you like to make the following edits?" — codex
#   wanted to write/edit files, the sandbox blocked it, and codex is
#   asking permission to retry without sandbox. 3-option UI, same
#   navigation as Bash (Approve = Enter, Always = Down + Enter).
_CODEX_QUESTION_PATTERNS: list[tuple[str, str]] = [
    ("Would you like to run", "Bash"),
    ("MCP server to run tool", "MCP"),
    ("Would you like to make", "Edit"),
]


def _extract_codex_bash_detail(
    lines: list[str], question_idx: int, selector_idx: int
) -> str:
    """Extract the `$ command` line from a Bash approval block.

    Codex wraps long commands onto multiple indented rows, so we
    join up to 4 continuation lines together with a space. The join
    stops at a new `$ ` prefix or the `›` selector glyph.
    """
    for i in range(question_idx + 1, selector_idx):
        stripped = lines[i].strip()
        if stripped.startswith("$ "):
            parts = [stripped[2:]]
            for j in range(i + 1, selector_idx):
                nxt = lines[j].strip()
                if not nxt:
                    continue
                if nxt.startswith("$ ") or nxt.startswith(_CODEX_SELECTOR):
                    break
                parts.append(nxt)
                if len(parts) >= 4:
                    break
            return " ".join(parts).strip()[:150]
    return ""


def _extract_codex_edit_detail(
    lines: list[str], question_idx: int, selector_idx: int
) -> str:
    """Extract the `Reason: ...` subtitle from an Edit approval block.

    The Edit / sandbox-retry UI has no `$ command` line — instead it
    shows a single-line reason like "command failed; retry without
    sandbox?". We surface that as the dashboard badge detail so the
    user can see *why* codex is asking before pressing Approve.
    Returns "" if no Reason: line is found in the question→selector
    window.
    """
    for i in range(question_idx + 1, selector_idx):
        m = _CODEX_REASON_RE.match(lines[i])
        if m:
            return m.group(1)[:150]
    return ""


def _extract_codex_generic_detail(
    lines: list[str], selector_idx: int, scan_start: int
) -> str:
    """Best-effort detail for an unclassified codex approval block.

    Used when no known title phrase matches (new UI variant, phrasing
    drift, pane wrap edge case) but the structural anchors confirm a
    real approval is pending. Priority order when scanning upward
    from the selector:
    1. nearest `$ ` command line  (Bash-like)
    2. nearest `Reason: ...`      (Edit / sandbox-retry-like)
    3. first indented question line ending in `?` (the title)
    4. empty string
    """
    for i in range(selector_idx - 1, scan_start - 1, -1):
        s = lines[i].strip()
        if s.startswith("$ "):
            return s[2:][:150]
        m = _CODEX_REASON_RE.match(lines[i])
        if m:
            return m.group(1)[:150]
    for i in range(selector_idx - 1, scan_start - 1, -1):
        s = lines[i].strip()
        if s.endswith("?") and len(s) > 5:
            return s[:150]
    return ""


def _extract_codex_mcp_detail(
    lines: list[str], question_idx: int, selector_idx: int
) -> str:
    """Extract "<server>: <tool>" from an MCP tool approval block.

    The title line reads:
        Allow the <server> MCP server to run tool "<tool>"?
    Codex may word-wrap the title across 2-3 lines on narrow panes,
    so we scan a 4-line window from `question_idx` and run the
    regexes over the concatenated text. Each line is stripped
    before joining so leading indentation from word-wrap
    continuation rows doesn't insert extra spaces between words
    ("...to run" + "  tool..." would otherwise yield "to run   tool"
    with three spaces, breaking the single-space regex anchor).
    """
    end = min(len(lines), question_idx + 4, selector_idx)
    combined = " ".join(line.strip() for line in lines[question_idx:end])
    server_match = _re.search(r"Allow the (\S+) MCP server to run tool", combined)
    tool_match = _re.search(r'tool "([^"]+)"', combined)
    server = server_match.group(1) if server_match else ""
    tool = tool_match.group(1) if tool_match else ""
    if server and tool:
        return f"{server}: {tool}"[:150]
    if tool:
        return tool[:150]
    return ""


def _extract_codex_always_label(
    lines: list[str],
    selector_idx: int,
    total: int,
    tool_name: str,
) -> str | None:
    """Extract the verbatim "Always" option label for the given UI.

    - Bash: option 2 text ("Yes, and don't ask again for commands
      that start with `...`"). `None` if only the 2-option variant
      is displayed (no Always).
    - MCP:  option 3 text ("Always allow"). `None` if option 3
      isn't present (e.g. a compact variant).

    Description columns (codex aligns descriptions via 2+ spaces on
    the same line for the MCP UI) are stripped so we only keep the
    option label proper.
    """
    if tool_name in ("Bash", "Edit"):
        # Both surface Always as option 2 ("Yes, and don't ask again
        # for commands that start with..." / "...for these files").
        target_re = _CODEX_OPTION2_RE
        stop_re = _CODEX_OPTION3_RE
    elif tool_name == "MCP":
        target_re = _CODEX_OPTION3_ALWAYS_RE
        stop_re = _CODEX_OPTION4_RE
    else:
        return None

    for i in range(selector_idx + 1, min(total, selector_idx + 12)):
        m = target_re.match(lines[i])
        if not m:
            continue
        parts = [m.group(1).strip()]
        for j in range(i + 1, min(total, i + 6)):
            nxt = lines[j]
            if not nxt.strip():
                break
            if stop_re.match(nxt):
                break
            parts.append(nxt.strip())
        text = " ".join(parts)
        # Strip the description column that codex renders after 2+
        # spaces ("Always allow            Run the tool and remember
        # ...") so only the label itself remains.
        text = _CODEX_COL_SPLIT_RE.split(text, 1)[0].strip()
        # Drop the trailing single-key hint codex prints next to
        # option 2 — `(p)` for legacy Bash, `(a)` for the Edit /
        # sandbox-retry UI, etc. Only strip the lowercase letter
        # variant so the literal label text isn't accidentally cut.
        text = _CODEX_OPTION_KEY_HINT_RE.sub("", text).strip()
        return text[:250] if text else None
    return None


def _parse_codex_approval_prompt(
    pane_text: str,
) -> tuple[str, str, str | None] | None:
    """Parse Codex CLI approval prompt from tmux pane text.

    Returns (tool_name, detail, always_label) or None — same shape as
    `_parse_approval_prompt` so `periodic_pending_check` can dispatch
    on session tool and treat both the same downstream.

    ## Detection philosophy

    Detection and classification are **decoupled**. Detection is
    purely structural — we confirm a live option list is on screen
    via three anchors that never change regardless of UI variant:
      1. `› 1. (Yes|Allow)` selector in the pane tail
      2. `2. <text>` option line within a few lines below it
      3. a codex footer (`Press enter...` or `enter to submit...`)
         within the same tail window
    If all three hit, there is a real codex approval pending. Full
    stop. No title phrase required.

    Classification (Bash / MCP / Edit) runs as a second, best-effort
    pass that scans upward for known title phrases or content-type
    hints. When classification fails we still report a pending
    approval as generic `"Codex"` — the dashboard shows the Approve
    button, Enter works on every codex UI, and only the Always
    button degrades (it requires knowing which option holds Always).

    This structure is why codex phrasing drift, new UI variants, and
    long command bodies no longer silently drop approvals on the
    floor: detection survives even when classification doesn't.

    ## Supported classifications

    - **Bash** — "Would you like to run the following command?" with
      2 or 3 options. `always_label` is option 2 text for 3-option
      variants, None for 2-option.
    - **Edit** — "Would you like to make the following edits?" —
      sandbox-retry. 3 options, Always is option 2 (Bash nav).
    - **MCP**  — "Allow the <server> MCP server to run tool <x>?"
      with 4 options. Always is option 3 (Down Down Enter).
    - **Codex** — anything else that looks structurally like a codex
      approval. No Always button; Approve still works.
    """
    lines = pane_text.splitlines()
    # tmux capture-pane returns a fixed-height grid, so panes shorter
    # than the window have trailing blank rows. Strip them so our
    # "last N lines" windowing reflects the real UI position.
    while lines and not lines[-1].strip():
        lines.pop()
    total = len(lines)
    if total == 0:
        return None

    # 16-line tail — enough to contain the tallest codex UI (MCP
    # 4-option with workingDirectory line + blank + footer).
    tail_start = max(0, total - 16)

    # Signal 1: `› 1. (Yes|Allow)` must be in the tail window.
    selector_idx = None
    for i in range(total - 1, tail_start - 1, -1):
        if _CODEX_SELECTOR_OPTION_RE.search(lines[i]):
            selector_idx = i
            break
    if selector_idx is None:
        return None

    # Signal 2: structural option 2 anchor within 8 lines below the
    # selector. Confirms a live option list is rendered and rules
    # out a stale single-line fragment that happens to contain
    # `› 1. Yes`. UI-variant-agnostic — doesn't care about Yes/Allow.
    option2_found = False
    for i in range(selector_idx + 1, min(total, selector_idx + 8)):
        if _CODEX_OPTION2_ANCHOR_RE.match(lines[i]):
            option2_found = True
            break
    if not option2_found:
        return None

    # Signal 3: codex footer phrase within the tail window. Guards
    # against a mid-redraw capture where the options are visible but
    # the input loop hasn't armed yet.
    footer_found = False
    for i in range(tail_start, total):
        if _CODEX_FOOTER_RE.search(lines[i]):
            footer_found = True
            break
    if not footer_found:
        return None

    # Classification: best-effort. Scan upward 80 lines for a known
    # title phrase (picks the closest match, so stale blocks higher
    # up don't shadow the current one). The 80-line window handles
    # long heredocs where codex still shows 25+ pre-truncation lines.
    #
    # Word-wrap join: codex wraps titles on narrow panes, so
    # "MCP server to run tool" can span two lines. We check each
    # line individually AND the line joined with its successor.
    tool_name: str | None = None
    question_idx: int | None = None
    scan_start = max(0, selector_idx - 80)
    for i in range(selector_idx - 1, scan_start - 1, -1):
        candidates = [lines[i]]
        if i + 1 < selector_idx:
            candidates.append(lines[i].strip() + " " + lines[i + 1].strip())
        for phrase, t_name in _CODEX_QUESTION_PATTERNS:
            if any(phrase in c for c in candidates):
                question_idx = i
                tool_name = t_name
                break
        if tool_name is not None:
            break

    # Dispatch detail extraction by classification. Generic codex
    # falls through to a best-effort helper that looks for `$ ...`,
    # `Reason: ...`, or the first `?`-terminated line upward.
    if tool_name == "Bash":
        assert question_idx is not None
        detail = _extract_codex_bash_detail(lines, question_idx, selector_idx)
    elif tool_name == "MCP":
        assert question_idx is not None
        detail = _extract_codex_mcp_detail(lines, question_idx, selector_idx)
    elif tool_name == "Edit":
        assert question_idx is not None
        detail = _extract_codex_edit_detail(lines, question_idx, selector_idx)
    else:
        tool_name = "Codex"
        detail = _extract_codex_generic_detail(lines, selector_idx, scan_start)

    always_label = _extract_codex_always_label(
        lines, selector_idx, total, tool_name,
    )

    return (tool_name, detail, always_label)


async def _broadcast_pending_clear(
    conn: aiosqlite.Connection, sid: str, tmux_name: str | None
) -> None:
    stats = await db.get_stats(conn)
    await broadcaster.broadcast({
        "type": "pending",
        "session_id": sid,
        "pending_tool": None,
        "pending_detail": None,
        "tmux_session": tmux_name,
        "waiting_count": stats["waiting_sessions"],
    })


async def parse_one(
    conn: aiosqlite.Connection, session_id: str
) -> None:
    """Parse one session's pane and update its pending_tool state.

    Shared by both detection paths:
    - Push: PanePipeManager fires this when a tmux pipe sees activity.
    - Polling fallback: periodic_pending_check calls this every 60s for
      every active session as a safety net.

    Self-contained — fetches the session row itself, so callers don't
    need to pre-load it. Returns silently if the session has gone away
    or doesn't have a tmux.
    """
    session = await db.get_session(conn, session_id)
    if session is None:
        return
    tmux_name = session.get("tmux_session")
    if not tmux_name:
        return
    db_tool = session.get("pending_tool")

    pane_text = await _tmux_capture(tmux_name)
    if pane_text is None:
        # tmux not reachable — clear any stale pending state.
        if db_tool:
            _APPROVED_SUPPRESS.pop(session_id, None)
            await db.update_session_pending_tool(conn, session_id, None, None)
            await tg_cancel_pending(session_id)
            await _broadcast_pending_clear(conn, session_id, tmux_name)
        return

    if session.get("tool") == "codex":
        parsed = _parse_codex_approval_prompt(pane_text)
    else:
        parsed = _parse_approval_prompt(pane_text)

    if parsed is None:
        if db_tool:
            # Prompt gone — tool was approved or cancelled.
            _APPROVED_SUPPRESS.pop(session_id, None)
            await db.update_session_pending_tool(conn, session_id, None, None)
            await tg_cancel_pending(session_id)
            await _broadcast_pending_clear(conn, session_id, tmux_name)
        return

    pending_tool, pending_detail, always_label = parsed
    sig = (pending_tool, pending_detail, always_label)

    # Suppression: user just clicked Approve via the Hub and the pane
    # UI hasn't dismissed yet. If signature still matches the approved
    # one within the grace window, drop this tick to avoid re-firing
    # the same notification.
    suppress = _APPROVED_SUPPRESS.get(session_id)
    if suppress is not None:
        suppress_sig, until_ts = suppress
        if time.time() < until_ts and sig == suppress_sig:
            return
        _APPROVED_SUPPRESS.pop(session_id, None)

    has_always = always_label is not None
    if (
        pending_tool != db_tool
        or pending_detail != session.get("pending_detail")
        or always_label != session.get("pending_always_label")
    ):
        await db.update_session_pending_tool(
            conn, session_id, pending_tool, pending_detail, always_label,
        )
        stats = await db.get_stats(conn)
        await broadcaster.broadcast({
            "type": "pending",
            "session_id": session_id,
            "pending_tool": pending_tool,
            "pending_detail": pending_detail,
            "has_always": has_always,
            "always_label": always_label,
            "tmux_session": tmux_name,
            "waiting_count": stats["waiting_sessions"],
        })
        updated_session = await db.get_session(conn, session_id)
        await tg_notify_pending(
            session_id, updated_session or session, has_always, always_label,
        )


def make_pane_pipe_callback(conn: aiosqlite.Connection):
    """Build the tmux→sid resolver callback for PanePipeManager.

    The push path delivers tmux_name; we resolve to the newest active
    session in that tmux (matches the dedupe rule used by the polling
    fallback below) and run parse_one for it.
    """
    async def callback(tmux_name: str) -> None:
        cursor = await conn.execute(
            "SELECT session_id FROM sessions "
            "WHERE tmux_session = ? AND status = 'active' "
            "ORDER BY started_at DESC LIMIT 1",
            (tmux_name,),
        )
        row = await cursor.fetchone()
        if row is None:
            return
        await parse_one(conn, row["session_id"])

    return callback


async def attach_all_active_pipes(conn: aiosqlite.Connection) -> int:
    """Attach a pipe to every currently-active session with a tmux.

    Used at hub startup so resumed-active sessions (sessions that were
    active before a hub restart) get push observability immediately
    instead of waiting for the next event from the agent. Also:
    1. Cleans up orphan pipe files left over from prior hub instances.
    2. Runs an initial parse_one pass so any approval prompt that's
       already on screen is detected without waiting for the next
       pane redraw.
    """
    mgr = get_pipe_manager()
    if mgr is None:
        return 0
    cursor = await conn.execute(
        "SELECT session_id, tmux_session FROM sessions "
        "WHERE status = 'active' AND tmux_session IS NOT NULL"
    )
    rows = await cursor.fetchall()
    keep: set[str] = {row["tmux_session"] for row in rows}
    await mgr.cleanup_orphan_pipes(keep)
    attached = 0
    for row in rows:
        ok = await mgr.attach(row["session_id"], row["tmux_session"])
        if ok:
            attached += 1
            # Bootstrap: pane may already show an approval prompt. Push
            # only fires on NEW activity, so without this initial parse
            # an existing prompt could go undetected until the user
            # interacts with the pane again.
            await parse_one(conn, row["session_id"])
    logger.info("attach_all_active_pipes: attached %d session(s)", attached)
    return attached


async def install_tmux_session_hook(port: int, *, verbose: bool = False) -> bool:
    """Bind a `session-created` hook in the user's tmux server so each
    new tmux session POSTs to `/api/internal/tmux-discovered` and we
    discover codex TUIs in ~50 ms instead of waiting on the 60 s poll.

    Idempotent: ``set-hook -g`` (no ``-a``) replaces any prior binding
    from us. Run on every hub restart AND on every codex-discovery tick
    so we self-heal when the tmux server is restarted (server lifecycle
    is independent of the hub's — a fresh server starts with no hooks).

    ``run-shell -b`` runs curl in tmux's background subprocess pool so
    a new-session command never blocks on the hub being slow / down.
    ``--max-time`` bounds the curl wait if the hub is unreachable.

    ``verbose=True`` logs the install on success; default is silent so
    the periodic re-install doesn't flood the journal.
    """
    url = f"http://127.0.0.1:{port}/api/internal/tmux-discovered"
    cmd = (
        f'run-shell -b "curl -s --max-time 2 -X POST '
        f'{url}?name=#{{session_name}}"'
    )
    try:
        proc = await asyncio.create_subprocess_exec(
            "tmux", "set-hook", "-g", "session-created", cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
    except Exception:
        # tmux binary missing or otherwise unavailable. The polling
        # fallback below still works; just log so the user knows push
        # discovery is degraded.
        logger.warning("tmux set-hook invocation failed (push discovery off)")
        return False
    if proc.returncode != 0:
        # Most common cause: no tmux server running yet. We retry on
        # every codex-discovery tick, so this self-heals as soon as
        # the user starts their first tmux.
        return False
    if verbose:
        logger.info("Installed tmux session-created hook → %s", url)
    return True


async def periodic_codex_discovery(
    conn: aiosqlite.Connection, hub_id: str, hub_port: int,
) -> None:
    """Polling fallback for codex discovery — scans all alive tmuxes
    every 60 s.

    Push (tmux ``session-created`` hook → ``/api/internal/tmux-discovered``)
    is the primary path and detects new codex panes within ~50 ms. This
    loop is the safety net for anything push misses, AND it re-asserts
    the session-created hook each tick so a tmux server restart (which
    drops global hooks) self-heals within ~60 s.
    """
    while True:
        try:
            await asyncio.sleep(60)
            await install_tmux_session_hook(hub_port)
            await _discover_codex_tmux(conn, hub_id, _LOCAL_HOSTNAME)
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Error in codex discovery")


async def periodic_pending_check(
    conn: aiosqlite.Connection, hub_id: str
) -> None:
    """Background task: poll active sessions for pending tool approval.

    Acts as a safety net behind the push-based PanePipeManager. Push
    handles the common case in <200ms; this loop runs every 60s so
    that anything push misses (inotify limits, watchfiles bugs, hub
    restart gaps, panes the manager didn't get attached to) is still
    detected within a minute.

    `hub_id` is unused here but retained for symmetry with the codex
    discovery task signature.
    """
    del hub_id  # symmetry only
    while True:
        try:
            await asyncio.sleep(60)
            sessions = await db.get_sessions(conn, status="active")

            # Dedupe by tmux: when a parent + subagent share a tmux,
            # only the newest-started session owns the pending-tool
            # detection. Otherwise both would hit the same pane and
            # both would get marked waiting on the same prompt.
            sessions_by_tmux_seen: set[str] = set()
            deduped: list[dict] = []
            for session in sorted(
                sessions,
                key=lambda s: s.get("started_at") or "",
                reverse=True,
            ):
                tn = session.get("tmux_session")
                if tn:
                    if tn in sessions_by_tmux_seen:
                        continue
                    sessions_by_tmux_seen.add(tn)
                deduped.append(session)

            for session in deduped:
                await parse_one(conn, session["session_id"])

        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Error checking pending tools")
