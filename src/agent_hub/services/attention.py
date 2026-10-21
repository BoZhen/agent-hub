"""What a phone needs to know about each session, sent as it changes.

The dashboard's /ws stream carries every hook event. A phone listening in
the background only needs to know when a session needs the user: its turn
ended and nobody has looked at it since, or it waits for an approval.

`Attention` keeps one small state object per live tmux session. It
recomputes them from the database whenever the Hub broadcasts anything,
and every few seconds for changes that are not broadcast (sweeps, renames,
deletes), and sends only the states that changed to the subscribers of
`GET /api/stream`.

A finished turn counts as seen when someone opens the session (`POST
/api/sessions/{id}/seen`), or when a terminal attached to its tmux receives
input after the turn ended: then the user is at that terminal and the phone
should stay quiet. A user who walks away gives no input, so the turn stays
unseen and the phone is told.
"""
from __future__ import annotations

import asyncio
import collections
import logging
import time
from datetime import datetime, timezone

import aiosqlite

from agent_hub import db
from agent_hub.api.ws import broadcaster

logger = logging.getLogger(__name__)

# Recompute at least this often, for changes nobody broadcasts.
TICK_S = 10.0
# A broadcast usually comes in a burst (event, then pending, then stats);
# wait this long so one recompute covers the burst.
DEBOUNCE_S = 0.25
# A terminal that has just attached sends input of its own (xterm.js
# reports focus), so input this close to the attach does not count.
ATTACH_GRACE_S = 3
# Notices younger than this are replayed to a subscriber that connects.
NOTICE_REPLAY_S = 3600
NOTICE_KEEP = 20
PREVIEW_CHARS = 600
_QUEUE_MAX = 500


