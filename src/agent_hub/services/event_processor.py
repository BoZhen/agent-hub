from __future__ import annotations

import json
import logging
from typing import Any

import aiosqlite

from agent_hub import db
from agent_hub.services import session_manager

logger = logging.getLogger(__name__)


async def process_event(
    conn: aiosqlite.Connection,
    payload: dict[str, Any],
    hub_id: str,
    hostname: str,
) -> None:
    session_id = payload["session_id"]
    event_type = payload["hook_event_name"]
    tool_name = payload.get("tool_name")
    cwd = payload.get("cwd", "")

    # Ensure session exists (auto-create on first event)
    await session_manager.ensure_session(
        conn,
        session_id=session_id,
        hub_id=hub_id,
        hostname=hostname,
        cwd=cwd,
        payload=payload,
    )

    summary = generate_summary(payload)
    sanitized = _sanitize_payload(payload)

    await db.insert_event(
        conn,
        hub_id=hub_id,
        session_id=session_id,
        event_type=event_type,
        tool_name=tool_name,
        summary=summary,
        payload=json.dumps(sanitized, ensure_ascii=False),
    )

    # Update session state
    if event_type == "Stop":
        await session_manager.mark_session_idle(conn, session_id)
    else:
        await session_manager.update_session_activity(conn, session_id)


# ── Summary generation ────────────────────────────────────────


def generate_summary(payload: dict[str, Any]) -> str:
    event_type = payload.get("hook_event_name", "")
    tool_name = payload.get("tool_name")
    tool_input = payload.get("tool_input") or {}

    if event_type == "SessionStart":
        model = payload.get("model", "unknown")
        source = payload.get("source", "startup")
        if source == "resume":
            return f"Session resumed (model: {model})"
        return f"Session started (model: {model})"

    if event_type == "PreToolUse":
        return _summarize_tool_use(tool_name, tool_input)

    if event_type == "PostToolUse":
        if tool_name == "Bash":
            cmd = _extract_bash_command(tool_input)
            exit_code = _extract_exit_code(payload.get("tool_response"))
            suffix = f" (exit {exit_code})" if exit_code is not None else ""
            return f"$ {_truncate(cmd, 100)}{suffix}"
        return f"{tool_name} completed"

    if event_type == "PostToolUseFailure":
        error = _truncate(str(payload.get("error", "")), 80)
        if tool_name == "Bash":
            cmd = _extract_bash_command(tool_input)
            return f"FAIL: Bash $ {_truncate(cmd, 60)} — {error}"
        return f"FAIL: {tool_name} — {error}"

    if event_type == "UserPromptSubmit":
        prompt = str(payload.get("prompt", ""))
        return f'User: "{_truncate(prompt, 80)}"'

    if event_type == "Stop":
        return "Session idle"

    return event_type


def _summarize_tool_use(tool_name: str | None, tool_input: Any) -> str:
    if not tool_name:
        return "Tool use"
    if tool_name == "Bash":
        cmd = _extract_bash_command(tool_input)
        return f"$ {_truncate(cmd, 120)}"
    if tool_name in ("Write", "Read", "Edit"):
        path = _extract_file_path(tool_input)
        return f"{tool_name} {path}"
    if tool_name in ("Grep", "Glob"):
        pattern = _safe_get(tool_input, "pattern", "")
        return f'{tool_name} "{_truncate(pattern, 60)}"'
    if tool_name == "Agent":
        desc = _safe_get(tool_input, "description", "")
        return f"Agent: {_truncate(desc, 80)}"
    return tool_name


# ── Payload sanitization ─────────────────────────────────────


def _sanitize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Strip sensitive/large content before storing."""
    p = dict(payload)
    tool_name = p.get("tool_name")
    tool_input = p.get("tool_input")

    # Strip file content from Write/Edit
    if tool_name in ("Write", "Edit") and isinstance(tool_input, dict):
        sanitized_input = dict(tool_input)
        sanitized_input.pop("content", None)
        sanitized_input.pop("new_string", None)
        sanitized_input.pop("old_string", None)
        p["tool_input"] = sanitized_input

    # Strip tool_response content for Write/Edit
    if tool_name in ("Write", "Edit"):
        p.pop("tool_response", None)

    # Truncate user prompt
    if p.get("hook_event_name") == "UserPromptSubmit" and "prompt" in p:
        p["prompt"] = _truncate(str(p["prompt"]), 200)

    # Strip transcript_path (local file path, not useful in DB)
    p.pop("transcript_path", None)

    return p


# ── Helpers ───────────────────────────────────────────────────


def _extract_bash_command(tool_input: Any) -> str:
    if isinstance(tool_input, dict):
        return str(tool_input.get("command", ""))
    if isinstance(tool_input, str):
        return tool_input
    return ""


def _extract_file_path(tool_input: Any) -> str:
    if isinstance(tool_input, dict):
        return str(tool_input.get("file_path", ""))
    return ""


def _extract_exit_code(tool_response: Any) -> int | None:
    if isinstance(tool_response, dict):
        code = tool_response.get("exitCode")
        if code is None:
            code = tool_response.get("exit_code")
        if code is not None:
            try:
                return int(code)
            except (ValueError, TypeError):
                pass
    return None


def _safe_get(obj: Any, key: str, default: str = "") -> str:
    if isinstance(obj, dict):
        return str(obj.get(key, default))
    return default


def _truncate(s: str, max_len: int) -> str:
    if len(s) <= max_len:
        return s
    return s[: max_len - 3] + "..."
