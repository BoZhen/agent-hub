"""Typing into a session's agent pane from outside its terminal.

POST /api/sessions/{id}/send and /interrupt use this, so a phone can answer
a session with its own keyboard instead of typing into the terminal.

Text goes in as one bracketed paste (tmux load-buffer, then paste-buffer
-p). Claude Code and Codex turn bracketed paste mode on, so a multi-line
message arrives as a single input and only the Enter pressed afterwards
submits it. Typing the text with send-keys would submit every line on
its own.
"""
from __future__ import annotations

import asyncio
import itertools
import os

TMUX = "tmux"
# Foreground commands of a pane that runs an agent; npm installs run as node.
AGENT_COMMANDS = ("claude", "codex")
NODE = "node"

_buffers = itertools.count(1)


class TmuxInputError(RuntimeError):
    pass


class NoAgentError(TmuxInputError):
    """The tmux exists but no pane runs claude or codex (the CLI exited
    and a shell is left). Typing there would run the text as a command."""


async def _tmux(*args: str, data: bytes | None = None) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        TMUX, *args,
        stdin=asyncio.subprocess.PIPE if data is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate(data)
    return (
        proc.returncode or 0,
        out.decode("utf-8", errors="replace"),
        err.decode("utf-8", errors="replace").strip(),
    )


def pick_agent_pane(panes: list[tuple[str, str, bool]]) -> str | None:
    """The pane an agent runs in, from (pane id, foreground command,
    active) rows: one running claude or codex, else one running node.
    Among several, the active one wins. None when no pane runs an agent."""
    for names in (AGENT_COMMANDS, (NODE,)):
        found = [p for p in panes if os.path.basename(p[1]) in names]
        if found:
            active = [p for p in found if p[2]]
            return (active or found)[0][0]
    return None


async def agent_pane(tmux_name: str) -> str:
    rc, out, err = await _tmux(
        "list-panes", "-s", "-t", f"={tmux_name}",
        "-F", "#{pane_id}\t#{pane_current_command}\t#{window_active}#{pane_active}",
    )
    if rc != 0:
        raise TmuxInputError(err or f"tmux session {tmux_name} not found")
    panes = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            panes.append((parts[0], parts[1], parts[2] == "11"))
    pane = pick_agent_pane(panes)
    if pane is None:
        running = ", ".join(sorted({p[1] for p in panes})) or "nothing"
        raise NoAgentError(f"No agent is running in tmux {tmux_name} (it runs {running})")
    return pane


async def paste(pane: str, text: str) -> None:
    """Paste text into a pane as one bracketed paste."""
    buffer = f"agent-hub-{os.getpid()}-{next(_buffers)}"
    rc, _, err = await _tmux("load-buffer", "-b", buffer, "-", data=text.encode("utf-8"))
    if rc != 0:
        raise TmuxInputError(err or "tmux load-buffer failed")
    rc, _, err = await _tmux("paste-buffer", "-p", "-d", "-b", buffer, "-t", pane)
    if rc != 0:
        await _tmux("delete-buffer", "-b", buffer)
        raise TmuxInputError(err or "tmux paste-buffer failed")


async def press(pane: str, *keys: str) -> None:
    rc, _, err = await _tmux("send-keys", "-t", pane, *keys)
    if rc != 0:
        raise TmuxInputError(err or "tmux send-keys failed")
