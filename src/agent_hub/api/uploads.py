"""POST /api/uploads: a file from the phone page for an agent (services/uploads.py)."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from agent_hub.services import uploads

router = APIRouter()

# Files one reply may carry.
MAX_ATTACHMENTS = 10


def _folder(request: Request):
    return uploads.directory(request.app.state.config.uploads_dir)


@router.post("/uploads")
async def upload(request: Request, name: str = Query(default="file")) -> dict[str, Any]:
    """Save the request body as a file and return its path. The phone page
    sends the path with its next reply (`attachments` of
    /sessions/{id}/send and /tmux/input)."""
    try:
        path = await uploads.save(_folder(request), name, request.stream())
    except uploads.UploadError as e:
        raise HTTPException(400, str(e))
    except OSError as e:
        raise HTTPException(500, f"Could not save the file: {e}")
    return {
        "path": str(path),
        "name": path.name,
        "size": path.stat().st_size,
        "image": path.suffix.lower() in uploads.IMAGE_SUFFIXES,
    }


def attachment_paths(request: Request, paths: list[str]) -> list[str]:
    """The attachments of a reply, each a file POST /uploads saved; 400 for
    anything else, so a reply cannot paste an arbitrary path."""
    if len(paths) > MAX_ATTACHMENTS:
        raise HTTPException(400, f"A reply takes at most {MAX_ATTACHMENTS} files")
    folder = _folder(request)
    found = []
    for path in paths:
        resolved = uploads.find(folder, path)
        if resolved is None:
            raise HTTPException(400, f"Attachment not found: {path}")
        found.append(resolved)
    return found
