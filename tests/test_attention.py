"""Session states for the phone stream (services/attention.py).

A session needs the user when its last turn ended after they last looked
and no new turn has started, or when it waits for an approval. The
stream sends a state only when it changes, drops sessions that end, and
treats input at the session's terminal after the turn ended as looking.
"""
import asyncio
from datetime import datetime, timedelta, timezone

from agent_hub import db
from agent_hub.services import attention as attention_mod
from agent_hub.services import event_processor
from agent_hub.services.attention import Attention, session_state


def _iso(seconds_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()


def _row(**kw) -> dict:
    row = {
        "session_id": "11111111-2222-3333-4444-555555555555",
        "status": "idle",
        "tool": "claude",
        "cwd": "/w",
        "tmux_session": "claude-w-1",
        "title": None,
        "pending_tool": None,
        "pending_detail": None,
        "pending_always_label": None,
        "turn_ended_at": None,
        "last_message": None,
        "seen_at": None,
    }
    row.update(kw)
    return row


def test_finished_turn_is_unseen_until_looked_at():
    state = session_state(_row(turn_ended_at=_iso(5)))
    assert state["unseen"] and state["needs_you"]
    state = session_state(_row(turn_ended_at=_iso(5), seen_at=_iso(1)))
    assert not state["unseen"] and not state["needs_you"]
    # Looked at before the turn ended: the new reply is still unseen.
    state = session_state(_row(turn_ended_at=_iso(5), seen_at=_iso(60)))
    assert state["unseen"]


def test_a_running_turn_is_not_unseen():
    state = session_state(_row(status="active", turn_ended_at=_iso(5)))
    assert not state["unseen"] and not state["needs_you"]


def test_waiting_approval_needs_you():
    state = session_state(_row(
        status="active", pending_tool="Bash", pending_detail="rm -rf build",
        pending_always_label="rm commands",
    ))
    assert state["needs_you"] and not state["unseen"]
    assert state["pending"] == {"tool": "Bash", "detail": "rm -rf build", "always_label": "rm commands"}


def test_name_and_preview():
    assert session_state(_row(title="paper"))["name"] == "paper"
    assert session_state(_row())["name"] == "claude-w-1"
    assert session_state(_row(tmux_session=None))["name"] == "11111111"
    long = "x" * 2000
    preview = session_state(_row(last_message=long))["last_message"]
    assert len(preview) == attention_mod.PREVIEW_CHARS and preview.endswith("…")


def _evt(sid, kind, **extra):
    p = {"session_id": sid, "hook_event_name": kind, "cwd": "/w"}
    p.update(extra)
    return p


async def _send(conn, payload):
    await event_processor.process_event(conn, payload, hub_id="hub-a", hostname="h")


def test_stop_hook_records_the_turn_end_and_reply():
    async def run():
        conn = await db.init_db(":memory:")
        try:
            await check(conn)
        finally:
            await conn.close()

    async def check(conn):
        sid = "aaaaaaaa-0000-0000-0000-000000000001"
        await _send(conn, _evt(sid, "SessionStart", _tmux_session="claude-w-1"))
        await _send(conn, _evt(sid, "UserPromptSubmit", prompt="hi"))
        await _send(conn, _evt(sid, "Stop", last_assistant_message="  Done. Tests pass.  "))
        row = await db.get_session(conn, sid)
        assert row["status"] == "idle"
        assert row["last_message"] == "Done. Tests pass."
        assert session_state(row)["unseen"]

        await db.mark_session_seen(conn, sid)
        assert not session_state(await db.get_session(conn, sid))["unseen"]

        # The next prompt starts a turn; its Stop is unseen again.
        await _send(conn, _evt(sid, "UserPromptSubmit", prompt="more"))
        assert not session_state(await db.get_session(conn, sid))["unseen"]
        await asyncio.sleep(0.01)
        await _send(conn, _evt(sid, "Stop"))
        row = await db.get_session(conn, sid)
        assert session_state(row)["unseen"]
        assert row["last_message"] is None, "a Stop without a reply must not keep the old one"

    asyncio.run(run())


async def _mk(conn, sid, status="idle", tmux="t1", parent=None, ended_ago=None):
    await db.upsert_session(
        conn, session_id=sid, hub_id="h", hostname="h", cwd="/w",
        status=status, tmux_session=tmux,
    )
    if parent:
        await db.set_session_parent(conn, sid, parent)
    if ended_ago is not None:
        await conn.execute(
            "UPDATE sessions SET turn_ended_at = ? WHERE session_id = ?",
            (_iso(ended_ago), sid),
        )
        await conn.commit()


def _drain(sub) -> list:
    out = []
    while not sub.queue.empty():
        out.append(sub.queue.get_nowait())
    return out


def test_stream_sends_changes_only():
    async def run():
        conn = await db.init_db(":memory:")
        await _mk(conn, "s1", ended_ago=30)
        await _mk(conn, "s2", status="active", tmux="t2")
        await _mk(conn, "child", tmux="t1", parent="s1", ended_ago=5)
        await _mk(conn, "gone", status="stopped", tmux="t3")
        hub = Attention()
        await hub.start(conn)
        try:
            sub, snap = await hub.subscribe()
            ids = sorted(s["session_id"] for s in snap["sessions"])
            assert ids == ["s1", "s2"], "sub-agents and stopped sessions are left out"
            assert next(s for s in snap["sessions"] if s["session_id"] == "s1")["unseen"]

            await hub.refresh()
            assert _drain(sub) == [], "nothing changed, nothing sent"

            await db.mark_session_seen(conn, "s1")
            await hub.refresh()
            events = _drain(sub)
            assert [(k, d["session_id"], d["unseen"]) for k, d in events] == [("session", "s1", False)]

            # A hook event moves last_seen_at only: no update.
            await db.update_session_activity(conn, "s2")
            await hub.refresh()
            assert _drain(sub) == []

            await db.update_session_status(conn, "s2", "stopped")
            await hub.refresh()
            assert _drain(sub) == [("removed", {"session_id": "s2"})]
        finally:
            await hub.stop()
            await conn.close()

    asyncio.run(run())


def test_broadcasts_trigger_a_refresh():
    async def run():
        from agent_hub.api.ws import broadcaster

        conn = await db.init_db(":memory:")
        await _mk(conn, "s1", ended_ago=30)
        hub = Attention()
        await hub.start(conn)
        try:
            sub, _ = await hub.subscribe()
            await db.mark_session_seen(conn, "s1")
            await broadcaster.broadcast({"type": "event", "session_id": "s1"})
            kind, data = await asyncio.wait_for(sub.queue.get(), timeout=3)
            assert kind == "session" and not data["unseen"]
        finally:
            await hub.stop()
            await conn.close()

    asyncio.run(run())


def test_terminal_input_after_the_turn_marks_it_seen(monkeypatch):
    async def run():
        conn = await db.init_db(":memory:")
        await _mk(conn, "s1", tmux="t1", ended_ago=60)
        await _mk(conn, "s2", tmux="t2", ended_ago=60)
        await _mk(conn, "s3", tmux="t3", ended_ago=60)
        now = int(datetime.now(timezone.utc).timestamp())
        clients = [
            ("t1", now - 600, now - 10),   # typed 10 s ago, after the turn ended
            ("t2", now - 600, now - 120),  # last input before the turn ended
            ("t3", now - 5, now - 5),      # just attached, no input since
        ]

        async def fake_clients():
            return clients

        monkeypatch.setattr(attention_mod, "_tmux_clients", fake_clients)
        hub = Attention()
        await hub.start(conn)
        try:
            assert await hub.mark_seen_by_terminal_input()
            await hub.refresh()
            unseen = {s["session_id"]: s["unseen"] for s in hub.snapshot()["sessions"]}
            assert unseen == {"s1": False, "s2": True, "s3": True}
        finally:
            await hub.stop()
            await conn.close()

    asyncio.run(run())


def test_notices_are_published_and_replayed():
    async def run():
        conn = await db.init_db(":memory:")
        hub = Attention()
        await hub.start(conn)
        try:
            sub, _ = await hub.subscribe()
            first = hub.notice("GPU run finished")
            second = hub.notice("another", title="train.py")
            assert second["id"] > first["id"]
            assert first["title"] == "Agent Hub" and second["title"] == "train.py"
            assert [k for k, _ in _drain(sub)] == ["notice", "notice"]
            _, snap = await hub.subscribe()
            assert [n["message"] for n in snap["notices"]] == ["GPU run finished", "another"]
        finally:
            await hub.stop()
            await conn.close()

    asyncio.run(run())


def test_back_to_back_hooks_keep_their_order():
    """Each hook is processed in its own task. A Stop that overtakes the
    prompt before it would leave the finished session marked active,
    and the phone would never hear about it."""
    async def run():
        from agent_hub.api import events as events_api

        conn = await db.init_db(":memory:")
        try:
            await check(conn, events_api)
        finally:
            await conn.close()

    async def check(conn, events_api):
        sid = "bbbbbbbb-0000-0000-0000-000000000002"
        tasks = [
            asyncio.create_task(events_api._process_safe(conn, p, "hub-a", "h"))
            for p in (
                _evt(sid, "SessionStart", _tmux_session="claude-w-2"),
                _evt(sid, "UserPromptSubmit", prompt="hi"),
                _evt(sid, "PreToolUse", tool_name="Bash", tool_input={"command": "ls"}),
                _evt(sid, "PostToolUse", tool_name="Bash", tool_input={"command": "ls"}),
                _evt(sid, "Stop", last_assistant_message="done"),
            )
        ]
        await asyncio.gather(*tasks)
        row = await db.get_session(conn, sid)
        assert row["status"] == "idle"
        assert session_state(row)["unseen"]
        assert not events_api._session_locks, "per-session locks are released"

    asyncio.run(run())