def _epoch(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return None


def _preview(text: str | None) -> str | None:
    if not text:
        return None
    text = text.strip()
    if len(text) <= PREVIEW_CHARS:
        return text
    return text[: PREVIEW_CHARS - 1] + "…"


def session_state(row: dict) -> dict:
    """The part of a session row a phone needs.

    `unseen` means the last turn ended after the user last looked and no
    new turn has started. `needs_you` adds sessions waiting for approval.
    Fields that change on every hook event (last_seen_at, token counts)
    are left out, so a busy session produces no updates.
    """
    pending = None
    if row.get("pending_tool"):
        pending = {
            "tool": row["pending_tool"],
            "detail": row.get("pending_detail"),
            "always_label": row.get("pending_always_label"),
        }
    ended = _epoch(row.get("turn_ended_at"))
    seen = _epoch(row.get("seen_at"))
    unseen = bool(
        row.get("status") == "idle"
        and ended is not None
        and (seen is None or seen < ended)
    )
    return {
        "session_id": row["session_id"],
        "name": row.get("title") or row.get("tmux_session") or row["session_id"][:8],
        "tool": row.get("tool") or "claude",
        "cwd": row.get("cwd") or "",
        "tmux_session": row.get("tmux_session"),
        "status": row.get("status"),
        "pending": pending,
        "turn_ended_at": row.get("turn_ended_at"),
        "last_message": _preview(row.get("last_message")),
        "unseen": unseen,
        "needs_you": pending is not None or unseen,
    }


async def _tmux_clients() -> list[tuple[str, int, int]]:
    """(session name, attach time, last input time) of every attached
    terminal, in epoch seconds. Empty when no tmux server runs."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "tmux", "list-clients", "-F",
            "#{client_session}\t#{client_created}\t#{client_activity}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
    except OSError:
        return []
    if proc.returncode != 0:
        return []
    clients = []
    for line in out.decode("utf-8", errors="replace").splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        try:
            clients.append((parts[0], int(parts[1]), int(parts[2])))
        except ValueError:
            continue
    return clients


class _Subscriber:
    def __init__(self) -> None:
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAX)


class Attention:
    def __init__(self) -> None:
        self._conn: aiosqlite.Connection | None = None
        self._states: dict[str, dict] = {}
        self._subscribers: set[_Subscriber] = set()
        self._notices: collections.deque = collections.deque(maxlen=NOTICE_KEEP)
        self._last_notice_id = 0
        self._dirty: asyncio.Event | None = None
        self._lock: asyncio.Lock | None = None
        self._task: asyncio.Task | None = None

    # ── Lifecycle ─────────────────────────────────────────────

    async def start(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn
        self._dirty = asyncio.Event()
        self._lock = asyncio.Lock()
        self._states = await self._compute()
        broadcaster.add_listener(self._on_broadcast)
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        broadcaster.remove_listener(self._on_broadcast)
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        for sub in list(self._subscribers):
            self._close(sub)

    def _on_broadcast(self, message: dict) -> None:
        self.poke()

    def poke(self) -> None:
        """Something may have changed; recompute soon."""
        if self._dirty is not None:
            self._dirty.set()

    async def _run(self) -> None:
        assert self._dirty is not None
        while True:
            try:
                await asyncio.wait_for(self._dirty.wait(), timeout=TICK_S)
                await asyncio.sleep(DEBOUNCE_S)
            except asyncio.TimeoutError:
                pass
            self._dirty.clear()
            try:
                await self.mark_seen_by_terminal_input()
                await self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Attention refresh failed")

    # ── State ─────────────────────────────────────────────────

    async def _compute(self) -> dict[str, dict]:
        assert self._conn is not None
        rows = await db.get_attention_rows(self._conn)
        return {r["session_id"]: session_state(r) for r in rows}

    async def refresh(self) -> None:
        """Recompute every state and send the ones that changed."""
        assert self._lock is not None
        async with self._lock:
            new = await self._compute()
            old = self._states
            self._states = new
            for sid, state in new.items():
                if old.get(sid) != state:
                    self._publish("session", state)
            for sid in old.keys() - new.keys():
                self._publish("removed", {"session_id": sid})

    async def mark_seen_by_terminal_input(self) -> bool:
        """Mark unseen turns seen when their tmux got input after the turn
        ended. Returns whether any changed."""
        assert self._conn is not None
        waiting = [s for s in self._states.values() if s["unseen"] and s["tmux_session"]]
        if not waiting:
            return False
        clients = await _tmux_clients()
        changed = False
        for state in waiting:
            ended = _epoch(state["turn_ended_at"])
            if ended is None:
                continue
            for name, created, activity in clients:
                if (
                    name == state["tmux_session"]
                    and activity > ended
                    and activity >= created + ATTACH_GRACE_S
                ):
                    await db.mark_session_seen(self._conn, state["session_id"])
                    changed = True
                    break
        return changed

    def snapshot(self) -> dict:
        cutoff = time.time() - NOTICE_REPLAY_S
        return {
            "sessions": list(self._states.values()),
            "notices": [n for n in self._notices if n["id"] / 1000 >= cutoff],
            "server_time": datetime.now(timezone.utc).isoformat(),
        }

    # ── Subscribers ───────────────────────────────────────────

    async def subscribe(self) -> tuple[_Subscriber, dict]:
        """Register a subscriber and return it with a fresh snapshot.
        Nothing awaits between the snapshot and the registration, so the
        subscriber misses no change."""
        await self.refresh()
        sub = _Subscriber()
        self._subscribers.add(sub)
        return sub, self.snapshot()

    def unsubscribe(self, sub: _Subscriber) -> None:
        self._subscribers.discard(sub)

    def _close(self, sub: _Subscriber) -> None:
        """Drop a subscriber; its stream ends and the client reconnects
        for a fresh snapshot."""
        self._subscribers.discard(sub)
        while not sub.queue.empty():
            sub.queue.get_nowait()
        sub.queue.put_nowait((None, None))

    def _publish(self, kind: str, data: dict) -> None:
        for sub in list(self._subscribers):
            try:
                sub.queue.put_nowait((kind, data))
            except asyncio.QueueFull:
                logger.warning("Dropping a stream subscriber that stopped reading")
                self._close(sub)

    # ── Notices ───────────────────────────────────────────────

    def notice(self, message: str, title: str | None = None) -> dict:
        """A free-form message for the phone, from POST /api/notify.
        Ids are millisecond timestamps, so they keep increasing across
        Hub restarts and a client can skip ones it has shown."""
        item = {
            "id": max(int(time.time() * 1000), self._last_notice_id + 1),
            "ts": datetime.now(timezone.utc).isoformat(),
            "title": title or "Agent Hub",
            "message": message,
        }
        self._last_notice_id = item["id"]
        self._notices.append(item)
        self._publish("notice", item)
        return item


attention = Attention()
