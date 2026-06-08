"""Tests for the stopped-session retention sweep.

Stopped sessions are terminal in the Hub's model and otherwise pile up
forever (only a manual "clear stopped" removed them). `_sweep_old_stopped`
auto-prunes stopped rows — and their events — once they age past
`stopped_retention_days`, while leaving active/idle sessions and
recently-stopped ones untouched. `retention_days <= 0` disables it.

Run with:
    uv run python tests/test_stopped_retention.py
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone

from agent_hub import db
from agent_hub.services import session_manager


async def _count(conn, table: str) -> int:
    cur = await conn.execute(f"SELECT COUNT(*) AS n FROM {table}")
    return (await cur.fetchone())["n"]


async def _exists(conn, sid: str) -> bool:
    return (await db.get_session(conn, sid)) is not None


async def _mk(conn, sid, status, *, stopped_age=None, last_seen_age=0):
    """Create a session then force its timestamps to a controlled age
    (days into the past). stopped_age=None leaves stopped_at NULL to
    exercise the legacy COALESCE fallback."""
    await db.upsert_session(
        conn, session_id=sid, hub_id="h", hostname="h", cwd="/x",
        status=status, tmux_session=f"tmux-{sid}",
    )
    now = datetime.now(timezone.utc)
    if status == "stopped" and stopped_age is not None:
        await conn.execute(
            "UPDATE sessions SET stopped_at=? WHERE session_id=?",
            ((now - timedelta(days=stopped_age)).isoformat(), sid),
        )
    await conn.execute(
        "UPDATE sessions SET last_seen_at=? WHERE session_id=?",
        ((now - timedelta(days=last_seen_age)).isoformat(), sid),
    )
    await conn.commit()


async def run() -> None:
    conn = await db.init_db(":memory:")

    await _mk(conn, "old-stopped", "stopped", stopped_age=30)
    await _mk(conn, "recent-stopped", "stopped", stopped_age=1)
    # Legacy row: stopped_at NULL, only an old last_seen_at -> must
    # still age out via COALESCE(stopped_at, last_seen_at, started_at).
    await _mk(conn, "legacy-stopped", "stopped", stopped_age=None,
              last_seen_age=30)
    await _mk(conn, "active-old", "active", last_seen_age=30)
    await _mk(conn, "idle-old", "idle", last_seen_age=30)
    # An event attached to the old stopped session must be cascaded.
    await db.insert_event(
        conn, hub_id="h", session_id="old-stopped",
        event_type="Stop", tool_name=None, summary="x", payload="{}",
    )

    # Disabled (0) must delete nothing.
    assert await session_manager._sweep_old_stopped(conn, 0) == 0
    assert await _count(conn, "sessions") == 5
    print("ok   retention disabled (0) prunes nothing")

    # 14-day window: old-stopped + legacy-stopped go; the rest stay.
    deleted = await session_manager._sweep_old_stopped(conn, 14)
    assert deleted == 2, f"expected 2 pruned, got {deleted}"
    assert not await _exists(conn, "old-stopped")
    assert not await _exists(conn, "legacy-stopped")
    assert await _exists(conn, "recent-stopped"), "recent stopped must survive"
    assert await _exists(conn, "active-old"), "active is never pruned"
    assert await _exists(conn, "idle-old"), "idle is never pruned"
    print("ok   prunes only stopped rows older than the window")

    # Events of the pruned session are gone too.
    assert await _count(conn, "events") == 0, "pruned session's events must be gone"
    print("ok   pruned sessions' events are cascaded")

    # Idempotent: a second pass with nothing newly aged removes nothing.
    assert await session_manager._sweep_old_stopped(conn, 14) == 0
    print("ok   second pass is a no-op")

    await conn.close()
    print("\nall stopped-retention tests passed")


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except AssertionError as exc:
        print("FAIL:", exc)
        sys.exit(1)
