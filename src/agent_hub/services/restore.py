"""Restore tmux sessions after machine reboot.

When the machine reboots, tmux server dies and all Claude/Codex
processes exit, but session metadata in the hub DB persists. This
module recreates the missing tmux sessions with the correct name +
cwd, prints a hint about how to resume Claude/Codex, and drops to a
shell. User runs `claude --resume <sid>` manually so they have full
control over which sessions to actually bring back online.

When the user runs `claude --resume <sid>`, Claude fires the
SessionStart hook with source=resume; ensure_session sees the
existing DB row and flips status back to active automatically.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import aiosqlite

from agent_hub.services.session_manager import mark_hub_launched

logger = logging.getLogger(__name__)


async def _list_alive_tmux_names() -> set[str]:
    proc = await asyncio.create_subprocess_exec(
        "tmux", "ls", "-F", "#{session_name}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        return set()
    return set(stdout.decode("utf-8", errors="replace").splitlines())


def _resume_hint_command(tool: str, session_id: str) -> str:
    """Return a shell-quoted human-readable resume hint for a tool."""
    if tool == "claude-yolo":
        return f"claude --resume {session_id} --dangerously-skip-permissions"
    if tool == "claude-sonnet":
        return f"claude --resume {session_id} --model sonnet"
    if tool == "claude-opus":
        return f"claude --resume {session_id} --model opus"
    if tool.startswith("claude"):
        return f"claude --resume {session_id}"
    if tool == "omx-yolo":
        return "omx resume --last --madmax --xhigh"
    if tool in ("codex", "omx"):
        # codex session_id stored in hub is synthetic (codex-{name}-{ts}),
        # so we can't pass it as a UUID. Hint the user toward omx's
        # interactive resume picker.
        return "omx resume"
    return ""


def _build_tmux_shell_command(tool: str, session_id: str) -> list[str]:
    """Build the bash -c command that runs inside the new tmux pane.

    Prints a one-liner hint with the exact resume command, then drops
    to the user's interactive shell. The hint stays in the pane
    scrollback so the user can copy it after attaching.
    """
    hint = _resume_hint_command(tool, session_id)
    banner = (
        "\\n\\033[1;33m  Restored after reboot\\033[0m  "
        f"(tool={tool}, session={session_id[:8]}...)\\n"
    )
    if hint:
        banner += (
            "\\n  \\033[1;36mTo resume:\\033[0m  "
            f"\\033[97m{hint}\\033[0m\\n\\n"
        )
    else:
        banner += (
            "\\n  \\033[2m(no auto-resume hint for this tool — start "
            "manually)\\033[0m\\n\\n"
        )
    # `exec ${SHELL:-bash} -i` keeps the pane interactive after the
    # echo. -i ensures rc files load (history, prompt, etc.).
    return ["bash", "-c", f"echo -e '{banner}'; exec ${{SHELL:-bash}} -i"]


async def find_orphan_sessions(conn: aiosqlite.Connection) -> list[dict[str, Any]]:
    """Return DB rows for sessions whose tmux is dead.

    Filters: status in (active, idle), tmux_session is set, parent is
    NULL (subagents are not first-class tmux owners and would conflict
    with their parent's tmux).
    """
    alive = await _list_alive_tmux_names()
    cursor = await conn.execute(
        "SELECT session_id, tmux_session, cwd, tool, model, status "
        "FROM sessions "
        "WHERE tmux_session IS NOT NULL "
        "AND status IN ('active', 'idle') "
        "AND parent_session_id IS NULL "
        "ORDER BY last_seen_at DESC"
    )
    rows = await cursor.fetchall()
    orphans = []
    seen_tmux: set[str] = set()
    for r in rows:
        d = dict(r)
        tn = d["tmux_session"]
        if tn in alive:
            continue
        # Dedupe: if multiple DB rows share a dead tmux name (e.g. orphan
        # from prior /clear), only restore the most recent (rows are
        # ordered DESC by last_seen_at).
        if tn in seen_tmux:
            continue
        seen_tmux.add(tn)
        orphans.append(d)
    return orphans


async def restore_session(
    conn: aiosqlite.Connection, session_row: dict[str, Any]
) -> dict[str, Any]:
    """Recreate tmux + spawn resume command for one session.

    `conn` is reserved for future use (e.g. recording restore attempts
    in DB). Currently unused but kept for API symmetry.

    Returns {"ok": True, "name": ...} on success, or
    {"ok": False, "error": ..., "name": ...} on failure.
    """
    _ = conn  # reserved
    name = session_row["tmux_session"]
    cwd = session_row["cwd"]
    sid = session_row["session_id"]
    tool = (session_row.get("tool") or "claude").strip()

    # Pre-flight checks.
    if not cwd or not os.path.isdir(cwd):
        return {"ok": False, "name": name, "error": f"cwd not found: {cwd}"}

    # If tmux already exists (race / hub restart without machine reboot),
    # do nothing — caller will see tmux is alive on next sweep.
    alive = await _list_alive_tmux_names()
    if name in alive:
        return {"ok": True, "name": name, "skipped": "tmux already alive"}

    shell_cmd = _build_tmux_shell_command(tool, sid)
    tmux_args = ["tmux", "new-session", "-d", "-s", name, "-c", cwd, *shell_cmd]
    proc = await asyncio.create_subprocess_exec(
        *tmux_args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        err = stderr.decode("utf-8", errors="replace").strip() or "unknown"
        return {"ok": False, "name": name, "error": f"tmux failed: {err}"}

    # Mark as hub-launched so the new SessionStart hook on Claude doesn't
    # get classified as "transferred" (workspace-trust delay heuristic).
    mark_hub_launched(name, tool)

    logger.info("Restored session %s in tmux=%s (tool=%s)", sid, name, tool)
    return {"ok": True, "name": name, "tool": tool}


async def restore_all_orphans(conn: aiosqlite.Connection) -> dict[str, Any]:
    """Find every orphan session and restore them sequentially.

    Sequential (not parallel) because spawning N Claude processes in
    parallel can spike CPU and saturate the API on large N. Small
    delay between spawns gives each Claude a chance to start cleanly.
    """
    orphans = await find_orphan_sessions(conn)
    if not orphans:
        return {"total": 0, "restored": 0, "failed": 0, "results": []}

    logger.info("Found %d orphan session(s) to restore", len(orphans))
    results: list[dict[str, Any]] = []
    restored = 0
    failed = 0
    for row in orphans:
        try:
            result = await restore_session(conn, row)
        except Exception as e:
            logger.exception("Restore failed for %s", row.get("session_id"))
            result = {
                "ok": False,
                "name": row.get("tmux_session"),
                "error": str(e),
            }
        results.append(result)
        if result.get("ok"):
            restored += 1
        else:
            failed += 1
            logger.warning(
                "Restore failed: tmux=%s reason=%s",
                result.get("name"), result.get("error"),
            )
        # Throttle: small gap between spawns.
        await asyncio.sleep(0.5)

    logger.info(
        "Restore complete: %d succeeded, %d failed, %d total",
        restored, failed, len(orphans),
    )
    return {
        "total": len(orphans),
        "restored": restored,
        "failed": failed,
        "results": results,
    }


async def restore_on_startup(conn: aiosqlite.Connection) -> None:
    """Background task to run shortly after hub starts.

    Delayed slightly so the WebSocket / discovery loops have a chance
    to settle. Logs results but doesn't raise — restore failures
    must not crash the hub.
    """
    try:
        await asyncio.sleep(2.0)  # let other startup tasks settle
        await restore_all_orphans(conn)
    except Exception:
        logger.exception("restore_on_startup failed")
