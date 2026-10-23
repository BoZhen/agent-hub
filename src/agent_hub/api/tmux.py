"""Tmux Hub APIs — list, create, and browse directories for tmux session management."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from agent_hub.services import tmux_input

# The launcher offers Claude Code, Codex, and — when a second config
# directory exists — Claude Code on a second account. Each runs plain
# with the tool's default model. Approvals and sandboxing follow the
# user's own configuration (Claude: permissions.defaultMode in
# settings.json; Codex: approval_policy and sandbox_mode in config.toml),
# so no flag is added. Codex runs bare: its hooks are a native feature
# (`[features] hooks = true` plus ~/.codex/hooks.json), so no wrapper
# such as oh-my-codex is needed.
# The keys double as the session's `tool` value once hooks report in, so
# restore.py can rebuild the same command and environment on resume;
# keep them stable. "claude-yolo", "codex-yolo" and "omx-yolo" are
# retired keys: rows launched by earlier versions still carry them, and
# restore.py still knows their flags.
_ALLOWED_COMMANDS = {
    "claude",
    "claude2",
    "codex",
}

# Maps a virtual command key → the actual binary name that gets resolved
# via shutil.which.
_COMMAND_BIN: dict[str, str] = {
    "claude": "claude",
    "claude2": "claude",
    "codex": "codex",
}

# Extra args appended after the resolved command path.
_COMMAND_ARGS: dict[str, list[str]] = {
    "claude": [],
    "claude2": [],
    "codex": [],
}

# Auto-name prefix for the generated tmux name: "claude-<workdir>-N",
# "claude2-<workdir>-N" and "codex-<workdir>-N".
_NAME_PREFIX: dict[str, str] = {
    "claude": "claude",
    "claude2": "claude2",
    "codex": "codex",
}


def claude2_config_dir(config: Any) -> str | None:
    """Expanded path of the second Claude account's config directory, or
    None when it is not set up on this machine."""
    raw = getattr(config, "claude2_config_dir", None) if config is not None else None
    if not raw:
        return None
    path = Path(raw).expanduser()
    return str(path) if path.is_dir() else None


def launch_env(tool: str, config: Any) -> dict[str, str]:
    """Environment a tmux needs for `tool`, shared by launch, resume and
    fork so a second-account session always opens under its own
    CLAUDE_CONFIG_DIR."""
    if tool == "claude2":
        directory = claude2_config_dir(config)
        if directory:
            return {"CLAUDE_CONFIG_DIR": directory}
    return {}


def session_path(env: Mapping[str, str] | None = None) -> str:
    """PATH pinned onto a session the Hub starts (launch, resume, fork).

    It is the Hub's own PATH, which main._augment_path extends with the
    user's CLI directories, minus the Hub's virtualenv: `uv run` puts
    .venv/bin first, and pinning that made `python` in every session the
    Hub started resolve to the Hub's interpreter.
    """
    env = os.environ if env is None else env
    drop = set()
    if env.get("VIRTUAL_ENV"):
        drop.add(os.path.normpath(os.path.join(env["VIRTUAL_ENV"], "bin")))
    if sys.prefix != sys.base_prefix:
        drop.add(os.path.normpath(os.path.join(sys.prefix, "bin")))
    parts = env.get("PATH", "").split(os.pathsep)
    return os.pathsep.join(p for p in parts if p and os.path.normpath(p) not in drop)

logger = logging.getLogger(__name__)
router = APIRouter()

_SEP = "\t"
_FORMAT = _SEP.join([
    "#{session_name}",
    "#{pane_current_path}",
    "#{session_attached}",
    "#{session_created}",
    "#{pane_dead}",
    "#{@agent_hub_title}",
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
    # fnm uses a different layout: ~/.fnm/node-versions/<ver>/installation/bin
    fnm_versions = root / ".fnm" / "node-versions"
    if fnm_versions.is_dir():
        dirs.extend(sorted(fnm_versions.glob("*/installation/bin"), reverse=True))
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
        title = parts[5] if len(parts) > 5 else ""
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
            "title": title or None,
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
    title: str | None = None  # the session's name in the Hub; renamable later


def clean_title(raw: str | None) -> str:
    """A Hub name: one line, at most 80 characters (400 otherwise)."""
    title = " ".join((raw or "").split())
    if len(title) > 80:
        raise HTTPException(400, "Session name is limited to 80 characters")
    return title


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
async def new_tmux(request: Request, req: NewTmuxRequest) -> dict[str, Any]:
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

    title = clean_title(req.title)
    cmd_path: str | None = None
    if req.command:
        if req.command not in _ALLOWED_COMMANDS:
            raise HTTPException(
                400,
                f"command must be one of {sorted(_ALLOWED_COMMANDS)}",
            )
        if req.command == "claude2" and not claude2_config_dir(request.app.state.config):
            raise HTTPException(
                400,
                "second Claude account is not set up: AGENT_HUB_CLAUDE2_CONFIG_DIR must point at its config directory",
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
    # Pin the hub's augmented PATH onto the new session. Without this,
    # `tmux new-session -d` inherits the long-running tmux server's
    # PATH (set when the user first ran tmux outside the hub), not
    # the hub process's. claude/codex hooks inside the pane then can't
    # find user-installed node, codex, etc. -e is per-session, so other
    # tmux sessions are unaffected. session_path() leaves out the Hub's
    # own virtualenv.
    path = session_path()
    if path:
        tmux_args.extend(["-e", f"PATH={path}"])
    for key, value in launch_env(req.command or "", request.app.state.config).items():
        tmux_args.extend(["-e", f"{key}={value}"])
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
    if title:
        await tmux_input.set_tmux_title(name, title)

    return {"name": name, "cwd": str(resolved), "command": req.command, "title": title or None}


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
