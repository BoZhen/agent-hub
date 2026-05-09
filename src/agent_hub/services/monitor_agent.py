"""Monitor agent — read-only assistant over hub state.

Scope (V1):
- Aggregate / query sessions and events across the hub.
- Translate natural-language requests into tool calls.
- NEVER decide on the user's behalf. No auto-approve, no auto-spawn.
  Mutating tools are intentionally absent in V1.

Provider:
- Uses any OpenAI-compatible Chat Completions endpoint via env vars
  (MONITOR_LLM_BASE_URL / MONITOR_LLM_API_KEY / MONITOR_LLM_MODEL).
- DeepSeek native, OpenRouter, vLLM/llama.cpp local, etc. — all work.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

import aiosqlite
from openai import AsyncOpenAI

from agent_hub import db

logger = logging.getLogger(__name__)

MAX_TOOL_ITERATIONS = 8

SYSTEM_PROMPT = """You are the Monitor for an Agent Hub managing Claude Code / Codex sessions on the user's machine.

Your job is to OBSERVE and SUMMARIZE — never to decide on the user's behalf. You can:
- Look up sessions, events, and aggregate counts via tools.
- Answer questions about what is running, what is idle, what was done.
- Summarize the contents of a session when asked.

You CANNOT (and have no tools to):
- Approve any pending tool calls.
- Spawn or kill sessions.
- Modify any state in the hub.

