"""Tmux Hub APIs — list, create, and browse directories for tmux session management."""
from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

_ALLOWED_COMMANDS = {
    "claude-sonnet",
    "claude-opus",
    "claude-yolo",
    "codex",
    "omx-yolo",
}

# Maps a virtual command key → the actual binary name that gets resolved
# via shutil.which. Keys not listed here are resolved as-is.
_COMMAND_BIN: dict[str, str] = {
    "claude-sonnet": "claude",
    "claude-opus": "claude",
    "claude-yolo": "claude",
    "codex": "omx",
    "omx-yolo": "omx",
}

# Extra args appended after the resolved command path. Lets virtual command
# keys inject model selectors / dangerous-mode flags while keeping a flat
# allowlist.
_COMMAND_ARGS: dict[str, list[str]] = {
    "claude-sonnet": ["--model", "sonnet"],
    "claude-opus": ["--model", "opus"],
    "claude-yolo": ["--dangerously-skip-permissions"],
    "omx-yolo": ["--madmax", "--xhigh"],
}

# Auto-name prefix override for virtual command keys. Commands not
# listed here fall back to the key itself. Used to shorten the auto-
# generated tmux name: "claude-sonnet-<workdir>-N" is too long, but
# "sonnet-<workdir>-N" / "opus-<workdir>-N" still disambiguate.
_NAME_PREFIX: dict[str, str] = {
    "claude-sonnet": "sonnet",
    "claude-opus": "opus",
}

logger = logging.getLogger(__name__)
router = APIRouter()

_SEP = "\t"
_FORMAT = _SEP.join([
    "#{session_name}",
    "#{pane_current_path}",
    "#{session_attached}",
    "#{session_created}",
    "#{pane_dead}",
])


def _extra_command_dirs(home: Path | None = None) -> list[str]:
    """Return user-level CLI install dirs missing from systemd user PATH.

    The Hub usually runs as a systemd user service.  That environment often
    does not load interactive shell startup files, so CLIs installed by nvm /
    npm / uv can be invisible to ``shutil.which`` even though they work in a
    normal terminal.  Keep this list conservative and user-scoped.
    """
    root = (home or Path.home()).expanduser()
    dirs = [
        root / ".local" / "bin",
        root / ".bun" / "bin",
        root / ".npm-global" / "bin",
        root / ".nvm" / "current" / "bin",
    ]
    node_versions = root / ".nvm" / "versions" / "node"
    if node_versions.is_dir():
        dirs.extend(sorted(node_versions.glob("*/bin"), reverse=True))
    return [str(path) for path in dirs if path.is_dir()]


def _resolve_command(bin_name: str, *, path_env: str | None = None, home: Path | None = None) -> str | None:
    """Resolve a command using PATH plus common per-user CLI directories."""
    search_path = path_env if path_env is not None else os.environ.get("PATH", "")
    paths = [p for p in search_path.split(os.pathsep) if p]
    seen = set(paths)
    for extra in _extra_command_dirs(home):
        if extra not in seen:
            paths.append(extra)
            seen.add(extra)
    for directory in paths:
        candidate = Path(directory) / bin_name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


