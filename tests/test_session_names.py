"""A session's name in the Hub (its title) is kept on the tmux session as a
user option, so it outlives the Hub process and passes to the next
conversation in the same tmux. Also: the Hub removes the tmux hook that
earlier versions installed for the Codex scan.

Runs against a private tmux server (TMUX_TMPDIR in a temp directory, $TMUX
unset).
"""
import asyncio
import shutil
import subprocess
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from agent_hub import db
from agent_hub.api import sessions as sessions_api
from agent_hub.api import tmux as tmux_api
from agent_hub.config import HubConfig
from agent_hub.services import event_processor, session_manager, tmux_input

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")


@pytest.fixture
def private_tmux(tmp_path, monkeypatch):
    tmux_dir = tmp_path / "tmux"
    tmux_dir.mkdir()
    monkeypatch.setenv("TMUX_TMPDIR", str(tmux_dir))
    monkeypatch.delenv("TMUX", raising=False)
    yield
    subprocess.run(["tmux", "kill-server"], capture_output=True)


def _request(conn=None):
    config = HubConfig(hub_id="t", db_path=":memory:")
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db=conn, config=config)))


def _start(sid, tmux, cwd):
    return {"session_id": sid, "hook_event_name": "SessionStart", "cwd": str(cwd), "_tmux_session": tmux}


def _run(check):
    async def run():
        conn = await db.init_db(":memory:")
        try:
            await check(conn)
        finally:
            await conn.close()
    asyncio.run(run())


def test_launch_name_reaches_the_session_and_survives_clear_and_rename(tmp_path, private_tmux):
    async def check(conn):
        res = await tmux_api.new_tmux(_request(conn), tmux_api.NewTmuxRequest(
            name="work-1", cwd=str(tmp_path), title="  Paper   review  "))
        assert res["title"] == "Paper review"
        assert await tmux_input.tmux_title("work-1") == "Paper review"
        listed = {s["name"]: s["title"] for s in await tmux_api._tmux_ls()}
        assert listed["work-1"] == "Paper review"

        # The agent reports in: its row takes the name from tmux.
        await event_processor.process_event(conn, _start("s1", "work-1", tmp_path), "h", "h")
        assert (await db.get_session(conn, "s1"))["title"] == "Paper review"
        # /clear: a new conversation in the same tmux keeps the name.
        await event_processor.process_event(conn, _start("s2", "work-1", tmp_path), "h", "h")
        assert (await db.get_session(conn, "s1"))["status"] == "stopped"
        assert (await db.get_session(conn, "s2"))["title"] == "Paper review"
        # A rename moves to tmux too, so the next conversation has it.
        await sessions_api.set_title(_request(conn), "s2", sessions_api.TitleRequest(title="Fig 3"))
        assert await tmux_input.tmux_title("work-1") == "Fig 3"
        await event_processor.process_event(conn, _start("s3", "work-1", tmp_path), "h", "h")
        assert (await db.get_session(conn, "s3"))["title"] == "Fig 3"
        # Clearing the name clears it on tmux.
        await sessions_api.set_title(_request(conn), "s3", sessions_api.TitleRequest(title=""))
        assert await tmux_input.tmux_title("work-1") is None

        with pytest.raises(HTTPException) as e:
            await tmux_api.new_tmux(_request(conn), tmux_api.NewTmuxRequest(cwd=str(tmp_path), title="x" * 81))
        assert e.value.status_code == 400

    _run(check)


def test_name_given_before_names_lived_on_tmux_is_carried_over(tmp_path, private_tmux):
    subprocess.run(["tmux", "new-session", "-d", "-s", "old-1", "sleep 600"], check=True)

    async def check(conn):
        await event_processor.process_event(conn, _start("s1", "old-1", tmp_path), "h", "h")
        await db.set_session_title(conn, "s1", "Notes")  # renamed by an older Hub
        await event_processor.process_event(conn, _start("s2", "old-1", tmp_path), "h", "h")
        assert (await db.get_session(conn, "s2"))["title"] == "Notes"
        assert await tmux_input.tmux_title("old-1") == "Notes"

    _run(check)


def test_the_old_codex_scan_hook_is_removed_and_others_are_kept(private_tmux):
    subprocess.run(["tmux", "new-session", "-d", "-s", "any", "sleep 600"], check=True)

    def hooks():
        return subprocess.run(["tmux", "show-hooks", "-g", "session-created"], capture_output=True, text=True).stdout

    old = 'run-shell -b "curl -s -o /dev/null -X POST http://127.0.0.1:7800/api/internal/tmux-discovered?name=#{session_name}"'
    subprocess.run(["tmux", "set-hook", "-g", "session-created", old], check=True)
    asyncio.run(session_manager.remove_legacy_tmux_session_hook())
    assert "tmux-discovered" not in hooks()

    mine = 'display-message "new session"'
    subprocess.run(["tmux", "set-hook", "-g", "session-created", mine], check=True)
    asyncio.run(session_manager.remove_legacy_tmux_session_hook())
    assert "new session" in hooks()
