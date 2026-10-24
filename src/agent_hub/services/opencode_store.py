"""opencode 2 sessions, read from opencode's own database.

opencode keeps every session in one SQLite database,
$XDG_DATA_HOME/opencode/opencode.db (~/.local/share/opencode/opencode.db);
OPENCODE_DB names another file, relative to that directory or absolute.
`session_v2` holds a session's title, model and token totals,
`session_message` its messages, each a JSON document in `data` whose kind
is the `type` column. The database is opened read-only; opencode writes it
in WAL mode, so reading never blocks it.
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

# opencode's tools under the names the Hub knows (Claude's), with the
# argument shown: (Hub name, opencode argument, Hub argument). The same
# table is in scripts/opencode-hub-plugin.js.
TOOLS: dict[str, tuple[str, str, str]] = {
    "shell": ("Bash", "command", "command"),
    "read": ("Read", "path", "file_path"),
    "write": ("Write", "path", "file_path"),
    "edit": ("Edit", "path", "file_path"),
    "grep": ("Grep", "pattern", "pattern"),
    "glob": ("Glob", "pattern", "pattern"),
    "webfetch": ("WebFetch", "url", "url"),
    "websearch": ("WebSearch", "query", "query"),
    "subagent": ("Agent", "description", "description"),
}


def db_path() -> str:
    """Where opencode keeps its database."""
    data = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    name = os.environ.get("OPENCODE_DB") or "opencode.db"
    return name if os.path.isabs(name) else os.path.join(data, "opencode", name)


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
    conn.row_factory = sqlite3.Row
    return conn


def read_usage(session_id: str, path: str | None = None) -> dict | None:
    """Token totals and model of a session, in read_usage's shape
    (transcript_reader), or None when the session is not in the database."""
    path = path or db_path()
    if not os.path.isfile(path):
        return None
    try:
        with closing(_connect(path)) as conn:
            row = conn.execute(
                "SELECT tokens_input, tokens_output, tokens_cache_read, tokens_cache_write, model "
                "FROM session_v2 WHERE id = ?",
                (session_id,),
            ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    try:
        model = (json.loads(row["model"]) or {}).get("id") if row["model"] else None
    except (ValueError, AttributeError):
        model = None
    return {
        "input_tokens": row["tokens_input"],
        "output_tokens": row["tokens_output"],
        "cache_read_tokens": row["tokens_cache_read"],
        "cache_create_tokens": row["tokens_cache_write"],
        "model": model,
    }


def read_messages(session_id: str, limit: int = 30, path: str | None = None) -> list[dict]:
    """The last `limit` messages of a session, oldest first, in the shape
    of conversation.read_messages."""
    from agent_hub.services.conversation import _item  # local: conversation imports this module

    path = path or db_path()
    if not os.path.isfile(path):
        return []
    found: list[dict] = []
    try:
        with closing(_connect(path)) as conn:
            # A message can give several items (text and tool calls), so
            # read in pages until enough are found.
            offset = 0
            while len(found) < limit:
                rows = conn.execute(
                    "SELECT type, time_created, data FROM session_message "
                    "WHERE session_id = ? ORDER BY seq DESC LIMIT 100 OFFSET ?",
                    (session_id, offset),
                ).fetchall()
                if not rows:
                    break
                offset += len(rows)
                for row in rows:
                    try:
                        data = json.loads(row["data"])
                    except ValueError:
                        continue
                    ts = datetime.fromtimestamp(row["time_created"] / 1000, timezone.utc).isoformat()
                    found.extend(reversed([_item(role, text, ts) for role, text in _items(row["type"], data)]))
    except sqlite3.Error:
        return []
    found = found[:limit]
    found.reverse()
    return found


def _items(kind: str, data: dict) -> list[tuple[str, str]]:
    """(role, text) pairs of one message, in order."""
    if kind == "user":
        text = (data.get("text") or "").strip()
        return [("user", text)] if text else []
    if kind == "shell":
        return [("user", "! " + str(data.get("command") or ""))]
    if kind == "idle":
        if data.get("outcome") == "interrupted":
            return [("note", "Interrupted")]
        if data.get("outcome") == "failed":
            return [("note", _error_text(data.get("error")) or "Failed")]
        return []
    if kind != "assistant":
        return []
    items = []
    for block in data.get("content") or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and (block.get("text") or "").strip():
            items.append(("assistant", block["text"]))
        elif block.get("type") == "tool":
            state = block.get("state")
            arguments = state.get("input") if isinstance(state, dict) else None
            items.append(("tool", tool_summary(block.get("name"), arguments)))
    return items


def _error_text(error) -> str:
    if isinstance(error, dict):
        return str(error.get("message") or "")
    return str(error or "")


def tool_summary(name, arguments) -> str:
    from agent_hub.services.event_processor import _summarize_tool_use

    spec = TOOLS.get(name or "")
    if spec is None:
        return str(name or "Tool use")
    value = arguments.get(spec[1]) if isinstance(arguments, dict) else None
    return _summarize_tool_use(spec[0], {spec[2]: value} if value is not None else {})
