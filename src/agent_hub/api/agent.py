"""Monitor agent HTTP endpoints."""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from agent_hub.services.monitor_agent import get_agent

logger = logging.getLogger(__name__)
router = APIRouter()


class ChatRequest(BaseModel):
    message: str
    history: list[dict[str, Any]] | None = None


@router.get("/agent/status")
async def agent_status() -> dict[str, Any]:
    agent = get_agent()
    return {
        "configured": agent.configured,
        "model": agent.model,
        "base_url": agent.base_url,
    }


@router.post("/agent/chat")
async def agent_chat(request: Request, payload: ChatRequest) -> dict[str, Any]:
    agent = get_agent()
    if not agent.configured:
        raise HTTPException(
            status_code=503,
            detail="Monitor agent not configured (MONITOR_LLM_* env vars missing)",
        )
    if not payload.message.strip():
        raise HTTPException(status_code=400, detail="empty message")
    conn = request.app.state.db
    try:
        return await agent.chat(conn, payload.message, payload.history)
    except Exception as e:
        logger.exception("Monitor agent chat failed")
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")
