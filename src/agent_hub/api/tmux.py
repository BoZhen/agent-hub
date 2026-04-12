"""Tmux Hub APIs — list, create, and browse directories for tmux session management."""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

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


def _auto_name(cwd: str, existing: set[str]) -> str:
    base = os.path.basename(cwd.rstrip("/")) or "tmux"
    i = 1
    while f"{base}-{i}" in existing:
        i += 1
    return f"{base}-{i}"


def _valid_tmux_name(name: str) -> bool:
    if not name or len(name) > 80:
        return False
    bad = set(".: \t\n\r/")
    return not any(c in bad for c in name)


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

    existing_sessions = await _tmux_ls()
    existing_names = {s["name"] for s in existing_sessions}

    name = req.name.strip() if req.name else ""
    if name:
        if not _valid_tmux_name(name):
            raise HTTPException(
                400,
                "tmux name must be 1-80 chars without . : / whitespace",
            )
        if name in existing_names:
            raise HTTPException(409, f"tmux session '{name}' already exists")
    else:
        name = _auto_name(str(resolved), existing_names)

    proc = await asyncio.create_subprocess_exec(
        "tmux", "new-session", "-d", "-s", name, "-c", str(resolved),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        err = stderr.decode("utf-8", errors="replace").strip() or "unknown"
        raise HTTPException(500, f"tmux new-session failed: {err}")

    return {"name": name, "cwd": str(resolved)}


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
