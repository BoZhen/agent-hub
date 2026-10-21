from __future__ import annotations

import json
import logging
from typing import Any, Callable

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)
router = APIRouter()


class Broadcaster:
    """Manages connected WebSocket clients and broadcasts events.

    Listeners are plain functions called with every broadcast message,
    after the clients; services/attention.py uses one to learn that
    session state may have changed.
    """

    def __init__(self):
        self._clients: list[WebSocket] = []
        self._listeners: list[Callable[[dict[str, Any]], None]] = []

    def add_listener(self, fn: Callable[[dict[str, Any]], None]) -> None:
        self._listeners.append(fn)

    def remove_listener(self, fn: Callable[[dict[str, Any]], None]) -> None:
        if fn in self._listeners:
            self._listeners.remove(fn)

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self._clients.append(ws)
        logger.info("WebSocket client connected (%d total)", len(self._clients))

    def disconnect(self, ws: WebSocket):
        self._clients.remove(ws)
        logger.info("WebSocket client disconnected (%d total)", len(self._clients))

    async def broadcast(self, message: dict[str, Any]):
        data = json.dumps(message, ensure_ascii=False, default=str)
        dead: list[WebSocket] = []
        for ws in self._clients:
            try:
                await ws.send_text(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._clients.remove(ws)
        for fn in list(self._listeners):
            try:
                fn(message)
            except Exception:
                logger.exception("Broadcast listener failed")


# Singleton broadcaster
broadcaster = Broadcaster()


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await broadcaster.connect(ws)
    try:
        while True:
            # Keep connection alive; client doesn't send meaningful data
            await ws.receive_text()
    except WebSocketDisconnect:
        broadcaster.disconnect(ws)
