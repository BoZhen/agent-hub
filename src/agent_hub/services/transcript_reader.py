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


def read_pending_tool(transcript_path: str) -> str | None:
    """Check if the last transcript entry is an assistant message with a pending tool_use."""
    path = Path(transcript_path)
    if not path.exists():
        return None

    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            # Read last 16KB — enough for several messages
            f.seek(max(0, size - 16384))
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

        last = json.loads(lines[-1])
        if last.get("type") != "assistant":
            return None

        content = last.get("message", {}).get("content", [])
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                return block.get("name")
        return None
    except Exception:
        return None
