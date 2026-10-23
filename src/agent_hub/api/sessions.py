from __future__ import annotations

import asyncio
import os
import time

import httpx
from fastapi import APIRouter, HTTPException, Query, Request

from pydantic import BaseModel

from agent_hub import db
from agent_hub.api.ws import broadcaster
from agent_hub.models import EventResponse, SessionResponse, StatsResponse
from agent_hub.services import conversation, tmux_input
from agent_hub.services.attention import attention
from agent_hub.services.session_manager import mark_approved_suppress
from agent_hub.services.restore import (
    _list_alive_tmux_names,
    _resume_hint_command,
    is_codex_tool,
    find_orphan_sessions,
    restart_live_sessions,
    restart_session,
    restore_all_orphans,
    restore_session,
    session_is_busy,
)

router = APIRouter()


@router.get("/sessions", response_model=list[SessionResponse])
async def list_sessions(
    request: Request,
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    conn = request.app.state.db
    rows = await db.get_sessions(conn, status=status, limit=limit, offset=offset)
    return rows


@router.get("/orphan-sessions")
async def list_orphans(request: Request):
    """List sessions whose tmux is dead (restore candidates).

    Path is `/orphan-sessions` (not `/sessions/orphans`) to avoid
    collision with the parameterized `/sessions/{session_id}` route.
    """
    conn = request.app.state.db
    return {"orphans": await find_orphan_sessions(conn)}


@router.post("/restore-orphans")
async def restore_all(request: Request):
    """Recreate tmux for every orphan session and resume Claude/Codex."""
    conn = request.app.state.db
    return await restore_all_orphans(conn, config=request.app.state.config)


@router.get("/sessions/{session_id}", response_model=SessionResponse)
async def get_session(request: Request, session_id: str):
    conn = request.app.state.db
    row = await db.get_session(conn, session_id)
    if not row:
        raise HTTPException(status_code=404, detail="Session not found")
    row["resume_command"] = _resume_hint_command(row.get("tool") or "", session_id) or None
    return row


@router.get("/sessions/{session_id}/events", response_model=list[EventResponse])
async def list_session_events(
    request: Request,
    session_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    conn = request.app.state.db
    session = await db.get_session(conn, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    rows = await db.get_session_events(conn, session_id, limit=limit, offset=offset)
    return rows


@router.get(
    "/sessions/{session_id}/events/latest", response_model=list[EventResponse]
)
async def latest_session_events(
    request: Request,
    session_id: str,
    n: int = Query(default=10, ge=1, le=100),
):
    conn = request.app.state.db
    rows = await db.get_session_events_latest(conn, session_id, n=n)
    return rows


@router.get("/stats", response_model=StatsResponse)
async def get_stats(request: Request):
    conn = request.app.state.db
    return await db.get_stats(conn)


@router.post("/sessions/clear-stopped")
async def clear_stopped_sessions(request: Request):
    """Bulk-delete every session with status='stopped' (and its events)."""
    conn = request.app.state.db
    count = await db.delete_stopped_sessions(conn)
    return {"ok": True, "deleted": count}


@router.delete("/sessions/{session_id}")
async def delete_session(request: Request, session_id: str):
    """Delete a session and its events. For idle sessions with a
    live tmux, also kill the tmux (terminates Claude) so the delete
    is final — otherwise the session would reappear on the next
    hook event."""
    conn = request.app.state.db
    session = await db.get_session(conn, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if session["status"] == "active":
        raise HTTPException(
            status_code=400,
            detail="Cannot delete an active session — wait until idle",
        )

    tmux_name = session.get("tmux_session")
    if session["status"] == "idle" and tmux_name:
        proc = await asyncio.create_subprocess_exec(
            "tmux", "kill-session", "-t", f"{tmux_name}:",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.communicate()
        # Non-zero return is fine — tmux may already be gone.

    deleted = await db.delete_session(conn, session_id)
    if not deleted:
        raise HTTPException(status_code=500, detail="Failed to delete session")
    return {"ok": True}


@router.post("/sessions/{session_id}/resume")
async def resume_one(request: Request, session_id: str):
    """Bring one session back: recreate its tmux and run the CLI's own
    resume command inside. Works for ended sessions and for live rows
    whose tmux went missing; a session that is still running is left
    alone."""
    conn = request.app.state.db
    session = await db.get_session(conn, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    tmux_name = session.get("tmux_session")
    if session["status"] != "stopped" and tmux_name and tmux_name in await _list_alive_tmux_names():
        raise HTTPException(status_code=409, detail="Session is still running; open it instead")
    if not session.get("tool"):
        raise HTTPException(status_code=400, detail="Session has no tool recorded, so there is no resume command")
    result = await restore_session(conn, session, mode="resume", config=request.app.state.config)
    if not result.get("ok"):
        raise HTTPException(status_code=500, detail=result.get("error"))
    return result


@router.post("/sessions/{session_id}/restore")
async def restore_one(request: Request, session_id: str):
    """Older name for /resume, kept for existing callers."""
    return await resume_one(request, session_id)


@router.post("/sessions/{session_id}/restart")
async def restart_one(
    request: Request,
    session_id: str,
    force: bool = Query(default=False),
):
    """Restart a running session's CLI in its own tmux, resuming the same
    conversation, so it runs the version installed now. A session in the
    middle of a turn is refused unless `force` is set, since the restart
    ends that turn."""
    conn = request.app.state.db
    session = await db.get_session(conn, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    tmux_name = session.get("tmux_session")
    if session["status"] == "stopped" or not tmux_name or tmux_name not in await _list_alive_tmux_names():
        raise HTTPException(status_code=409, detail="Session is not running; resume it instead")
    if session.get("parent_session_id"):
        raise HTTPException(status_code=409, detail="A subagent runs inside its parent's CLI; restart the parent")
    if not force and await session_is_busy(session):
        raise HTTPException(status_code=409, detail="Session is in the middle of a turn")
    result = await restart_session(session, config=request.app.state.config)
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("error"))
    return result


class RestartAllRequest(BaseModel):
    tool: str  # "claude" or "codex"


@router.post("/restart-sessions")
async def restart_all(request: Request, req: RestartAllRequest):
    """Restart every running Claude session, or every running Codex
    session, so they run the version installed now. Sessions in the middle
    of a turn are skipped and listed with the reason."""
    if req.tool not in ("claude", "codex"):
        raise HTTPException(status_code=400, detail="tool must be claude or codex")
    return await restart_live_sessions(
        request.app.state.db, req.tool, config=request.app.state.config
    )


@router.post("/sessions/{session_id}/approve")
async def approve_session(
    request: Request,
    session_id: str,
    always: bool = Query(default=False),
):
    """Send approval to the tmux session where the agent is running.

    Dispatch by tool:
    - Claude: option 1 via Web Terminal `y\\n`, option 2 via tmux
      `Down Enter` (tmux send-keys can't send single keys for Claude's
      approval navigation without Web Terminal mediation).
    - Codex: `Enter` confirms option 1 (default highlighted); `Down
      Enter` navigates to option 2 and confirms. We originally tried
      the single-key shortcuts `(y)` / `(p)` codex advertises, but
      that left the key char echoing in codex's prompt input after
      the UI dismissed. `Enter`-based navigation leaves no residue
      and mirrors the Claude Always path, so both tools share the
      same key sequence for option 2.

    The `always=True` path is guarded against stale state: if the
    session has no active `pending_always_label`, we 400. This
    protects both Claude and Codex from a notification button pressed
    after the user already resolved the prompt manually.
    """
    conn = request.app.state.db
    session = await db.get_session(conn, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    tmux_name = session.get("tmux_session")
    if not tmux_name:
        raise HTTPException(
            status_code=400,
            detail="No tmux session associated with this session. The agent must be running inside tmux.",
        )

    if always and not session.get("pending_always_label"):
        raise HTTPException(
            status_code=400,
            detail="No active 'always' option on this session",
        )

    # Capture the signature BEFORE sending keys so the periodic
    # pending-check can suppress re-broadcasting this exact approval
    # while claude/codex hasn't dismissed the pane UI yet. Without
    # this, a back-to-back same-signature approval (e.g., two Bash
    # `ls` in a row) looks identical to the stale DB cache and
    # never broadcasts — the badge stays cleared until the user
    # manually refreshes.
    approval_sig = (
        session.get("pending_tool"),
        session.get("pending_detail"),
        session.get("pending_always_label"),
    )

    tool = session.get("tool") or "claude"

    if is_codex_tool(tool):
        # Option 1 is highlighted by default in every codex approval
        # UI; Enter confirms it.
        #
        # For Always, the key sequence depends on which UI variant
        # is up:
        # - Bash 3-option:  Always is option 2 → 1 Down + Enter
        #   (Yes / Yes, and don't ask again... / No)
        # - Edit 3-option:  Always is option 2 → 1 Down + Enter
        #   (Yes, proceed / Yes, and don't ask again for these
        #   files / No) — sandbox-retry path.
        # - MCP 4-option:   Always is option 3 → 2 Downs + Enter
        #   (Allow / Allow for this session / Always allow / Cancel)
        #
        # We derive the down count from `pending_tool`, which the
        # parser set to "Bash" / "Edit" / "MCP" when it recognized
        # the UI. No new DB column — the discriminator is already
        # in the existing pending-state fields.
        if always:
            down_count = 2 if session.get("pending_tool") == "MCP" else 1
            keys: tuple[str, ...] = tuple(["Down"] * down_count) + ("Enter",)
        else:
            keys = ("Enter",)
        proc = await asyncio.create_subprocess_exec(
            "tmux", "send-keys", "-t", f"{tmux_name}:", *keys,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            err_msg = stderr.decode().strip() if stderr else "unknown error"
            raise HTTPException(
                status_code=400,
                detail=f"tmux send-keys failed: {err_msg}",
            )
        mark_approved_suppress(session_id, *approval_sig)
        await db.update_session_pending_tool(conn, session_id, None, None)
        # Broadcast cleared state immediately so the dashboard
        # clears its badge / always label / dot / waiting counter
        # without waiting for the next periodic tick — which
        # wouldn't fire a clear broadcast anyway since db_tool is
        # already None. Also covers approvals from the phone, which
        # have no optimistic JS running on the dashboard.
        stats = await db.get_stats(conn)
        await broadcaster.broadcast({
            "type": "pending",
            "session_id": session_id,
            "pending_tool": None,
            "pending_detail": None,
            "tmux_session": tmux_name,
            "waiting_count": stats["waiting_sessions"],
        })
        return {"ok": True, "tmux_session": tmux_name, "always": always}

    if always:
        # Claude option 2: Down arrow + Enter, via direct tmux send-keys.
        # Trailing colon forces session-level target (avoids pane lookup errors).
        proc = await asyncio.create_subprocess_exec(
            "tmux", "send-keys", "-t", f"{tmux_name}:", "Down", "Enter",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            err_msg = stderr.decode().strip() if stderr else "unknown error"
            raise HTTPException(
                status_code=400,
                detail=f"tmux send-keys failed: {err_msg}",
            )
    else:
        # Claude option 1: send 'y' + Enter via Web Terminal API
        terminal_port = request.app.state.config.terminal_port
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"http://127.0.0.1:{terminal_port}/api/terminals/{tmux_name}/send",
                    json={"keys": "y\n"},
                    timeout=5.0,
                )
                if resp.status_code == 404:
                    raise HTTPException(
                        status_code=400,
                        detail=f"tmux session '{tmux_name}' not found on terminal server",
                    )
                resp.raise_for_status()
        except httpx.HTTPError as e:
            raise HTTPException(status_code=502, detail=f"Terminal server error: {e}")

    mark_approved_suppress(session_id, *approval_sig)
    await db.update_session_pending_tool(conn, session_id, None, None)
    # Mirror the codex-branch broadcast: emit a cleared pending
    # event so the dashboard, the phone stream and any other
    # subscriber updates immediately instead of waiting on a
    # periodic-tick clear (which no longer fires — see codex branch).
    stats = await db.get_stats(conn)
    await broadcaster.broadcast({
        "type": "pending",
        "session_id": session_id,
        "pending_tool": None,
        "pending_detail": None,
        "tmux_session": tmux_name,
        "waiting_count": stats["waiting_sessions"],
    })
    return {"ok": True, "tmux_session": tmux_name, "always": always}


@router.post("/sessions/{session_id}/seen")
async def mark_seen(request: Request, session_id: str):
    """The user has looked at the session, so its finished turn no
    longer needs them (see services/attention.py)."""
    conn = request.app.state.db
    if not await db.get_session(conn, session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    await db.mark_session_seen(conn, session_id)
    attention.poke()
    return {"ok": True}


class SendRequest(BaseModel):
    text: str
    # False leaves the text in the input box without pressing Enter.
    submit: bool = True


_SEND_MAX_CHARS = 20000
# Pause between the paste and the Enter, so the TUI has finished taking
# the paste in; an Enter that arrives with it would become a newline.
_SUBMIT_DELAY_S = 0.3


async def _running_agent_pane(conn, session_id: str) -> tuple[dict, str]:
    """The session and the tmux pane its agent runs in, or the HTTP error
    for why there is none."""
    session = await db.get_session(conn, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    tmux_name = session.get("tmux_session")
    if not tmux_name or session["status"] == "stopped":
        raise HTTPException(status_code=409, detail="Session is not running")
    try:
        pane = await tmux_input.agent_pane(tmux_name)
    except tmux_input.NoAgentError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except tmux_input.TmuxInputError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return session, pane


@router.post("/sessions/{session_id}/send")
async def send_text(request: Request, session_id: str, body: SendRequest):
    """Type a message into the session's agent pane and press Enter.

    The text goes in as one bracketed paste (services/tmux_input.py), so
    a multi-line message is one prompt. A session waiting for an approval
    is refused: its pane shows a menu, and pasted letters could pick an
    option. So is a tmux whose agent has exited, where the text would run
    as a shell command.
    """
    conn = request.app.state.db
    text = body.text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Message is empty")
    if len(text) > _SEND_MAX_CHARS:
        raise HTTPException(status_code=400, detail=f"Message is limited to {_SEND_MAX_CHARS} characters")
    session, pane = await _running_agent_pane(conn, session_id)
    if session.get("pending_tool"):
        raise HTTPException(
            status_code=409,
            detail="Session is waiting for an approval; approve or deny it first",
        )
    try:
        await tmux_input.paste(pane, text)
        if body.submit:
            await asyncio.sleep(_SUBMIT_DELAY_S)
            await tmux_input.press(pane, "Enter")
    except tmux_input.TmuxInputError as e:
        raise HTTPException(status_code=502, detail=str(e))
    await db.mark_session_seen(conn, session_id)
    attention.poke()
    return {"ok": True, "submitted": body.submit}


# Last /interrupt per session (monotonic seconds).
_last_interrupt: dict[str, float] = {}
_INTERRUPT_GAP_S = 2.0


@router.post("/sessions/{session_id}/interrupt")
async def interrupt(request: Request, session_id: str):
    """Press Escape in the agent pane. It stops a running turn, and on an
    approval prompt it declines.

    A second press within two seconds is ignored: in Claude Code two
    Escapes in a row open the menu for going back to an earlier message.
    """
    conn = request.app.state.db
    _, pane = await _running_agent_pane(conn, session_id)
    now = time.monotonic()
    if now - _last_interrupt.get(session_id, -_INTERRUPT_GAP_S) < _INTERRUPT_GAP_S:
        return {"ok": True, "ignored": True}
    _last_interrupt[session_id] = now
    try:
        await tmux_input.press(pane, "Escape")
    except tmux_input.TmuxInputError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"ok": True, "ignored": False}


@router.get("/sessions/{session_id}/messages")
async def session_messages(
    request: Request,
    session_id: str,
    limit: int = Query(default=30, ge=1, le=200),
):
    """The last messages of the session (user, assistant, one-line tool
    calls), read from the CLI's transcript. Empty when the Hub does not
    know the transcript, as for a Codex session that has not had its
    first prompt yet."""
    conn = request.app.state.db
    session = await db.get_session(conn, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    config = request.app.state.config
    path = conversation.find_transcript(session, ["~/.claude", config.claude2_config_dir])
    if not path:
        return {"session_id": session_id, "items": []}
    items = await asyncio.to_thread(conversation.read_messages, path, session.get("tool"), limit)
    return {"session_id": session_id, "items": items}


class ForkRequest(BaseModel):
    name: str | None = None  # default: <source>-fork-N


@router.post("/sessions/{session_id}/fork")
async def fork_session(request: Request, session_id: str, body: ForkRequest):
    """Fork a Claude session into a fresh tmux that inherits the source
    session's conversation history.

    Equivalent to `tmux new -d -s <new> -c <cwd> claude --resume <sid>
    --fork-session`. The source session is not touched — its claude
    process keeps running in its own tmux. Only Claude sources are
    supported; codex has no scriptable fork CLI.
    """
    import os
    import re
    from pathlib import Path

    from agent_hub.api.tmux import (
        _resolve_command,
        _tmux_ls,
        _valid_tmux_name,
        launch_env,
        session_path,
    )
    from agent_hub.services.session_manager import (
        mark_hub_launched,
        register_pending_fork,
    )

    conn = request.app.state.db
    src = await db.get_session(conn, session_id)
    if not src:
        raise HTTPException(status_code=404, detail="Session not found")
    tool = src.get("tool") or ""
    if "claude" not in tool:
        raise HTTPException(
            status_code=400,
            detail="Spawn only supports Claude sessions; codex fork has no scriptable CLI yet",
        )
    if src.get("transferred"):
        raise HTTPException(
            status_code=400,
            detail="Cannot fork a remote (transferred) session — the fork must run on the source machine",
        )
    src_cwd = src.get("cwd")
    if not src_cwd:
        raise HTTPException(status_code=400, detail="Source session has no cwd recorded")
    cwd_path = Path(src_cwd).expanduser()
    try:
        resolved_cwd = cwd_path.resolve(strict=True)
    except FileNotFoundError:
        raise HTTPException(status_code=400, detail=f"Source cwd no longer exists: {src_cwd}")
    if not resolved_cwd.is_dir():
        raise HTTPException(status_code=400, detail=f"Source cwd is not a directory: {src_cwd}")
    if not os.access(resolved_cwd, os.R_OK | os.X_OK):
        raise HTTPException(status_code=400, detail=f"Source cwd is not accessible: {src_cwd}")

    cmd_path = _resolve_command("claude")
    if not cmd_path:
        raise HTTPException(status_code=400, detail="claude binary not found in PATH")

    existing_sessions = await _tmux_ls()
    existing_names = {s["name"] for s in existing_sessions}

    if body.name:
        name = body.name.strip().replace("–", "-").replace("—", "-")
        if not _valid_tmux_name(name):
            raise HTTPException(
                status_code=400,
                detail="tmux name must be 1-64 ASCII letters/digits/underscore/hyphen",
            )
        if name in existing_names:
            raise HTTPException(status_code=409, detail=f"tmux session '{name}' already exists")
    else:
        base = src.get("tmux_session") or "fork"
        base = re.sub(r"[^a-zA-Z0-9_-]+", "-", base).strip("-") or "fork"
        if len(base) > 55:
            base = base[:55].rstrip("-") or "fork"
        i = 1
        while f"{base}-fork-{i}" in existing_names:
            i += 1
        name = f"{base}-fork-{i}"

    claude_args = ["--resume", session_id, "--fork-session"]
    if "yolo" in tool:
        claude_args.append("--dangerously-skip-permissions")

    # Pin hub's augmented PATH so the forked tmux pane inherits
    # node/nvm/fnm dirs even when the long-running tmux server's PATH
    # predates them (see tmux.py for the same rationale).
    tmux_args = ["tmux", "new-session", "-d", "-s", name, "-c", str(resolved_cwd)]
    path = session_path()
    if path:
        tmux_args.extend(["-e", f"PATH={path}"])
    for key, value in launch_env(tool, request.app.state.config).items():
        tmux_args.extend(["-e", f"{key}={value}"])
    tmux_args.extend([cmd_path, *claude_args])
    proc = await asyncio.create_subprocess_exec(
        *tmux_args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        err = stderr.decode("utf-8", errors="replace").strip() or "unknown"
        raise HTTPException(status_code=500, detail=f"tmux new-session failed: {err}")

    mark_hub_launched(name, tool if tool == "claude2" else "claude-fork")
    register_pending_fork(name, str(resolved_cwd))
    return {"name": name, "cwd": str(resolved_cwd), "source_session_id": session_id}


class PinRequest(BaseModel):
    pinned: bool


@router.post("/sessions/{session_id}/pin")
async def pin_session(request: Request, session_id: str, body: PinRequest):
    """Pin or unpin a session to the main dashboard.

    Pinned idle sessions stay visible on `/` instead of only
    showing up on `/idle`. Stopped sessions cannot be pinned —
    a dead tmux means Claude is gone and the pin would be meaningless.
    Going stopped also auto-unpins (handled in update_session_status).
    """
    conn = request.app.state.db
    session = await db.get_session(conn, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if session["status"] == "stopped":
        raise HTTPException(
            status_code=400,
            detail="Cannot pin a stopped session",
        )
    await db.set_session_pinned(conn, session_id, body.pinned)
    return {"ok": True, "pinned": body.pinned}


class TitleRequest(BaseModel):
    title: str | None = None


@router.post("/sessions/{session_id}/title")
async def set_title(request: Request, session_id: str, body: TitleRequest):
    """Rename how a session is shown in the Hub.

    This is a display name only. The tmux session keeps its name, so
    attach / approve / restore keep working unchanged.
    """
    conn = request.app.state.db
    session = await db.get_session(conn, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    title = (body.title or "").strip()
    if len(title) > 80:
        raise HTTPException(status_code=400, detail="Title is limited to 80 characters")
    await db.set_session_title(conn, session_id, title or None)
    # The next conversation in this tmux takes the name over.
    if session.get("tmux_session") and not session.get("parent_session_id"):
        await tmux_input.set_tmux_title(session["tmux_session"], title or None)
    return {"ok": True, "title": title or None}


class NotifyRequest(BaseModel):
    message: str
    title: str | None = None


@router.post("/notify")
async def send_notification(body: NotifyRequest):
    """Send a free-form message to the phone, for scripts that want to
    say they are done. It goes out on GET /api/stream as a notice, and a
    client that connects within the hour still gets it."""
    message = body.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="Message is empty")
    return {"ok": True, "notice": attention.notice(message[:4000], (body.title or "").strip()[:200] or None)}
