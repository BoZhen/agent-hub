"""Hub-internal endpoints — used by tmux hooks the hub installs on
itself, not by external clients. Kept on a separate router so the
public surface area is obvious from `/api/internal/*`.
"""
from __future__ import annotations

import logging
import socket

from fastapi import APIRouter, Request

from agent_hub.services import session_manager

logger = logging.getLogger(__name__)
router = APIRouter()
_LOCAL_HOSTNAME = socket.gethostname()


@router.post("/internal/tmux-discovered")
async def tmux_discovered(request: Request, name: str) -> dict:
    """Push notification from the tmux ``session-created`` hook.

    The hook (installed at hub startup) curls this endpoint with the
    new tmux session name. We immediately run codex-discovery for that
    one tmux instead of waiting for the 60 s polling fallback. Result:
    new codex TUIs land on the dashboard within ~50 ms of being
    started.
    """
    if not name:
        return {"ok": False, "error": "missing name"}
    conn = request.app.state.db
    config = request.app.state.config
    try:
        created = await session_manager.discover_codex_for_tmux(
            conn, config.hub_id, _LOCAL_HOSTNAME, name,
        )
    except Exception:
        logger.exception("tmux-discovered handler failed for %s", name)
        return {"ok": False, "error": "handler failed"}
    return {"ok": True, "created": bool(created), "name": name}