async def _tmux_ls() -> list[dict[str, Any]]:
    proc = await asyncio.create_subprocess_exec(
        "tmux", "ls", "-F", _FORMAT,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        return []
    sessions: list[dict[str, Any]] = []
    for line in stdout.decode("utf-8", errors="replace").splitlines():
        parts = line.split(_SEP)
        if len(parts) < 5:
            continue
        name, cwd, attached, created, dead = parts[:5]
        try:
            created_ts = int(created)
        except ValueError:
            created_ts = 0
        sessions.append({
            "name": name,
            "cwd": cwd,
            "attached": attached == "1",
            "created_at": created_ts,
            "dead": dead == "1",
        })
    return sessions


@router.get("/tmux/list")
async def list_tmux(request: Request) -> dict[str, list]:
    """List tmux sessions not currently owned by an active/idle Claude session."""
    conn = request.app.state.db
    sessions = await _tmux_ls()

    cursor = await conn.execute(
        "SELECT DISTINCT tmux_session FROM sessions "
        "WHERE tmux_session IS NOT NULL AND status IN ('active', 'idle')"
    )
    rows = await cursor.fetchall()
    claimed = {r["tmux_session"] for r in rows if r["tmux_session"]}

    bare = [s for s in sessions if s["name"] not in claimed]
    return {"sessions": bare}


class NewTmuxRequest(BaseModel):
    name: str | None = None
    cwd: str
    command: str | None = None  # e.g. "claude" — launched inside the new tmux


# Matches the web-terminal server's accepted name regex so every name
# agent-hub hands out can be attached to from the browser.
_VALID_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def _auto_name(cwd: str, existing: set[str], prefix: str = "") -> str:
    base = os.path.basename(cwd.rstrip("/")) or "tmux"
    # Collapse any non-ASCII-safe runs to a single hyphen so Chinese
    # directory names / spaces still produce an attachable name.
    base = re.sub(r"[^a-zA-Z0-9_-]+", "-", base).strip("-") or "tmux"
    if prefix:
        base = f"{prefix}-{base}"
    # Leave room for a "-NNN" suffix within the 64-char cap.
    if len(base) > 60:
        base = base[:60].rstrip("-") or "tmux"
    i = 1
    while f"{base}-{i}" in existing:
        i += 1
    return f"{base}-{i}"


def _valid_tmux_name(name: str) -> bool:
    return bool(_VALID_NAME_RE.match(name))


@router.post("/tmux/new")
async def new_tmux(req: NewTmuxRequest) -> dict[str, Any]:
    cwd_path = Path(req.cwd).expanduser()
    if not cwd_path.is_absolute():
        raise HTTPException(400, "cwd must be an absolute path")
    try:
        resolved = cwd_path.resolve(strict=True)
    except FileNotFoundError:
        raise HTTPException(400, f"cwd does not exist: {req.cwd}")
    if not resolved.is_dir():
        raise HTTPException(400, f"cwd is not a directory: {req.cwd}")
    if not os.access(resolved, os.R_OK | os.X_OK):
        raise HTTPException(400, f"cwd is not accessible: {req.cwd}")

    cmd_path: str | None = None
    if req.command:
        if req.command not in _ALLOWED_COMMANDS:
            raise HTTPException(
                400,
                f"command must be one of {sorted(_ALLOWED_COMMANDS)}",
            )
        bin_name = _COMMAND_BIN.get(req.command, req.command)
        cmd_path = _resolve_command(bin_name)
        if not cmd_path:
            raise HTTPException(400, f"command not found in PATH: {bin_name}")

    existing_sessions = await _tmux_ls()
    existing_names = {s["name"] for s in existing_sessions}

    name = req.name.strip() if req.name else ""
    if name:
        # Fold Chinese-IME smart punctuation back to ASCII so a user
        # typing "codex-a" with "智能标点" on doesn't silently create a
        # session the web-terminal will refuse to attach to.
        name = name.replace("\u2013", "-").replace("\u2014", "-")
        if not _valid_tmux_name(name):
            raise HTTPException(
                400,
                "tmux name must be 1-64 ASCII letters/digits/underscore/hyphen",
            )
        if name in existing_names:
            raise HTTPException(409, f"tmux session '{name}' already exists")
    else:
        cmd_key = req.command or ""
        name = _auto_name(
            str(resolved), existing_names,
            prefix=_NAME_PREFIX.get(cmd_key, cmd_key),
        )

    tmux_args = [
        "tmux", "new-session", "-d", "-s", name, "-c", str(resolved),
    ]
    if cmd_path:
        tmux_args.append(cmd_path)
        tmux_args.extend(_COMMAND_ARGS.get(req.command or "", []))

    proc = await asyncio.create_subprocess_exec(
        *tmux_args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        err = stderr.decode("utf-8", errors="replace").strip() or "unknown"
        raise HTTPException(500, f"tmux new-session failed: {err}")

    if cmd_path:
        from agent_hub.services.session_manager import mark_hub_launched
        mark_hub_launched(name, req.command or "")

    return {"name": name, "cwd": str(resolved), "command": req.command}


class KillTmuxRequest(BaseModel):
    name: str


@router.post("/tmux/kill")
async def kill_tmux(req: KillTmuxRequest) -> dict[str, Any]:
    """Kill a tmux session by name."""
    if not _valid_tmux_name(req.name):
        raise HTTPException(400, "invalid tmux name")
    proc = await asyncio.create_subprocess_exec(
        "tmux", "kill-session", "-t", f"{req.name}:",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        err = stderr.decode("utf-8", errors="replace").strip() or "unknown"
        raise HTTPException(500, f"tmux kill-session failed: {err}")
    return {"name": req.name, "killed": True}


@router.get("/browse")
async def browse(path: str = Query(default="")) -> dict[str, Any]:
    """List subdirectories of path for the directory picker."""
    if not path:
        path = str(Path.home())
    p = Path(path).expanduser()
    if not p.is_absolute():
        raise HTTPException(400, "path must be absolute")
    try:
        resolved = p.resolve(strict=True)
    except FileNotFoundError:
        raise HTTPException(404, f"path not found: {path}")
    if not resolved.is_dir():
        raise HTTPException(400, f"not a directory: {path}")
    if not os.access(resolved, os.R_OK | os.X_OK):
        raise HTTPException(403, f"not accessible: {path}")

    entries: list[dict[str, Any]] = []
    try:
        for child in sorted(resolved.iterdir(), key=lambda c: c.name.lower()):
            if child.name.startswith("."):
                continue
            try:
                if not child.is_dir():
                    continue
            except OSError:
                continue
            if not os.access(child, os.R_OK | os.X_OK):
                continue
            entries.append({"name": child.name, "is_dir": True})
    except PermissionError:
        raise HTTPException(403, f"permission denied: {path}")

    parent = str(resolved.parent) if resolved != resolved.parent else None
    return {
        "path": str(resolved),
        "parent": parent,
        "entries": entries,
    }
