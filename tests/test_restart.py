"""Restarting a running session's CLI in place, one session or all of one
tool's sessions.

The tests run against a private tmux server (TMUX_TMPDIR in a temp
directory, $TMUX unset), never the user's. The running "agent" is a
script named `claude` or `codex` that sleeps; the resume command finds a
stand-in of the same name first on PATH, which writes its working
directory and arguments to a file instead of starting a real CLI.
"""
import asyncio
import os
import shutil
import subprocess
import time
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from agent_hub import db
from agent_hub.api import sessions as sessions_api
from agent_hub.api import tmux as tmux_api
from agent_hub.config import HubConfig
from agent_hub.services.session_manager import _pane_shows_working

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")

CODEX_ID = "019a0000-0000-7000-8000-000000000001"


@pytest.fixture
def private_tmux(tmp_path, monkeypatch):
    tmux_dir = tmp_path / "tmux"
    tmux_dir.mkdir()
    monkeypatch.setenv("TMUX_TMPDIR", str(tmux_dir))
    monkeypatch.delenv("TMUX", raising=False)
    yield
    subprocess.run(["tmux", "kill-server"], capture_output=True)


@pytest.fixture
def stand_ins(tmp_path, monkeypatch):
    """`claude` and `codex` on PATH that log their cwd and argv to
    <tmp>/<tool>.<first argument after resume>.log, then keep running."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("claude", "codex"):
        script = bin_dir / tool
        script.write_text(
            "#!/bin/sh\n"
            f'log="{tmp_path}/{tool}.$2.log"\n'
            'printf "%s\\n" "$PWD" "$@" > "$log"\n'
            "sleep 600\n"
        )
        script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return tmp_path


# The bottom of a Claude Code pane, as tmux captures it: the TUI has not
# filled the screen, so blank rows follow the footer.
CLAUDE_FOOTER = """
────────────────────────────────────────
❯\u00a0
────────────────────────────────────────
  [Opus] │ Git/agent-home git:(main)
  Context ██░░░░░░░░ 20% │ Usage ░░░░░░░░░░ 1%
  ✓ Bash ×7 | ✓ Write ×1
  ⏵⏵ auto mode on (shift+tab to cycle)
