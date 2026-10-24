"""Files sent from the phone page for an agent to look at.

On a desktop a screenshot reaches the CLI with Ctrl+V: Claude Code and
Codex read the image from the workstation's clipboard. A phone's clipboard
stays on the phone, so the phone page uploads the file (POST /api/uploads)
and the Hub keeps it in one directory (HubConfig.uploads_dir). A reply then
names the saved files, and services/tmux_input.py pastes each path on its
own into the agent pane: Claude Code and Codex turn a paste that is one
image path into an image attachment ("[Image #1]"). Other files, and every
file in pi and opencode, arrive as a path the agent can read.

Files older than KEEP_DAYS are removed on the next upload.
"""
from __future__ import annotations

import os
import re
import secrets
import time
from collections.abc import AsyncIterator
from pathlib import Path

MAX_BYTES = 50 * 1024 * 1024
KEEP_DAYS = 14
# The image types Claude Code attaches from a pasted path.
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp")
_NAME_MAX = 80
# Anything but letters, digits, dot, dash and underscore becomes a dash, so
# the saved path pastes as one word.
_UNSAFE = re.compile(r"[^\w.-]+")


class UploadError(ValueError):
    pass


def directory(config_dir: str) -> Path:
    return Path(config_dir).expanduser()


def safe_name(name: str) -> str:
    """The last component of `name`, with only safe characters, at most
    _NAME_MAX long (the extension kept)."""
    stem, suffix = os.path.splitext(_UNSAFE.sub("-", os.path.basename(name or "")))
    if len(suffix) > 10:
        stem, suffix = stem + suffix, ""
    stem = stem.strip(".-")[:_NAME_MAX - len(suffix)].strip(".-")
    return (stem or "file") + suffix


async def save(folder: Path, name: str, chunks: AsyncIterator[bytes]) -> Path:
    """Write the chunks to a new file in `folder` and return its path. The
    name starts with the time and a random tag, so uploads never collide."""
    folder.mkdir(parents=True, exist_ok=True)
    prune(folder)
    path = folder / f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}-{safe_name(name)}"
    part = path.with_name(f".{path.name}.part")
    size = 0
    try:
        with open(part, "wb") as f:
            async for chunk in chunks:
                size += len(chunk)
                if size > MAX_BYTES:
                    raise UploadError(f"Files are limited to {MAX_BYTES // 2**20} MB")
                f.write(chunk)
        if size == 0:
            raise UploadError("The file is empty")
        part.rename(path)
    finally:
        part.unlink(missing_ok=True)
    return path


def find(folder: Path, path: str) -> str | None:
    """`path`, resolved, when it names a file saved in `folder`; else None."""
    candidate = Path(path)
    if not candidate.is_absolute():
        return None
    try:
        resolved = candidate.resolve(strict=True)
        inside = resolved.parent == folder.resolve(strict=True)
    except OSError:
        return None
    return str(resolved) if inside and resolved.is_file() else None


def prune(folder: Path, now: float | None = None) -> None:
    cutoff = (time.time() if now is None else now) - KEEP_DAYS * 86400
    for entry in folder.iterdir():
        try:
            if entry.is_file() and entry.stat().st_mtime < cutoff:
                entry.unlink()
        except OSError:
            pass
