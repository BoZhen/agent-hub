"""Bring sessions back: after a reboot, and on demand for ended ones.

A session's tmux dies when the machine reboots or when the CLI inside it
exits. The Hub row survives with everything needed to rebuild the pair:
tmux name, cwd and the launch flags encoded in `tool`. This module turns
that row into a fresh tmux running the CLI's own resume command
(`claude --resume <sid>` or `codex resume <sid>`), so the TUI comes back
with its conversation.

Two entry points share one implementation:

- restore_on_startup: shortly after the Hub starts, every active/idle row
  whose tmux is missing (a reboot, typically) is brought back. Mode
  "resume" runs the resume command; mode "shell" only recreates the tmux
  and prints the command, for people who want to pick by hand.
- POST /api/sessions/{id}/resume: the same for one row, including ended
  ones.

When the resumed CLI reports in (Claude fires SessionStart at once, Codex
with the first prompt), ensure_session finds the existing row and marks
it active. The pane falls back to an interactive shell if the CLI exits
or fails to resume, so the tmux and the row stay usable.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
from typing import Any

import aiosqlite

from agent_hub import db
from agent_hub.services.session_manager import mark_hub_launched

logger = logging.getLogger(__name__)

RESTORE_MODES = ("resume", "shell")


def extract_cwd_from_transcript(
    transcript_path: str | None, *, max_lines: int = 30
) -> str | None:
    """Read a Claude transcript JSONL and return the first ``cwd`` value.

    Claude indexes session JSONL under
    ``~/.claude/projects/<encoded-cwd>/<sid>.jsonl`` based on the cwd at
    first launch. Resuming with ``claude --resume <sid>`` later only
    works from that original cwd, so we need an authoritative way to
    recover it. The transcript itself records ``cwd`` on regular
    user/attachment records — but the first few records are metadata
    (``last-prompt``, ``permission-mode``) that lack the field, so we
    scan the head of the file for the first hit.

    Returns ``None`` for codex transcripts (no ``cwd`` field) and for
    any IO/parse failure — callers fall back to the DB ``cwd`` column.
    """
    if not transcript_path:
        return None
    try:
        with open(transcript_path, encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i >= max_lines:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                cwd = rec.get("cwd") if isinstance(rec, dict) else None
                if isinstance(cwd, str) and cwd:
                    return cwd
    except OSError as e:
        logger.debug("transcript read failed: %s (%s)", transcript_path, e)
    return None


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


_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)

_CLAUDE_FLAGS = {
    "claude-yolo": ["--dangerously-skip-permissions"],
    "claude-sonnet": ["--model", "sonnet"],
    "claude-opus": ["--model", "opus"],
}


def is_codex_tool(tool: str | None) -> bool:
    return (tool or "").startswith(("codex", "omx"))


def resume_argv(tool: str, session_id: str) -> list[str] | None:
    """The command that resumes this session, as argv with the bare binary
    name first, or None when the tool is unknown.

    A codex session that reported through the hook carries Codex's own
    UUID, which `codex resume` accepts directly. A session that was only
    discovered by pane scanning has a synthetic id (codex-<tmux>-<ts>), so
    the interactive picker is used. "omx-yolo" rows predate the bare-codex
    launcher; `omx --madmax` was an alias for the bypass flag, so the same
    codex command resumes them whether or not oh-my-codex is installed.
    """
    tool = (tool or "").strip()
    if tool.startswith("claude"):
        return ["claude", "--resume", session_id, *_CLAUDE_FLAGS.get(tool, [])]
    if is_codex_tool(tool):
        argv = ["codex", "resume"]
        if _UUID_RE.match(session_id):
            argv.append(session_id)
        if "yolo" in tool:
            argv.append("--dangerously-bypass-approvals-and-sandbox")
        return argv
    return None


def _resume_hint_command(tool: str, session_id: str) -> str:
    """Human-readable form of resume_argv for banners and the API."""
    argv = resume_argv(tool, session_id)
    return shlex.join(argv) if argv else ""


def _shell_pane_command(tool: str, session_id: str) -> list[str]:
    """Pane command for mode "shell": print the resume hint, then drop to
    the user's interactive shell. Also the fallback when the CLI binary
    cannot be found. The hint stays in the pane scrollback."""
    hint = _resume_hint_command(tool, session_id)
    banner = (
        "\\n\\033[1;33m  agent-hub recreated this tmux\\033[0m  "
        f"(tool={tool}, session={session_id[:8]}...)\\n"
    )
    if hint:
        banner += (
            "\\n  \\033[1;36mTo resume:\\033[0m  "
            f"\\033[97m{hint}\\033[0m\\n\\n"
        )
    else:
        banner += (
            "\\n  \\033[2m(no resume command known for this tool — start "
            "manually)\\033[0m\\n\\n"
        )
    # `exec ${SHELL:-bash} -i` keeps the pane interactive after the
    # echo. -i ensures rc files load (history, prompt, etc.).
    return ["bash", "-c", f"echo -e '{banner}'; exec ${{SHELL:-bash}} -i"]


def _resume_pane_command(argv: list[str]) -> list[str]:
    """Pane command for mode "resume": run the resolved resume command,
    then drop to an interactive shell so the pane survives if the CLI
    exits or fails to resume. argv travels as positional parameters, so
    nothing is re-quoted by the shell."""
    script = (
        "printf '\\033[2m  agent-hub: %s\\033[0m\\n' \"$1\"; shift; \"$@\"; "
        "exec \"${SHELL:-bash}\" -i"
    )
    return ["bash", "-c", script, "_", shlex.join(argv), *argv]


def _free_tmux_name(preferred: str | None, tool: str, cwd: str, alive: set[str]) -> str:
    """Reuse the row's tmux name when it is free; otherwise suffix it, or
    derive one from the cwd when the row never had a name."""
    if not preferred:
        from agent_hub.api.tmux import _auto_name  # local: api imports this module

        prefix = "codex" if is_codex_tool(tool) else ("claude2" if tool == "claude2" else "claude")
        return _auto_name(cwd, alive, prefix=prefix)
    name, n = preferred, 2
    while name in alive:
        name = f"{preferred[:58]}-{n}"
        n += 1
    return name


async def find_orphan_sessions(conn: aiosqlite.Connection) -> list[dict[str, Any]]:
    """Return DB rows for sessions whose tmux is dead.

    Filters: status in (active, idle), tmux_session is set, parent is
    NULL (subagents are not first-class tmux owners and would conflict
    with their parent's tmux).
    """
    alive = await _list_alive_tmux_names()
    cursor = await conn.execute(
        "SELECT session_id, tmux_session, cwd, tool, model, status, transcript_path "
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
    conn: aiosqlite.Connection,
    session_row: dict[str, Any],
    *,
    mode: str = "resume",
    config: Any = None,
) -> dict[str, Any]:
    """Recreate the tmux for one row and start the CLI inside it (mode
    "resume"), or leave a shell with the command printed (mode "shell").

    Works for rows whose tmux is gone, whether the row is active, idle
    or stopped. Returns {"ok": True, "name", "mode", "command"} or
    {"ok": False, "name", "error"}.
    """
    sid = session_row["session_id"]
    tool = (session_row.get("tool") or "claude").strip()
    db_cwd = session_row.get("cwd")
    old_name = session_row.get("tmux_session")

    # Trust the transcript: the JSONL lives under the directory Claude
    # used at first launch, and `claude --resume <sid>` only works from
    # that exact path. The DB `cwd` column can drift if the user once
    # resumed from elsewhere (pre-fix data), so prefer the transcript.
    canonical_cwd = extract_cwd_from_transcript(session_row.get("transcript_path")) or db_cwd
    cwd = canonical_cwd
    if not cwd or not os.path.isdir(cwd):
        return {"ok": False, "name": old_name, "error": f"cwd not found: {cwd}"}

    # If the transcript revealed a different cwd than the DB held,
    # quietly correct the row so the dashboard / Terminal links also
    # point at the right place. Best-effort; never block restore on it.
    if canonical_cwd != db_cwd:
        try:
            await conn.execute(
                "UPDATE sessions SET cwd = ? WHERE session_id = ?",
                (canonical_cwd, sid),
            )
            await conn.commit()
            logger.info(
                "Corrected drifted cwd for %s: %r → %r", sid, db_cwd, canonical_cwd
            )
        except Exception:
            logger.exception("Failed to backfill cwd for %s", sid)

    alive = await _list_alive_tmux_names()
    if old_name in alive and session_row.get("status") != "stopped":
        # Race / hub restart without a reboot: nothing to do.
        return {"ok": True, "name": old_name, "skipped": "tmux already alive"}
    name = _free_tmux_name(old_name, tool, cwd, alive)

    argv = resume_argv(tool, sid)
    binary = None
    if mode == "resume" and argv:
        from agent_hub.api.tmux import _resolve_command  # local: api imports this module

        binary = _resolve_command(argv[0])
        if not binary:
            logger.warning(
                "Resume: %s not found in PATH; leaving a shell in tmux=%s", argv[0], name
            )
    if binary:
        pane_cmd = _resume_pane_command([binary, *argv[1:]])
        effective_mode = "resume"
    else:
        pane_cmd = _shell_pane_command(tool, sid)
        effective_mode = "shell"

    tmux_args = ["tmux", "new-session", "-d", "-s", name, "-c", cwd]
    from agent_hub.api.tmux import launch_env, session_path  # local: api imports this module

    # Same reasoning as the launcher: the tmux server's PATH predates the
    # hub's, so pin the hub's augmented PATH for hooks inside the pane.
    path = session_path()
    if path:
        tmux_args.extend(["-e", f"PATH={path}"])

    for key, value in launch_env(tool, config).items():
        tmux_args.extend(["-e", f"{key}={value}"])
    tmux_args.extend(pane_cmd)
    proc = await asyncio.create_subprocess_exec(
        *tmux_args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        err = stderr.decode("utf-8", errors="replace").strip() or "unknown"
        return {"ok": False, "name": name, "error": f"tmux failed: {err}"}

    # Mark as hub-launched so the CLI's SessionStart keeps the launch
    # flags in `tool` and is never classified as "transferred".
    mark_hub_launched(name, tool)
    if name != old_name:
        await db.update_session_tmux(conn, sid, name)
    if session_row.get("status") == "stopped":
        # The tmux is back, so the row is idle again. The CLI's own
        # SessionStart flips it to active.
        await db.update_session_status(conn, sid, "idle")

    logger.info(
        "Resumed session %s in tmux=%s (tool=%s, mode=%s)", sid, name, tool, effective_mode
    )
    return {
        "ok": True,
        "name": name,
        "tool": tool,
        "mode": effective_mode,
        "command": shlex.join(argv) if argv else None,
    }


async def restore_all_orphans(
    conn: aiosqlite.Connection, *, mode: str = "resume", config: Any = None
) -> dict[str, Any]:
    """Find every orphan session and restore them sequentially.

    Sequential (not parallel) because spawning N CLI processes in
    parallel can spike CPU and saturate the API on large N. Small
    delay between spawns gives each one a chance to start cleanly.
    """
    orphans = await find_orphan_sessions(conn)
    if not orphans:
        return {"total": 0, "restored": 0, "failed": 0, "results": []}

    logger.info("Found %d orphan session(s) to restore (mode=%s)", len(orphans), mode)
    results: list[dict[str, Any]] = []
    restored = 0
    failed = 0
    for row in orphans:
        try:
            result = await restore_session(conn, row, mode=mode, config=config)
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


async def backfill_canonical_cwds(conn: aiosqlite.Connection) -> int:
    """Repair drifted cwd values by reading transcripts.

    Pre-fix data may have a DB ``cwd`` that diverged from Claude's
    project-dir-encoded location (the user once resumed from a
    different directory). Walk every active/idle Claude session, ask
    the transcript for the original cwd, and write it back where it
    differs. Returns the count of rows fixed. Best-effort — failures
    are logged and ignored.
    """
    cursor = await conn.execute(
        "SELECT session_id, cwd, transcript_path FROM sessions "
        "WHERE status IN ('active','idle') AND tool LIKE 'claude%' "
        "AND transcript_path IS NOT NULL"
    )
    rows = await cursor.fetchall()
    fixed = 0
    for r in rows:
        real = extract_cwd_from_transcript(r["transcript_path"])
        if not real or real == r["cwd"]:
            continue
        try:
            await conn.execute(
                "UPDATE sessions SET cwd = ? WHERE session_id = ?",
                (real, r["session_id"]),
            )
            fixed += 1
            logger.info(
                "Backfilled cwd for %s: %r → %r",
                r["session_id"], r["cwd"], real,
            )
        except Exception:
            logger.exception("cwd backfill failed for %s", r["session_id"])
    if fixed:
        await conn.commit()
    return fixed


async def restore_on_startup(
    conn: aiosqlite.Connection, mode: str = "resume", config: Any = None
) -> None:
    """Background task to run shortly after hub starts.

    Delayed slightly so the WebSocket / discovery loops have a chance
    to settle. Logs results but doesn't raise — restore failures
    must not crash the hub.
    """
    if mode not in RESTORE_MODES:
        logger.warning("Unknown restore mode %r; using 'resume'", mode)
        mode = "resume"
    try:
        await asyncio.sleep(2.0)  # let other startup tasks settle
        # Repair drifted cwds before any restore decision uses them.
        await backfill_canonical_cwds(conn)
        await restore_all_orphans(conn, mode=mode, config=config)
    except Exception:
        logger.exception("restore_on_startup failed")