""" + "\n" * 14


@pytest.mark.parametrize("pane, working", [
    # Current Claude Code: verb, ellipsis, elapsed time; no "esc to interrupt".
    ("● Reading the code.\n\n✻ Elucidating… (9m 25s · ↓ 50.6k tokens · thought for 22s)\n" + CLAUDE_FOOTER, True),
    ("✶ Compacting conversation… (12s · ↑ 1.2k tokens)\n" + CLAUDE_FOOTER, True),
    # Older Claude Code and Codex.
    ("✻ Pondering… (3s · esc to interrupt)\n" + CLAUDE_FOOTER, True),
    ("• Working (11s • esc to interrupt)\n\n› Ask Codex to do anything\n" + "\n" * 20, True),
    # A finished turn.
    ("● All tests pass.\n\n  Thought for 35s, searched for 1 pattern, ran 5 shell commands\n" + CLAUDE_FOOTER, False),
    ("› Ask Codex to do anything\n\n  100% context left\n", False),
])
def test_busy_check_reads_the_status_line_of_a_running_turn(pane, working):
    assert _pane_shows_working(pane) is working


def _start(tmp_path, name: str, program: str | None) -> str:
    """A tmux session whose pane runs a sleeping script named `program`
    under a wrapper shell, or only a shell when program is None. Returns
    the pane id."""
    if program:
        agent = tmp_path / "running" / program
        agent.parent.mkdir(exist_ok=True)
        agent.write_text("#!/bin/sh\nsleep 600\n")
        agent.chmod(0o755)
        command = f"bash -c '{agent}; exec sleep 600'"
    else:
        command = "sh"
    subprocess.run(["tmux", "new-session", "-d", "-s", name, "-x", "100", "-y", "30", command], check=True)
    return subprocess.run(
        ["tmux", "display", "-p", "-t", f"{name}:", "#{pane_id}"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def _wait_for_file(path, timeout: float = 5.0) -> list[str]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(path):
            lines = open(path).read().splitlines()
            if lines:
                return lines
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {path}")


def _request(conn) -> SimpleNamespace:
    config = HubConfig(hub_id="t", db_path=":memory:")
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db=conn, config=config)))


async def _session(conn, sid, tmux, cwd, tool="claude", status="idle"):
    await db.upsert_session(
        conn, session_id=sid, hub_id="h", hostname="h", cwd=str(cwd),
        status=status, tmux_session=tmux, tool=tool,
    )


def _run(check):
    async def run():
        conn = await db.init_db(":memory:")
        try:
            await check(conn)
        finally:
            await conn.close()
    asyncio.run(run())


def test_restart_resumes_the_conversation_in_the_same_pane(tmp_path, private_tmux, stand_ins):
    pane = _start(tmp_path, "work", "claude")
    project = tmp_path / "project"
    project.mkdir()

    async def check(conn):
        req = _request(conn)
        await _session(conn, "s1", "work", project)

        # Waiting for an approval counts as mid-turn: refused unless forced.
        await db.update_session_pending_tool(conn, "s1", "Bash", "make")
        with pytest.raises(HTTPException) as e:
            await sessions_api.restart_one(req, "s1", force=False)
        assert e.value.status_code == 409 and "middle of a turn" in e.value.detail

        result = await sessions_api.restart_one(req, "s1", force=True)
        assert result["ok"] and result["name"] == "work"
        assert result["command"] == "claude --resume s1"

    _run(check)
    assert _wait_for_file(tmp_path / "claude.s1.log") == [str(project), "--resume", "s1"]
    # Same tmux, same pane: an attached terminal stays on it.
    panes = subprocess.run(
        ["tmux", "list-panes", "-s", "-t", "=work", "-F", "#{pane_id}"],
        capture_output=True, text=True, check=True,
    ).stdout.split()
    assert panes == [pane]


def test_restart_refuses_sessions_it_cannot_restart(tmp_path, private_tmux, stand_ins):
    _start(tmp_path, "shell", None)
    _start(tmp_path, "scanned", "codex")

    async def check(conn):
        req = _request(conn)
        await _session(conn, "ended", "shell", tmp_path, status="stopped")
        await _session(conn, "no-cli", "shell", tmp_path)
        await _session(conn, "codex-scanned-1700000000", "scanned", tmp_path, tool="codex")
        for sid, reason in (
            ("ended", "not running"),
            ("no-cli", "No agent"),
            ("codex-scanned-1700000000", "never reported"),
        ):
            with pytest.raises(HTTPException) as e:
                await sessions_api.restart_one(req, sid, force=False)
            assert e.value.status_code == 409 and reason in e.value.detail, sid

    _run(check)


def test_restart_all_restarts_one_tool_and_skips_busy_sessions(tmp_path, private_tmux, stand_ins, monkeypatch):
    # No terminal is attached, so the Codex pane starts after the wait.
    monkeypatch.setattr(tmux_api, "CODEX_TERMINAL_WAIT_S", 0.5)
    for name, program in (("c-idle", "claude"), ("c-busy", "claude"), ("c-shell", None), ("x-idle", "codex")):
        _start(tmp_path, name, program)

    async def check(conn):
        req = _request(conn)
        await _session(conn, "c1", "c-idle", tmp_path)
        await _session(conn, "c2", "c-busy", tmp_path, status="active")
        await db.update_session_pending_tool(conn, "c2", "Bash", "make")
        await _session(conn, "c3", "c-shell", tmp_path)
        await _session(conn, CODEX_ID, "x-idle", tmp_path, tool="codex")

        res = await sessions_api.restart_all(req, sessions_api.RestartAllRequest(tool="claude"))
        assert [s["session_id"] for s in res["restarted"]] == ["c1"]
        reasons = {s["session_id"]: s["reason"] for s in res["skipped"]}
        assert reasons["c2"] == "in the middle of a turn"
        assert "No agent" in reasons["c3"]
        assert CODEX_ID not in reasons, "Codex sessions are left to the Codex restart"
        assert not os.path.exists(tmp_path / f"codex.{CODEX_ID}.log")

        res = await sessions_api.restart_all(req, sessions_api.RestartAllRequest(tool="codex"))
        assert [s["session_id"] for s in res["restarted"]] == [CODEX_ID] and not res["skipped"]

        with pytest.raises(HTTPException):
            await sessions_api.restart_all(req, sessions_api.RestartAllRequest(tool="gemini"))

    _run(check)
    assert _wait_for_file(tmp_path / "claude.c1.log")[1:] == ["--resume", "c1"]
    assert _wait_for_file(tmp_path / f"codex.{CODEX_ID}.log")[1:] == ["resume", CODEX_ID]
