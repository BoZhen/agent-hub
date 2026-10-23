"""Recent messages of a session, read from the CLI's own transcript.

Claude Code writes ~/.claude/projects/<dir>/<session id>.jsonl, Codex
writes ~/.codex/sessions/YYYY/MM/DD/rollout-<time>-<session id>.jsonl and
pi writes ~/.pi/agent/sessions/--<dir>--/<time>_<session id>.jsonl. All
are JSON Lines and can be large (Claude keeps whole API requests in some
records), so the file is read backwards in blocks until enough messages
are found.

Each message is {"role": "user" | "assistant" | "tool" | "note",
"text": ..., "ts": ...}. "tool" is a one-line summary of a tool call;
"note" is something the CLI wrote into the conversation, such as an
interruption.
"""
from __future__ import annotations

import glob
import json
import os
import re
from collections.abc import Iterator

from agent_hub.services.event_processor import _summarize_tool_use
from agent_hub.services.restore import is_codex_tool, is_pi_tool

BLOCK = 256 * 1024
MAX_SCAN = 32 * 1024 * 1024
TEXT_MAX = 20000
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

# Text Claude Code stores as a user message but the user did not type.
_CLAUDE_SKIP_PREFIXES = (
    "<local-command-caveat>",
    "<local-command-stdout>",
    "<local-command-stderr>",
    "<system-reminder>",
    "<bash-stdout>",
    "<bash-stderr>",
    "<task-notification>",
)
_COMMAND_RE = re.compile(r"<command-name>(.*?)</command-name>", re.S)
_COMMAND_ARGS_RE = re.compile(r"<command-args>(.*?)</command-args>", re.S)
_BASH_INPUT_RE = re.compile(r"<bash-input>(.*?)</bash-input>", re.S)


def find_transcript(session: dict, claude_config_dirs: list[str]) -> str | None:
    """The transcript file of a session, or None.

    The hooks report the path (stored as transcript_path). Without it the
    file is looked up by session id: Claude under each config directory,
    Codex under ~/.codex/sessions, pi under its session directory. A Codex
    session the Hub only knows from pane scanning (earlier Hub versions)
    has a made-up id and no transcript.
    """
    path = session.get("transcript_path")
    if path and os.path.isfile(path):
        return path
    sid = session["session_id"]
    if is_codex_tool(session.get("tool")):
        if not _UUID_RE.match(sid):
            return None
        pattern = os.path.expanduser(f"~/.codex/sessions/*/*/*/rollout-*-{sid}.jsonl")
        hits = glob.glob(pattern)
    elif is_pi_tool(session.get("tool")):
        root = os.environ.get("PI_CODING_AGENT_SESSION_DIR") or "~/.pi/agent/sessions"
        hits = glob.glob(os.path.join(os.path.expanduser(root), "*", f"*_{glob.escape(sid)}.jsonl"))
    else:
        hits = []
        for root in claude_config_dirs:
            hits += glob.glob(os.path.join(os.path.expanduser(root), "projects", "*", f"{sid}.jsonl"))
    return max(hits, key=os.path.getmtime) if hits else None


def read_messages(path: str, tool: str | None, limit: int = 30) -> list[dict]:
    """The last `limit` messages of a transcript, oldest first."""
    parse = _codex_items if is_codex_tool(tool) else _pi_items if is_pi_tool(tool) else _claude_items
    found: list[dict] = []
    for record in _records_backwards(path):
        found.extend(reversed(parse(record)))
        if len(found) >= limit:
            break
    found = found[:limit]
    found.reverse()
    return found


def _records_backwards(path: str, max_scan: int = MAX_SCAN) -> Iterator[dict]:
    """JSON records of a JSON Lines file, last one first."""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        pos = f.tell()
        scanned = 0
        tail = b""
        while pos > 0 and scanned < max_scan:
            step = min(BLOCK, pos)
            pos -= step
            scanned += step
            f.seek(pos)
            lines = (f.read(step) + tail).split(b"\n")
            # The first piece may be the end of a line that starts in the
            # block before; keep it until that block is read.
            tail = lines[0]
            for line in reversed(lines[1:]):
                record = _parse(line)
                if record is not None:
                    yield record
        if pos == 0:
            record = _parse(tail)
            if record is not None:
                yield record


def _parse(line: bytes) -> dict | None:
    line = line.strip()
    if not line:
        return None
    try:
        record = json.loads(line)
    except ValueError:
        return None
    return record if isinstance(record, dict) else None


def _item(role: str, text: str, ts: str | None) -> dict:
    text = text.strip()
    if len(text) > TEXT_MAX:
        text = text[: TEXT_MAX - 1] + "…"
    return {"role": role, "text": text, "ts": ts}


# ── Claude Code ───────────────────────────────────────────────


def _claude_items(record: dict) -> list[dict]:
    if record.get("isSidechain") or record.get("isMeta"):
        return []
    message = record.get("message")
    if not isinstance(message, dict):
        return []
    ts = record.get("timestamp")
    content = message.get("content")
    if record.get("type") == "user":
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            # tool_result blocks are tool output, not something the user said.
            parts = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    parts.append(block.get("text") or "")
                elif block.get("type") == "image":
                    parts.append("[image]")
            text = "\n".join(p for p in parts if p)
        else:
            return []
        return _claude_user(text, ts)
    if record.get("type") == "assistant" and isinstance(content, list):
        items = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and (block.get("text") or "").strip():
                items.append(_item("assistant", block["text"], ts))
            elif block.get("type") == "tool_use":
                items.append(_item("tool", _summarize_tool_use(block.get("name"), block.get("input")), ts))
        return items
    return []


