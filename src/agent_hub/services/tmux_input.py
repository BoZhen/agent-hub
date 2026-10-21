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
# Process names of an agent CLI. An npm install runs as `node`, so a node
# process counts when its command line names one of them.
AGENT_COMMANDS = ("claude", "codex")
_TREE_DEPTH = 6

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


def _process_table() -> dict[int, tuple[int, str]]:
    """pid -> (parent pid, process name) for every process, from /proc."""
    table: dict[int, tuple[int, str]] = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat", "rb") as f:
                stat = f.read().decode("utf-8", errors="replace")
        except OSError:
            continue
        # The name sits in parentheses and may itself contain spaces or
        # parentheses; the parent pid is the second field after the last ")".
        close = stat.rfind(")")
        fields = stat[close + 2:].split()
        if len(fields) > 1:
            table[int(entry)] = (int(fields[1]), stat[stat.find("(") + 1:close])
    return table


def _is_agent(pid: int, name: str) -> bool:
    if name in AGENT_COMMANDS:
        return True
    if name != "node":
        return False
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            argv = f.read().decode("utf-8", errors="replace").split("\0")
    except OSError:
        return False
    script = argv[1] if len(argv) > 1 else ""
    return (
        os.path.basename(script) in AGENT_COMMANDS
        or "@anthropic-ai/claude-code" in script
        or "@openai/codex" in script
    )


def runs_agent(pane_pid: int, table: dict[int, tuple[int, str]]) -> bool:
    """Whether claude or codex runs in the pane's process tree.

    A pane's own foreground process is not enough: the Hub resumes a
    session as `bash -c '...; claude --resume <id>; exec $SHELL -i'`, and
    fish runs commands without handing them the terminal, so tmux reports
    `bash` or `fish` while the agent runs underneath.
    """
    children: dict[int, list[int]] = {}
    for pid, (ppid, _) in table.items():
        children.setdefault(ppid, []).append(pid)
    level = [pane_pid]
    for _ in range(_TREE_DEPTH):
        if any(pid in table and _is_agent(pid, table[pid][1]) for pid in level):
            return True
        level = [child for pid in level for child in children.get(pid, [])]
        if not level:
            return False
    return False


def pick_agent_pane(panes: list[tuple[str, bool, bool]]) -> str | None:
    """The pane an agent runs in, from (pane id, runs an agent, active)
    rows. Among several, the active one wins. None when no pane runs an
    agent."""
    found = [p for p in panes if p[1]]
    active = [p for p in found if p[2]]
    return (active or found)[0][0] if found else None


async def agent_pane(tmux_name: str) -> str:
    rc, out, err = await _tmux(
        "list-panes", "-s", "-t", f"={tmux_name}",
        "-F", "#{pane_id}\t#{pane_pid}\t#{window_active}#{pane_active}\t#{pane_current_command}",
    )
    if rc != 0:
        raise TmuxInputError(err or f"tmux session {tmux_name} not found")
    table = _process_table()
    panes = []
    commands = set()
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == 4 and parts[1].isdigit():
            panes.append((parts[0], runs_agent(int(parts[1]), table), parts[2] == "11"))
            commands.add(parts[3])
    pane = pick_agent_pane(panes)
    if pane is None:
        running = ", ".join(sorted(commands)) or "nothing"
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
