"""Tests for the tmux gate in ``event_processor.process_event``.

Agent Hub is tmux-centric: it can only attach to, approve, or sweep a
session that lives in a tmux pane. Tools like **claude-console** /
**codex-console** drive ``claude`` / ``codex`` as plain subprocesses
*outside* tmux, yet the global hooks still fire, so their events reach
``/api/events`` with no tmux name. Such rows can never be reaped by the
dead-tmux sweep (it only inspects ``tmux_session IS NOT NULL``) and so
linger forever as "idle" zombies. The gate drops them at the door.

These tests pin the gate's contract:
  * tmux-less *new* sessions create no row and store no event;
  * tmux-backed sessions are tracked as before;
  * tmux-less tool events of an *already tracked* session still flow
    (only SessionStart carries the tmux name — every other hook is
    tmux-less by design);
  * hub-forked sessions, whose first event is tmux-less but back-filled
    from the pending-fork table, are still tracked.

Run with:
    uv run python tests/test_event_processor_tmux_gate.py
"""
from __future__ import annotations

import asyncio
import sys

from agent_hub import db
from agent_hub.services import event_processor, session_manager


async def _count(conn, table: str) -> int:
    cur = await conn.execute(f"SELECT COUNT(*) AS n FROM {table}")
    return (await cur.fetchone())["n"]


def _evt(session_id: str, event_type: str, **extra) -> dict:
    p = {"session_id": session_id, "hook_event_name": event_type}
    p.update(extra)
    return p


async def _send(conn, payload) -> None:
    await event_processor.process_event(
        conn, payload, hub_id="hub-a", hostname="h"
    )


async def run() -> None:
    conn = await db.init_db(":memory:")

    # 1. SessionStart with no tmux → dropped: no row, no event.
    await _send(conn, _evt("console-1", "SessionStart",
                           cwd="/home/u/Git/claude-console"))
    assert await _count(conn, "sessions") == 0, \
        "tmux-less SessionStart must not create a session row"
    assert await _count(conn, "events") == 0, \
        "tmux-less SessionStart must not store an event"

    # 2. A follow-up tmux-less tool event for the same console sid is
    #    likewise dropped (the row never came into existence).
    await _send(conn, _evt("console-1", "PreToolUse", tool_name="Bash",
                           cwd="/home/u/Git/claude-console",
                           tool_input={"command": "ls"}))
    assert await _count(conn, "sessions") == 0, \
        "tmux-less tool event must not create a session row"
    assert await _count(conn, "events") == 0, \
        "tmux-less tool event must not store an event"
    print("ok   tmux-less console session is ignored (no row, no events)")

    # 3. SessionStart WITH tmux → tracked normally.
    start = _evt("real-1", "SessionStart", cwd="/home/u/proj")
    start["_tmux_session"] = "claude-proj"
    await _send(conn, start)
    row = await db.get_session(conn, "real-1")
    assert row is not None, "tmux-backed SessionStart must create a row"
    assert row["tmux_session"] == "claude-proj"
    print("ok   tmux-backed session is tracked")

    # 4. A tmux-less tool event for the EXISTING tmux session must flow
    #    through (every non-SessionStart hook is tmux-less) and must not
    #    clobber the stored tmux name.
    before = await _count(conn, "events")
    await _send(conn, _evt("real-1", "PreToolUse", tool_name="Read",
                           cwd="/home/u/proj",
                           tool_input={"file_path": "x.py"}))
    assert await _count(conn, "events") == before + 1, \
        "tmux-less tool event of a tracked session must be stored"
    row = await db.get_session(conn, "real-1")
    assert row["tmux_session"] == "claude-proj", \
        "existing tmux name must be preserved"
    print("ok   tmux-less tool event of a tracked session still flows")

    # 5. Hub-forked session: first event is tmux-less, but a pending
    #    fork registered by cwd back-fills the tmux → tracked, not dropped.
    session_manager.register_pending_fork("claude-fork", "/home/u/forked")
    await _send(conn, _evt("fork-1", "UserPromptSubmit",
                           cwd="/home/u/forked", prompt="hi"))
    row = await db.get_session(conn, "fork-1")
    assert row is not None, \
        "hub-forked session must be tracked via pending-fork back-fill"
    assert row["tmux_session"] == "claude-fork"
    print("ok   hub-forked (back-filled) session is tracked")

    # 6. A legacy tmux-less row (a zombie created before the gate
    #    existed) must be inert: its later tmux-less events are dropped
    #    and never revive it back to "active".
    await db.upsert_session(
        conn, session_id="zombie-1", hub_id="hub-a", hostname="h",
        cwd="/home/u/Git/claude-console", status="idle", tmux_session=None,
    )
    before = await _count(conn, "events")
    await _send(conn, _evt("zombie-1", "PreToolUse", tool_name="Bash",
                           cwd="/home/u/Git/claude-console",
                           tool_input={"command": "ls"}))
    assert await _count(conn, "events") == before, \
        "events for a legacy tmux-less row must be dropped"
    row = await db.get_session(conn, "zombie-1")
    assert row["status"] == "idle", \
        "a legacy tmux-less row must not be revived to active"
    print("ok   legacy tmux-less row stays inert (no revival)")

    await conn.close()
    print("\nall event-processor tmux-gate tests passed")


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except AssertionError as exc:
        print("FAIL:", exc)
        sys.exit(1)
