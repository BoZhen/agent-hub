"""GET /api/stream: server-sent events for a phone that only needs to know
when a session needs the user (services/attention.py).

Events:
  snapshot  {"api", "sessions": [state…], "notices": [notice…],
             "server_time", "page_version"}
            sent first on every connection, so a client that was offline
            catches up by replacing what it had; api is the phone API
            version (attention.PHONE_API_VERSION); page_version tells an
            open phone page that the Hub now serves a newer one
  session   one session's state after it changed
  removed   {"session_id"}: the session ended or was deleted
  notice    a message from POST /api/notify

A comment line goes out every 20 s, so both ends notice a dead connection.
Server-sent events rather than a WebSocket: they are a plain HTTP response
read line by line, which an Android app can do without a library.
"""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from agent_hub.services.attention import attention
from agent_hub.web.routes import _static_version

router = APIRouter()

HEARTBEAT_S = 20.0


def _event(kind: str, data: dict) -> str:
    return f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


@router.get("/stream")
async def stream():
    sub, snapshot = await attention.subscribe()
    snapshot["page_version"] = _static_version()

    async def body():
        try:
            yield "retry: 5000\n\n"
            yield _event("snapshot", snapshot)
            while True:
                try:
                    kind, data = await asyncio.wait_for(sub.queue.get(), timeout=HEARTBEAT_S)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                if kind is None:
                    break
                yield _event(kind, data)
        finally:
            attention.unsubscribe(sub)

    return StreamingResponse(
        body(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.get("/attention")
async def snapshot():
    """The same states as the stream's snapshot, as one JSON response."""
    await attention.refresh()
    return attention.snapshot()
