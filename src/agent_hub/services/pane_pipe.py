"""Push-based pane observability for the Hub.

Replaces the 3-second `tmux capture-pane` poll loop with `tmux pipe-pane`
streaming pane bytes into a regular file watched via inotify (the
`watchfiles` Rust library). When a tmux pane has activity, an inotify
event arrives within ~1ms; we throttle with a leading-edge + trailing
strategy so the parser fires immediately on the first event (≈0 ms
detection latency) and at most once per THROTTLE_SECONDS afterwards.

The pipe carries raw ANSI byte streams — we deliberately do NOT parse
them directly. The pipe is a wake-up signal; actual parsing still
captures the rendered pane via `tmux capture-pane` and runs the existing
`_parse_approval_prompt` / `_parse_codex_approval_prompt` functions, so
existing detection logic is unchanged.
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from watchfiles import awatch

logger = logging.getLogger(__name__)


# Leading-edge throttle: the first event in a burst fires the parser
# immediately (zero latency for the dashboard), then subsequent events
# are folded into a single trailing fire scheduled THROTTLE_SECONDS
# after the leading one. Keeps approval detection snappy while bounding
# CPU on panes that stream output continuously.
THROTTLE_SECONDS = 0.5

# Maximum bytes a pipe file can grow to before we truncate it. Picked
# at ~64 KB so most active panes never hit it (a typical Claude/Codex
# screen is ~5 KB rendered); only output-heavy commands like `cat
# bigfile` will. Truncation triggers an inotify echo we filter via a
# brief cool-off below.
TRUNCATE_THRESHOLD_BYTES = 64 * 1024
TRUNCATE_COOLOFF_SECONDS = 0.2


def _runtime_dir() -> Path:
    """Per-user tmpfs dir for transient pipe files. Cleared on reboot,
    no disk pollution. Falls back to /tmp on systems without
    XDG_RUNTIME_DIR (rare; only non-systemd-logind machines)."""
    base = os.environ.get("XDG_RUNTIME_DIR")
    if not base:
        base = f"/run/user/{os.getuid()}"
    return Path(base) / "agent-hub" / "pipes"


def _safe(name: str) -> str:
    # tmux disallows `:` and `.` in session names already, so the only
    # filesystem-hostile char we still need to handle is `/`. Keeping
    # the mapping reversible (one-to-one) means we can derive a
    # tmux_name back from a pipe filename for orphan cleanup.
    return name.replace("/", "%2F")


def _unsafe(stem: str) -> str:
    return stem.replace("%2F", "/")


async def _list_pane_ids(tmux_name: str) -> list[str]:
    """Return every `%<id>` pane in this tmux session.

    Sessions with split windows have multiple panes; piping only the
    active pane (via `-t name:`) misses codex/claude running in a
    split. Returns an empty list if the session is gone or listing
    failed.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "tmux", "list-panes", "-t", tmux_name, "-s",
            "-F", "#{pane_id}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
    except Exception:
        logger.exception("tmux list-panes failed for %s", tmux_name)
        return []
    if proc.returncode != 0:
        return []
    return [p for p in out.decode().split() if p.startswith("%")]


@dataclass
class PipeContext:
    tmux_name: str
    path: Path
    # If we already scheduled a trailing fire after the throttle window
    # ends, this is its handle. New events while it's pending fold into
    # it (no extra schedule).
    trailing_handle: asyncio.TimerHandle | None = None
    # asyncio loop.time() of the last fire — leading edge takes effect
    # only when this many seconds have elapsed since the last fire.
    last_fire_ts: float = 0.0
    # Inotify echoes from our own truncate calls land here briefly so
    # we don't busy-loop on the file we just emptied.
    cool_off_until: float = 0.0
    holders: set[str] = field(default_factory=set)


# Callback fires per throttle window with the tmux session name.
# It's the caller's job to find the owning session and run parsing.
ParseCallback = Callable[[str], Awaitable[None]]