def _claude_user(text: str, ts: str | None) -> list[dict]:
    text = text.strip()
    if not text or text.startswith(_CLAUDE_SKIP_PREFIXES):
        return []
    if text.startswith("[Request interrupted"):
        return [_item("note", text.strip("[]"), ts)]
    command = _COMMAND_RE.search(text)
    if command and text.startswith("<command-"):
        args = _COMMAND_ARGS_RE.search(text)
        line = command.group(1).strip()
        if args and args.group(1).strip():
            line += " " + args.group(1).strip()
        return [_item("user", line, ts)]
    bash = _BASH_INPUT_RE.search(text)
    if bash and text.startswith("<bash-input>"):
        return [_item("user", "! " + bash.group(1).strip(), ts)]
    return [_item("user", text, ts)]


# ── Codex ─────────────────────────────────────────────────────


def _codex_items(record: dict) -> list[dict]:
    """Codex logs every finished item as an item_completed event; its
    UserMessage items are what the user typed, without the instructions
    Codex injects as user-role messages."""
    payload = record.get("payload")
    if record.get("type") != "event_msg" or not isinstance(payload, dict):
        return []
    if payload.get("type") != "item_completed":
        return []
    item = payload.get("item")
    if not isinstance(item, dict):
        return []
    ts = record.get("timestamp")
    kind = item.get("type")
    if kind in ("UserMessage", "AgentMessage"):
        text = _codex_text(item.get("content"))
        if not text:
            return []
        return [_item("user" if kind == "UserMessage" else "assistant", text, ts)]
    if kind == "CommandExecution":
        return [_item("tool", "$ " + _codex_command(item.get("command")), ts)]
    if kind == "McpToolCall":
        name = ".".join(str(p) for p in (item.get("server"), item.get("tool")) if p)
        return [_item("tool", f"MCP {name}".strip(), ts)]
    if kind == "Extension" and item.get("kind") == "web.search":
        return [_item("tool", f"WebSearch {item.get('query') or ''}".strip(), ts)]
    return []


def _codex_text(content) -> str:
    if not isinstance(content, list):
        return ""
    parts = [
        block.get("text") or ""
        for block in content
        if isinstance(block, dict) and str(block.get("type", "")).lower() == "text"
    ]
    return "\n".join(p for p in parts if p).strip()


def _codex_command(command) -> str:
    """Codex runs commands as [shell, "-lc", script]; show the script."""
    if isinstance(command, list):
        if len(command) >= 3 and command[1] in ("-lc", "-c"):
            text = str(command[2])
        else:
            text = " ".join(str(c) for c in command)
    else:
        text = str(command or "")
    text = " ".join(text.split())
    return text if len(text) <= 200 else text[:199] + "…"


# ── pi ────────────────────────────────────────────────────────

# pi's built-in tools under the names _summarize_tool_use knows (Claude's):
# pi name -> (Claude name, pi argument, Claude argument). Kept in step with
# scripts/pi-hub-extension.js.
_PI_TOOLS = {
    "bash": ("Bash", "command", "command"),
    "read": ("Read", "path", "file_path"),
    "write": ("Write", "path", "file_path"),
    "edit": ("Edit", "path", "file_path"),
    "grep": ("Grep", "pattern", "pattern"),
    "find": ("Glob", "pattern", "pattern"),
    "ls": ("LS", "path", "path"),
}


def _pi_items(record: dict) -> list[dict]:
    """pi stores the conversation as a tree of entries; a "message" entry
    holds one message. Read backwards, the newest branch comes first."""
    message = record.get("message")
    if record.get("type") != "message" or not isinstance(message, dict):
        return []
    ts = record.get("timestamp")
    role = message.get("role")
    content = message.get("content")
    if role == "user":
        text = _pi_text(content)
        return [_item("user", text, ts)] if text else []
    if role == "bashExecution":
        return [_item("user", "! " + str(message.get("command") or ""), ts)]
    if role != "assistant" or not isinstance(content, list):
        return []
    items = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and (block.get("text") or "").strip():
            items.append(_item("assistant", block["text"], ts))
        elif block.get("type") == "toolCall":
            items.append(_item("tool", _pi_tool_summary(block.get("name"), block.get("arguments")), ts))
    if message.get("stopReason") == "aborted":
        items.append(_item("note", "Interrupted", ts))
    elif message.get("stopReason") == "error" and message.get("errorMessage"):
        items.append(_item("note", str(message["errorMessage"]), ts))
    return items


def _pi_text(content) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            parts.append(block.get("text") or "")
        elif block.get("type") == "image":
            parts.append("[image]")
    return "\n".join(p for p in parts if p).strip()


def _pi_tool_summary(name, arguments) -> str:
    spec = _PI_TOOLS.get(name or "")
    if spec is None:
        return str(name or "Tool use")
    value = arguments.get(spec[1]) if isinstance(arguments, dict) else None
    return _summarize_tool_use(spec[0], {spec[2]: value} if value is not None else {})