Style:
- Match the user's language (Chinese ↔ English).
- Be concise. Tables work well for multi-session answers.
- When you don't know something, say so — don't fabricate session ids or event details.
- Always include the session_id (or first 8 chars) when referring to a specific session, so the user can act on it themselves.
"""


# ── Tool registry ─────────────────────────────────────────────────────


def _tool_schemas() -> list[dict[str, Any]]:
    """OpenAI-compatible function-calling schemas."""
    return [
        {
            "type": "function",
            "function": {
                "name": "list_sessions",
                "description": "List sessions on this hub. Filter by status. Returns session_id, tool, status, tmux_session, model, cwd, last_seen_at.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "status": {
                            "type": "string",
                            "enum": ["active", "idle", "stopped"],
                            "description": "Optional filter. Omit to list all.",
                        },
                        "limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 200},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_session_detail",
                "description": "Get one session's metadata plus its most recent events.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "session_id": {"type": "string", "description": "Session UUID. First 8 chars also accepted (will resolve)."},
                        "event_limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 100},
                    },
                    "required": ["session_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_dashboard_overview",
                "description": "Aggregated counts across the hub: active / idle / stopped / waiting sessions and total events.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "search_events",
                "description": "Full-text search over event summaries (and tool names). Returns matching events newest-first.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Substring to match in summary or tool_name."},
                        "session_id": {"type": "string", "description": "Optional: scope the search to one session."},
                        "limit": {"type": "integer", "default": 30, "minimum": 1, "maximum": 100},
                    },
                    "required": ["query"],
                },
            },
        },
    ]


async def _resolve_session_id(conn: aiosqlite.Connection, sid: str) -> str | None:
    """Accept full UUID or 8-char prefix. Return canonical session_id."""
    sid = sid.strip()
    if not sid:
        return None
    if len(sid) >= 32:
        return sid
    cursor = await conn.execute(
        "SELECT session_id FROM sessions WHERE session_id LIKE ? LIMIT 2",
        (sid + "%",),
    )
    rows = list(await cursor.fetchall())
    if len(rows) == 1:
        return rows[0]["session_id"]
    return None


async def _tool_list_sessions(
    conn: aiosqlite.Connection, status: str | None = None, limit: int = 50
) -> dict[str, Any]:
    rows = await db.get_sessions(conn, status=status, limit=limit)
    out = []
    for r in rows:
        out.append({
            "session_id": r["session_id"],
            "tool": r.get("tool"),
            "status": r["status"],
            "tmux_session": r.get("tmux_session"),
            "model": r.get("model"),
            "cwd": r.get("cwd"),
            "pending_tool": r.get("pending_tool"),
            "last_seen_at": r.get("last_seen_at"),
        })
    return {"count": len(out), "sessions": out}


async def _tool_get_session_detail(
    conn: aiosqlite.Connection, session_id: str, event_limit: int = 20
) -> dict[str, Any]:
    sid = await _resolve_session_id(conn, session_id)
    if not sid:
        return {"error": f"session not found or ambiguous prefix: {session_id}"}
    session = await db.get_session(conn, sid)
    if not session:
        return {"error": f"session not found: {sid}"}
    events = await db.get_session_events_latest(conn, sid, n=event_limit)
    return {
        "session": {
            "session_id": session["session_id"],
            "tool": session.get("tool"),
            "status": session["status"],
            "model": session.get("model"),
            "tmux_session": session.get("tmux_session"),
            "cwd": session.get("cwd"),
            "started_at": session.get("started_at"),
            "last_seen_at": session.get("last_seen_at"),
            "pending_tool": session.get("pending_tool"),
            "pending_detail": session.get("pending_detail"),
            "input_tokens": session.get("input_tokens", 0),
            "output_tokens": session.get("output_tokens", 0),
            "parent_session_id": session.get("parent_session_id"),
        },
        "recent_events": [
            {
                "event_type": e["event_type"],
                "tool_name": e.get("tool_name"),
                "summary": e.get("summary"),
                "created_at": e["created_at"],
            }
            for e in events
        ],
    }


async def _tool_get_dashboard_overview(conn: aiosqlite.Connection) -> dict[str, Any]:
    return await db.get_stats(conn)


async def _tool_search_events(
    conn: aiosqlite.Connection,
    query: str,
    session_id: str | None = None,
    limit: int = 30,
) -> dict[str, Any]:
    sql = (
        "SELECT id, session_id, event_type, tool_name, summary, created_at "
        "FROM events WHERE (summary LIKE ? OR tool_name LIKE ?) "
    )
    params: list[Any] = [f"%{query}%", f"%{query}%"]
    if session_id:
        sid = await _resolve_session_id(conn, session_id)
        if sid:
            sql += "AND session_id = ? "
            params.append(sid)
    sql += "ORDER BY id DESC LIMIT ?"
    params.append(limit)
    cursor = await conn.execute(sql, params)
    rows = list(await cursor.fetchall())
    return {
        "count": len(rows),
        "events": [
            {
                "id": r["id"],
                "session_id": r["session_id"],
                "event_type": r["event_type"],
                "tool_name": r["tool_name"],
                "summary": r["summary"],
                "created_at": r["created_at"],
            }
            for r in rows
        ],
    }


_TOOL_DISPATCH = {
    "list_sessions": _tool_list_sessions,
    "get_session_detail": _tool_get_session_detail,
    "get_dashboard_overview": _tool_get_dashboard_overview,
    "search_events": _tool_search_events,
}


async def _execute_tool(
    conn: aiosqlite.Connection, name: str, args: dict[str, Any]
) -> dict[str, Any]:
    fn = _TOOL_DISPATCH.get(name)
    if not fn:
        return {"error": f"unknown tool: {name}"}
    try:
        return await fn(conn, **args)
    except TypeError as e:
        return {"error": f"bad arguments for {name}: {e}"}
    except Exception as e:
        logger.exception("tool %s failed", name)
        return {"error": f"{type(e).__name__}: {e}"}


# ── Agent loop ────────────────────────────────────────────────────────


class MonitorAgent:
    def __init__(self) -> None:
        self.base_url = os.environ.get("MONITOR_LLM_BASE_URL")
        self.api_key = os.environ.get("MONITOR_LLM_API_KEY")
        self.model = os.environ.get("MONITOR_LLM_MODEL")
        self._client: AsyncOpenAI | None = None

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)

    @property
    def client(self) -> AsyncOpenAI:
        if self._client is None:
            if not self.configured:
                raise RuntimeError(
                    "Monitor agent not configured. Set MONITOR_LLM_BASE_URL, "
                    "MONITOR_LLM_API_KEY, MONITOR_LLM_MODEL in .env."
                )
            # Some OpenAI-compat endpoints append /v1 themselves; trust the
            # user's base_url verbatim.
            self._client = AsyncOpenAI(base_url=self.base_url, api_key=self.api_key)
        return self._client

    async def chat(
        self,
        conn: aiosqlite.Connection,
        user_message: str,
        history: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Run one turn of the agent loop.

        Returns ``{"reply": str, "history": list, "tool_trace": list}``.
        Caller stores history client-side (browser) so we stay stateless.
        """
        messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": user_message})
        tool_trace: list[dict[str, Any]] = []
        assert self.model is not None  # gated by self.configured check above

        for _ in range(MAX_TOOL_ITERATIONS):
            resp = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,  # type: ignore[arg-type]
                tools=_tool_schemas(),  # type: ignore[arg-type]
                tool_choice="auto",
                temperature=0.3,
            )
            msg = resp.choices[0].message
            assistant_entry: dict[str, Any] = {
                "role": "assistant",
                "content": msg.content or "",
            }
            # DeepSeek (and some other thinking-enabled providers) attach a
            # `reasoning_content` field to the assistant message and refuse
            # subsequent calls unless we pass it back verbatim. The OpenAI
            # SDK doesn't model this in its types, so reach in via getattr.
            reasoning = getattr(msg, "reasoning_content", None)
            if reasoning:
                assistant_entry["reasoning_content"] = reasoning
            tool_calls = list(msg.tool_calls or [])
            # Filter to function-type tool calls (the only kind we declare).
            fn_calls = [tc for tc in tool_calls if getattr(tc, "function", None) is not None]
            if fn_calls:
                assistant_entry["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,  # type: ignore[union-attr]
                            "arguments": tc.function.arguments,  # type: ignore[union-attr]
                        },
                    }
                    for tc in fn_calls
                ]
            messages.append(assistant_entry)

            if not fn_calls:
                return {
                    "reply": msg.content or "",
                    "history": messages[1:],  # drop system from returned history
                    "tool_trace": tool_trace,
                }

            for tc in fn_calls:
                fn_name = tc.function.name  # type: ignore[union-attr]
                fn_args_raw = tc.function.arguments  # type: ignore[union-attr]
                try:
                    args = json.loads(fn_args_raw or "{}")
                except json.JSONDecodeError:
                    args = {}
                result = await _execute_tool(conn, fn_name, args)
                tool_trace.append({
                    "name": fn_name,
                    "args": args,
                    "result_preview": _preview(result),
                })
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(result, ensure_ascii=False, default=str),
                })

        return {
            "reply": "（达到工具调用上限,已中止。）",
            "history": messages[1:],
            "tool_trace": tool_trace,
        }


def _preview(obj: Any, limit: int = 200) -> str:
    s = json.dumps(obj, ensure_ascii=False, default=str)
    if len(s) > limit:
        return s[:limit] + "…"
    return s


_singleton: MonitorAgent | None = None


def get_agent() -> MonitorAgent:
    global _singleton
    if _singleton is None:
        _singleton = MonitorAgent()
    return _singleton