class PanePipeManager:
    """Owns the tmux pipe-pane lifecycle for a Hub instance.

    Sessions register interest by calling `attach(sid, tmux_name)`.
    Multiple sids may share a tmux (parent + subagent); the manager
    refcounts sids per tmux and only tears down the pipe when the last
    holder leaves.

    Activity on any pipe file fires `parse_callback(tmux_name)` with a
    leading-edge + trailing throttle, so the first event in a burst
    parses immediately and subsequent events fold into one trailing
    parse at the end of the THROTTLE_SECONDS window.
    """

    def __init__(self, parse_callback: ParseCallback | None = None) -> None:
        self.dir = _runtime_dir()
        self.parse_callback = parse_callback
        self._pipes: dict[str, PipeContext] = {}
        self._sid_to_tmux: dict[str, str] = {}
        self._watch_task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

    # ── Lifecycle ───────────────────────────────────────────────

    async def start(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self._stop_event.clear()
        self._watch_task = asyncio.create_task(
            self._watch_loop(), name="pane-pipe-watch",
        )
        logger.info("PanePipeManager started, watching %s", self.dir)

    async def stop(self) -> None:
        self._stop_event.set()
        if self._watch_task is not None:
            self._watch_task.cancel()
            try:
                await self._watch_task
            except asyncio.CancelledError:
                pass
            self._watch_task = None
        for tmux_name in list(self._pipes.keys()):
            await self._close_pipe(tmux_name)
        self._sid_to_tmux.clear()
        logger.info("PanePipeManager stopped")

    # ── Session registration ────────────────────────────────────

    async def attach(self, sid: str, tmux_name: str) -> bool:
        """Register `sid` as wanting the pipe for `tmux_name`. Idempotent.

        Returns True on success, False if pipe-pane setup failed (caller
        should rely on the polling fallback for this session).
        """
        old = self._sid_to_tmux.get(sid)
        if old and old != tmux_name:
            await self.detach(sid)

        ctx = self._pipes.get(tmux_name)
        if ctx is None:
            ok = await self._open_pipe(tmux_name)
            if not ok:
                return False
            ctx = self._pipes[tmux_name]

        ctx.holders.add(sid)
        self._sid_to_tmux[sid] = tmux_name
        return True

    async def detach(self, sid: str) -> None:
        """Release sid's hold; tear down the pipe iff no holder remains."""
        tmux_name = self._sid_to_tmux.pop(sid, None)
        if tmux_name is None:
            return
        ctx = self._pipes.get(tmux_name)
        if ctx is None:
            return
        ctx.holders.discard(sid)
        if not ctx.holders:
            await self._close_pipe(tmux_name)

    def is_attached(self, sid: str) -> bool:
        return sid in self._sid_to_tmux

    async def cleanup_orphan_pipes(self, keep_tmux_names: set[str]) -> int:
        """Remove pipe files for tmuxes that aren't in the keep set.

        Called at startup to clear pipe files left over from a prior
        hub instance. Also issues `tmux pipe-pane -t name:` (off) for
        each orphan so any zombie pipe-pane shell from the old hub
        stops writing to a now-stale path.
        """
        if not self.dir.exists():
            return 0
        cleaned = 0
        for path in self.dir.iterdir():
            if not path.is_file() or path.suffix != ".log":
                continue
            tmux_name = _unsafe(path.stem)
            if tmux_name in keep_tmux_names:
                continue
            # Disable pipe-pane on every pane — the orphan may have
            # been a multi-pane session piped from a prior hub run.
            for pid in await _list_pane_ids(tmux_name):
                try:
                    proc = await asyncio.create_subprocess_exec(
                        "tmux", "pipe-pane", "-t", pid,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    await proc.communicate()
                except Exception:
                    pass
            try:
                path.unlink(missing_ok=True)
                cleaned += 1
            except OSError:
                pass
        if cleaned:
            logger.info("cleanup_orphan_pipes: removed %d stale file(s)", cleaned)
        return cleaned

    # ── Internal: pipe lifecycle ────────────────────────────────

    async def _open_pipe(self, tmux_name: str) -> bool:
        path = self.dir / f"{_safe(tmux_name)}.log"
        try:
            path.write_bytes(b"")  # truncate / create
        except OSError:
            logger.exception("Cannot prepare pipe file %s", path)
            return False

        pane_ids = await _list_pane_ids(tmux_name)
        if not pane_ids:
            logger.warning("No panes found for tmux=%s; skipping pipe", tmux_name)
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            return False

        # tmux pipe-pane -o: only-open. If a pipe is already attached
        # (e.g. left over from a prior hub instance writing to the same
        # path), this is a no-op — desirable, since data continues to
        # flow into the file we just truncated. We enable a pipe on
        # every pane so codex/claude running in a non-active split is
        # still tracked; all writers append to one log via `cat >>`.
        any_ok = False
        for pid in pane_ids:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "tmux", "pipe-pane", "-o",
                    "-t", pid,
                    f"cat >> {path!s}",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, err = await proc.communicate()
            except Exception:
                logger.exception("tmux pipe-pane invocation failed for %s pane %s",
                                 tmux_name, pid)
                continue
            if proc.returncode != 0:
                logger.warning(
                    "tmux pipe-pane returned %d for %s pane %s: %s",
                    proc.returncode, tmux_name, pid,
                    err.decode(errors="replace").strip() if err else "",
                )
                continue
            any_ok = True

        if not any_ok:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            return False

        self._pipes[tmux_name] = PipeContext(tmux_name=tmux_name, path=path)
        logger.info("Opened pipe for tmux=%s (%d pane(s)) → %s",
                    tmux_name, len(pane_ids), path)
        return True

    async def _close_pipe(self, tmux_name: str) -> None:
        ctx = self._pipes.pop(tmux_name, None)
        if ctx is None:
            return
        if ctx.trailing_handle is not None and not ctx.trailing_handle.cancelled():
            ctx.trailing_handle.cancel()
        # Toggle pipe off on every pane — best-effort (tmux may already
        # be gone, or panes may have been closed since open).
        for pid in await _list_pane_ids(tmux_name):
            try:
                proc = await asyncio.create_subprocess_exec(
                    "tmux", "pipe-pane", "-t", pid,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await proc.communicate()
            except Exception:
                pass
        try:
            ctx.path.unlink(missing_ok=True)
        except OSError:
            pass
        logger.info("Closed pipe for tmux=%s", tmux_name)

    # ── Internal: watch loop and debounce ───────────────────────

    async def _watch_loop(self) -> None:
        try:
            async for changes in awatch(
                self.dir,
                stop_event=self._stop_event,
                yield_on_timeout=False,
                recursive=False,
            ):
                loop = asyncio.get_running_loop()
                now = loop.time()
                # One yielded batch may contain multiple events for the
                # same path — only schedule once per ctx per batch.
                seen: set[str] = set()
                for _, path_str in changes:
                    if path_str in seen:
                        continue
                    seen.add(path_str)
                    ctx = self._lookup_by_path(Path(path_str))
                    if ctx is None:
                        continue
                    # Drop inotify echoes from our own truncate calls.
                    # Real activity arriving in this 200 ms window is
                    # also dropped, but the next byte from the pane
                    # will re-trigger us — losses are bounded.
                    if now < ctx.cool_off_until:
                        continue
                    self._schedule(ctx, now)
        except asyncio.CancelledError:
            raise
        except Exception:
            # If watchfiles itself bails (e.g. inotify exhausted), the
            # 60s polling fallback in session_manager keeps detection
            # alive. We log and exit so we don't busy-loop.
            logger.exception("PanePipeManager watch loop crashed; "
                             "falling back to polling-only mode")

    def _lookup_by_path(self, path: Path) -> PipeContext | None:
        for ctx in self._pipes.values():
            if ctx.path == path:
                return ctx
        return None

    def _schedule(self, ctx: PipeContext, now: float) -> None:
        """Leading-edge throttle: fire immediately if cooled, else
        coalesce into one trailing fire at the end of the window."""
        # Trailing fire already scheduled — this event folds into it.
        if ctx.trailing_handle is not None and not ctx.trailing_handle.cancelled():
            return

        elapsed = now - ctx.last_fire_ts
        if elapsed >= THROTTLE_SECONDS:
            # Cooled off — fire immediately. ~0 ms detection latency
            # for the leading event in a burst.
            ctx.last_fire_ts = now
            asyncio.create_task(self._fire(ctx))
            return

        # In the throttle window — schedule the trailing fire so any
        # state change late in the burst (e.g. an approval prompt
        # appearing right after a long Bash stream finishes) is still
        # caught.
        loop = asyncio.get_running_loop()
        delay = THROTTLE_SECONDS - elapsed
        ctx.trailing_handle = loop.call_later(
            delay,
            lambda c=ctx: asyncio.create_task(self._fire(c, trailing=True)),
        )

    async def _fire(self, ctx: PipeContext, trailing: bool = False) -> None:
        if trailing:
            ctx.trailing_handle = None
            ctx.last_fire_ts = asyncio.get_running_loop().time()

        # Bound file size — only truncate when it has grown past the
        # threshold to avoid generating an inotify echo for every fire.
        # Heavy panes get one truncate per ~5 minutes of streaming;
        # idle / lightly-active panes never truncate.
        try:
            size = ctx.path.stat().st_size
        except OSError:
            size = 0
        if size > TRUNCATE_THRESHOLD_BYTES:
            try:
                with open(ctx.path, "r+b") as f:
                    f.truncate(0)
                ctx.cool_off_until = (
                    asyncio.get_running_loop().time() + TRUNCATE_COOLOFF_SECONDS
                )
            except OSError:
                pass

        if self.parse_callback is None:
            logger.info("pipe activity (no callback): tmux=%s", ctx.tmux_name)
            return
        try:
            await self.parse_callback(ctx.tmux_name)
        except Exception:
            logger.exception(
                "parse_callback raised for tmux=%s", ctx.tmux_name
            )


# Module-level singleton. session_manager calls these to attach/detach
# pipes from session lifecycle hooks without needing to thread a
# PanePipeManager reference through every call site.
_GLOBAL: PanePipeManager | None = None


def set_pipe_manager(mgr: PanePipeManager | None) -> None:
    global _GLOBAL
    _GLOBAL = mgr


def get_pipe_manager() -> PanePipeManager | None:
    return _GLOBAL
