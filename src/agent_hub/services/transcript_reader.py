from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def read_usage(transcript_path: str) -> dict | None:
    """Parse a Claude Code transcript (.jsonl) and return aggregated usage and latest model."""
    path = Path(transcript_path)
    if not path.exists():
        return None

    input_tokens = 0
    output_tokens = 0
    cache_read_tokens = 0
    cache_create_tokens = 0
    model = None

    try:
        with open(path) as f:
            for line in f:
                msg = json.loads(line)
                if msg.get("type") != "assistant" or "message" not in msg:
                    continue
                message = msg["message"]
                if message.get("model"):
                    model = message["model"]
                usage = message.get("usage")
                if not usage:
                    continue
                input_tokens += usage.get("input_tokens", 0)
                output_tokens += usage.get("output_tokens", 0)
                cache_read_tokens += usage.get("cache_read_input_tokens", 0)
                cache_create_tokens += usage.get("cache_creation_input_tokens", 0)
    except Exception:
        logger.debug("Failed to read transcript: %s", transcript_path)
        return None

    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_create_tokens": cache_create_tokens,
        "model": model,
    }


def _tool_detail(name: str, inp: dict) -> str:
    """Extract a human-readable detail string from tool input."""
    if name == "Bash":
        return str(inp.get("command", ""))[:150]
    elif name in ("Read", "Write"):
        return str(inp.get("file_path", ""))
    elif name == "Edit":
        return str(inp.get("file_path", ""))
    elif name in ("Grep", "Glob"):
        return str(inp.get("pattern", ""))
    elif name == "Agent":
        return str(inp.get("description", ""))[:100]
    elif name == "WebSearch":
        return str(inp.get("query", ""))[:100]
    elif name == "WebFetch":
        return str(inp.get("url", ""))[:100]
    return ""


class PendingTool:
    """Info about a pending tool_use detected in a transcript."""
    __slots__ = ("name", "detail", "reasoning", "user_prompt")

    def __init__(
        self, name: str, detail: str,
        reasoning: str = "", user_prompt: str = "",
    ) -> None:
        self.name = name
        self.detail = detail
        self.reasoning = reasoning
        self.user_prompt = user_prompt


def read_pending_tool(transcript_path: str) -> PendingTool | None:
    """Check if the last transcript entry is an assistant message with a pending tool_use.

    Returns PendingTool with tool name, detail, reasoning text, and last user prompt.
    """
    path = Path(transcript_path)
    if not path.exists():
        return None

    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            # Read last 256KB — needs to be large enough to span past
            # attachment entries (embedded PDF pages can be very large).
            f.seek(max(0, size - 262144))
            pos = f.tell()
            data = f.read().decode("utf-8", errors="replace")

        lines = data.strip().splitlines()
        if not lines:
            return None
        # Skip first partial line if we seeked mid-file
        if pos > 0:
            lines = lines[1:]
        if not lines:
            return None

        # Collect tool_use IDs that have been resolved (have a tool_result).
        resolved_ids: set[str] = set()
        for line in lines:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("type") != "user":
                continue
            msg = entry.get("message", {})
            blocks = msg.get("content", []) if isinstance(msg, dict) else []
            if not isinstance(blocks, list):
                continue
            for b in blocks:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    tid = b.get("tool_use_id", "")
                    if tid:
                        resolved_ids.add(tid)

        # Scan backwards for the last assistant message with an unresolved tool_use.
        last = None
        for candidate_line in reversed(lines):
            try:
                candidate = json.loads(candidate_line)
            except json.JSONDecodeError:
                continue
            if candidate.get("type") != "assistant":
                continue
            content = candidate.get("message", {}).get("content", [])
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    if b.get("id", "") not in resolved_ids:
                        last = candidate
                        break
            if last is not None:
                break
        if last is None:
            return None

        content = last.get("message", {}).get("content", [])

        # Collect text blocks before the first tool_use as reasoning
        reasoning_parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                reasoning_parts.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                name = block.get("name", "")
                detail = _tool_detail(name, block.get("input", {}))
                reasoning = "\n".join(reasoning_parts).strip()
                # Truncate reasoning to last meaningful chunk
                if len(reasoning) > 300:
                    reasoning = "..." + reasoning[-297:]

                # Find last user prompt by scanning backwards
                user_prompt = ""
                for prev_line in reversed(lines[:-1]):
                    try:
                        prev = json.loads(prev_line)
                    except json.JSONDecodeError:
                        continue
                    if prev.get("type") == "user":
                        msg = prev.get("message", {})
                        msg_content = msg.get("content") if isinstance(msg, dict) else None
                        if isinstance(msg_content, str):
                            user_prompt = msg_content[:200]
                        elif isinstance(msg_content, list):
                            for b in msg_content:
                                if isinstance(b, dict) and b.get("type") == "text":
                                    user_prompt = b.get("text", "")[:200]
                                    break
                        break

                return PendingTool(name, detail, reasoning, user_prompt)
        return None
    except Exception:
        return None


def read_transcript_tail(transcript_path: str, max_bytes: int = 262144) -> list[dict]:
    """Read recent entries from transcript tail. Returns parsed JSON objects."""
    path = Path(transcript_path)
    if not path.exists():
        return []

    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            pos = max(0, size - max_bytes)
            f.seek(pos)
            data = f.read().decode("utf-8", errors="replace")

        lines = data.strip().splitlines()
        if pos > 0:
            lines = lines[1:]  # skip partial first line

        entries = []
        for line in lines:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return entries
    except Exception:
        return []


def summarize_transcript(transcript_path: str) -> str | None:
    """Extract a structured summary of recent transcript activity."""
    entries = read_transcript_tail(transcript_path)
    if not entries:
        return None

    user_prompts: list[str] = []
    tool_calls: list[str] = []
    text_responses: list[str] = []

    for entry in entries:
        etype = entry.get("type", "")

        if etype == "user":
            # Extract user message text
            message = entry.get("message", {})
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, str):
                user_prompts.append(content[:200])
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        user_prompts.append(block.get("text", "")[:200])
                        break

        elif etype == "assistant":
            message = entry.get("message", {})
            content = message.get("content", [])
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    text = block.get("text", "")
                    if len(text) > 20:  # skip trivial text
                        text_responses.append(text[:300])
                elif block.get("type") == "tool_use":
                    name = block.get("name", "?")
                    inp = block.get("input", {})
                    detail = ""
                    if name == "Bash":
                        detail = f": {str(inp.get('command', ''))[:120]}"
                    elif name in ("Read", "Write", "Edit"):
                        detail = f": {inp.get('file_path', '')}"
                    elif name in ("Grep", "Glob"):
                        detail = f": {inp.get('pattern', '')}"
                    elif name == "Agent":
                        detail = f": {inp.get('description', '')}"
                    tool_calls.append(f"{name}{detail}")

    lines: list[str] = []

    # Show last few user prompts
    recent_prompts = user_prompts[-5:]
    if recent_prompts:
        lines.append("=== Recent User Prompts ===")
        for p in recent_prompts:
            lines.append(f"  > {p}")
        lines.append("")

    # Show last tool calls
    recent_tools = tool_calls[-20:]
    if recent_tools:
        lines.append("=== Recent Tool Calls ===")
        for t in recent_tools:
            lines.append(f"  {t}")
        lines.append("")

    # Show last text responses (truncated)
    recent_text = text_responses[-3:]
    if recent_text:
        lines.append("=== Recent Assistant Responses ===")
        for t in recent_text:
            lines.append(f"  {t}")

    return "\n".join(lines) if lines else "No meaningful activity found in transcript."
