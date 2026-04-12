from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import aiosqlite

from agent_hub import db
from agent_hub.api.ws import broadcaster
from agent_hub.services.telegram_bot import (
    cancel_pending as tg_cancel_pending,
    notify_pending as tg_notify_pending,
)

logger = logging.getLogger(__name__)


# Tmux sessions launched by Hub's `/api/tmux/new` with `command=claude`
# are never "transferred" — they're first-class Claude sessions. We
# track them here so `_detect_transferred` can skip the timing heuristic
# (claude's workspace-trust prompt can delay SessionStart past the 5s
# threshold, otherwise misclassifying them as From Tmux).
_HUB_LAUNCHED_TMUX: dict[str, float] = {}
_HUB_LAUNCHED_TTL = 120.0


def mark_hub_launched(tmux_name: str) -> None:
    import time
    now = time.time()
    for k in [k for k, ts in _HUB_LAUNCHED_TMUX.items() if now - ts > _HUB_LAUNCHED_TTL]:
        _HUB_LAUNCHED_TMUX.pop(k, None)
    _HUB_LAUNCHED_TMUX[tmux_name] = now


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
) -> None:
    """Create session if not exists, or reactivate on resume.

    On new-session creation, detect whether this Claude session was
    started inside a pre-existing bare tmux (tmux older than ~30s at
    the moment of the SessionStart hook). If so, mark transferred=1
    so the dashboard can file it under the "From Tmux" tab.
    """
    event_type = payload.get("hook_event_name", "")
    model = payload.get("model")  # Accept model from any event type

    existing = await db.get_session(conn, session_id)

    if existing is None:
        transferred = 0
        if tmux_session:
            transferred = await _detect_transferred(tmux_session)
            # Retire earlier sessions that shared this tmux name — the
            # name has been reused for a new Claude instance, so any
            # prior session_id bound to it is definitively dead. Without
            # this, tmux-name reuse leaves stale idle rows forever.
            cursor = await conn.execute(
                "SELECT session_id FROM sessions "
                "WHERE tmux_session = ? AND session_id != ? "
                "AND status IN ('active', 'idle')",
                (tmux_session, session_id),
            )
            orphans = [r["session_id"] for r in await cursor.fetchall()]
            for orphan_id in orphans:
                await db.update_session_status(conn, orphan_id, "stopped")
                await db.update_session_pending_tool(conn, orphan_id, None, None)
            if orphans:
                logger.info(
                    "Retired %d older session(s) bound to reused tmux %s",
                    len(orphans), tmux_session,
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
        )
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
        )
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


async def mark_session_idle(
    conn: aiosqlite.Connection, session_id: str
) -> None:
    await db.update_session_status(conn, session_id, "idle")
    await db.update_session_pending_tool(conn, session_id, None, None)


_INTERRUPT_RE = None  # lazy-compiled regex (see _is_claude_thinking)


async def _is_claude_thinking(tmux_name: str) -> bool:
    """Return True if Claude's status line shows it is actively
    processing (thinking / streaming / running a tool).

    Detected by the parenthesised \"(... esc to interrupt ...)\" hint
    that appears in the status line at the bottom of the pane. The
    parentheses requirement makes this robust against free text that
    happens to mention the phrase without the full TUI structure.
    """
    global _INTERRUPT_RE
    if _INTERRUPT_RE is None:
        import re
        _INTERRUPT_RE = re.compile(r"\([^)]*esc to interrupt[^)]*\)")

    pane_text = await _tmux_capture(tmux_name)
    if pane_text is None:
        return False
    lines = pane_text.splitlines()
    tail = "\n".join(lines[-8:])
    return bool(_INTERRUPT_RE.search(tail))


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
    for row in rows:
        if row["tmux_session"] not in alive:
            await db.update_session_status(conn, row["session_id"], "stopped")
            await db.update_session_pending_tool(conn, row["session_id"], None, None)
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

import re as _re
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


async def periodic_pending_check(conn: aiosqlite.Connection) -> None:
    """Background task: check active sessions for pending tool approval every 3s.

    Uses tmux capture-pane as ground truth — if the terminal shows
    "Do you want to proceed?", the session is waiting for approval.
    No delay, no false positives.
    """
    while True:
        try:
            await asyncio.sleep(3)
            sessions = await db.get_sessions(conn, status="active")

            for session in sessions:
                sid = session["session_id"]
                tmux_name = session.get("tmux_session")
                db_tool = session.get("pending_tool")

                if not tmux_name:
                    continue

                pane_text = await _tmux_capture(tmux_name)

                if pane_text is None:
                    # tmux not reachable — clear pending if set
                    if db_tool:
                        await db.update_session_pending_tool(conn, sid, None, None)
                        await tg_cancel_pending(sid)
                        stats = await db.get_stats(conn)
                        await broadcaster.broadcast({
                            "type": "pending",
                            "session_id": sid,
                            "pending_tool": None,
                            "pending_detail": None,
                            "tmux_session": tmux_name,
                            "waiting_count": stats["waiting_sessions"],
                        })
                    continue

                parsed = _parse_approval_prompt(pane_text)

                if parsed:
                    pending_tool, pending_detail, always_label = parsed
                    has_always = always_label is not None
                    db_label = session.get("pending_always_label")
                    if (
                        pending_tool != db_tool
                        or pending_detail != session.get("pending_detail")
                        or always_label != db_label
                    ):
                        await db.update_session_pending_tool(
                            conn, sid, pending_tool, pending_detail, always_label,
                        )
                        stats = await db.get_stats(conn)
                        await broadcaster.broadcast({
                            "type": "pending",
                            "session_id": sid,
                            "pending_tool": pending_tool,
                            "pending_detail": pending_detail,
                            "has_always": has_always,
                            "always_label": always_label,
                            "tmux_session": tmux_name,
                            "waiting_count": stats["waiting_sessions"],
                        })
                        # Re-fetch session with updated pending fields
                        updated_session = await db.get_session(conn, sid)
                        await tg_notify_pending(
                            sid, updated_session or session, has_always, always_label,
                        )
                elif db_tool:
                    # Prompt gone — tool was approved or cancelled
                    await db.update_session_pending_tool(conn, sid, None, None)
                    await tg_cancel_pending(sid)
                    stats = await db.get_stats(conn)
                    await broadcaster.broadcast({
                        "type": "pending",
                        "session_id": sid,
                        "pending_tool": None,
                        "pending_detail": None,
                        "tmux_session": tmux_name,
                        "waiting_count": stats["waiting_sessions"],
                    })

        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Error checking pending tools")
