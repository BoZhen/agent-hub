from datetime import datetime

from pydantic import BaseModel


class SessionResponse(BaseModel):
    session_id: str
    hub_id: str
    hostname: str
    cwd: str
    model: str | None = None
    status: str
    started_at: datetime
    last_seen_at: datetime
    stopped_at: datetime | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_create_tokens: int = 0
    pending_tool: str | None = None
    pending_detail: str | None = None
    pending_always_label: str | None = None
    tmux_session: str | None = None
    pinned: bool = False

class EventResponse(BaseModel):
    id: int
    event_uid: str
    session_id: str
    event_type: str
    tool_name: str | None = None
    summary: str | None = None
    created_at: datetime

class StatsResponse(BaseModel):
    active_sessions: int
    idle_sessions: int
    stopped_sessions: int
    waiting_sessions: int
    total_events: int
