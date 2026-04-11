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
