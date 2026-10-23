"""Codex hooks carry no tmux name: Codex runs a session in its app-server,
outside the session's terminal. A SessionStart is matched to the tmux pane
that runs codex in the same directory and printed just now.

Runs against a private tmux server (TMUX_TMPDIR in a temp directory, $TMUX
unset). The "codex" in a pane is a shell script of that name that echoes
what it reads, so a test can make one pane print and leave another quiet.
"""
import asyncio
import shutil
import subprocess
import time

import pytest

from agent_hub import db
from agent_hub.services import event_processor, session_manager

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")

UUID = "019a2222-aaaa-7aaa-8aaa-000000000001"
QUIET = 2.6  # long enough for a pane to fall out of the 2 s window


@pytest.fixture
def private_tmux(tmp_path, monkeypatch):
    tmux_dir = tmp_path / "tmux"
    tmux_dir.mkdir()
    monkeypatch.setenv("TMUX_TMPDIR", str(tmux_dir))
    monkeypatch.delenv("TMUX", raising=False)
    # Short window so a test can let a pane go quiet without waiting 15 s.
    # tmux keeps activity times in whole seconds, so it is 2 s, not less.
    monkeypatch.setattr(session_manager, "CODEX_MATCH_WINDOW_S", 2.0)
    yield
    subprocess.run(["tmux", "kill-server"], capture_output=True)


@pytest.fixture
def codex(tmp_path):
    script = tmp_path / "bin" / "codex"
    script.parent.mkdir()
    script.write_text("#!/bin/sh\nwhile read line; do echo \"codex: $line\"; done\n")
    script.chmod(0o755)
    return str(script)


def _pane(name, cwd, command):
    subprocess.run(["tmux", "new-session", "-d", "-s", name, "-c", str(cwd), command], check=True)


def _speak(name):
    """Make the pane print, as a TUI does when a prompt is submitted."""
    subprocess.run(["tmux", "send-keys", "-t", f"{name}:", "go", "Enter"], check=True)
    time.sleep(0.3)


def _start(sid, cwd, event="SessionStart"):
    return {"session_id": sid, "hook_event_name": event, "cwd": str(cwd), "_tool": "codex"}


def _run(check):
    async def run():
        conn = await db.init_db(":memory:")
        try:
            await check(conn)
        finally:
            await conn.close()
    asyncio.run(run())


def test_session_start_takes_the_pane_that_just_printed(tmp_path, private_tmux, codex):
    project = tmp_path / "project"
    project.mkdir()
    _pane("cx-a", project, codex)
    _pane("cx-b", project, codex)
    time.sleep(QUIET)  # both quiet now
    _speak("cx-a")

    async def check(conn):
        # The scan's placeholder for cx-a gives way to Codex's own id.
        await db.upsert_session(conn, session_id="codex-cx-a-1", hub_id="h", hostname="h",
                                cwd=str(project), status="idle", tmux_session="cx-a", tool="codex")
        await event_processor.process_event(conn, _start(UUID, project), "h", "h")
        row = await db.get_session(conn, UUID)
        assert row and row["tmux_session"] == "cx-a" and row["tool"] == "codex"
        assert (await db.get_session(conn, "codex-cx-a-1"))["status"] == "stopped"
        # Later events carry no tmux name either and still reach the row.
        await event_processor.process_event(conn, _start(UUID, project, "UserPromptSubmit"), "h", "h")
        assert (await db.get_session(conn, UUID))["status"] == "active"

    _run(check)


def test_no_match_without_a_single_fresh_pane(tmp_path, private_tmux, codex):
    project = tmp_path / "project"
    elsewhere = tmp_path / "elsewhere"
    project.mkdir()
    elsewhere.mkdir()
    _pane("cx-a", project, codex)
    _pane("cx-b", project, codex)
    time.sleep(QUIET)

    async def check(conn):
        # Both quiet: a codex in another terminal, not one of these panes.
        await event_processor.process_event(conn, _start("s-quiet", project), "h", "h")
        assert await db.get_session(conn, "s-quiet") is None
        # Both printed: cannot tell which one.
        _speak("cx-a")
        _speak("cx-b")
        await event_processor.process_event(conn, _start("s-both", project), "h", "h")
        assert await db.get_session(conn, "s-both") is None
        # Another directory.
        await event_processor.process_event(conn, _start("s-other", elsewhere), "h", "h")
        assert await db.get_session(conn, "s-other") is None
        # Only SessionStart is matched; a stray tool event stays out.
        _speak("cx-a")
        await event_processor.process_event(conn, _start("s-tool", project, "PreToolUse"), "h", "h")
        assert await db.get_session(conn, "s-tool") is None

    _run(check)


def test_a_pane_in_the_middle_of_a_turn_is_not_taken(tmp_path, private_tmux, codex):
    project = tmp_path / "project"
    project.mkdir()
    _pane("cx-a", project, codex)
    time.sleep(QUIET)
    _speak("cx-a")  # its own turn prints

    async def check(conn):
        await db.upsert_session(conn, session_id=UUID, hub_id="h", hostname="h", cwd=str(project),
                                status="active", tmux_session="cx-a", tool="codex")
        await event_processor.process_event(conn, _start("s-new", project), "h", "h")
        assert await db.get_session(conn, "s-new") is None
        assert (await db.get_session(conn, UUID))["status"] == "active"

    _run(check)
