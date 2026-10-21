"""Endpoints a phone uses: send, interrupt, seen, messages, notify and the
event stream.

The tmux tests run against a private tmux server (TMUX_TMPDIR in a temp
directory, $TMUX unset), never the user's. The "agent" there is a small
Python program started under the name `claude`: it turns on bracketed
paste mode like the real CLIs and writes every byte it receives to a file.
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from agent_hub import db
from agent_hub.api import sessions as sessions_api
from agent_hub.api import stream as stream_api
from agent_hub.config import HubConfig
from agent_hub.services import tmux_input
from agent_hub.services.attention import attention
from agent_hub.services.tmux_input import pick_agent_pane

needs_tmux = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")

READER = r"""
import os, sys, tty
out = sys.argv[1]
fd = sys.stdin.fileno()
tty.setraw(fd)
os.write(sys.stdout.fileno(), b"\x1b[?2004h")
open(out + ".ready", "w").close()
data = b""
while True:
    chunk = os.read(fd, 4096)
    if not chunk:
        break
    data += chunk
    with open(out, "wb") as f:
        f.write(data)
"""


def test_pick_agent_pane():
    assert pick_agent_pane([("%1", "zsh", False), ("%2", "claude", False)]) == "%2"
    assert pick_agent_pane([("%1", "codex", False), ("%2", "codex", True)]) == "%2"
    assert pick_agent_pane([("%1", "bash", True), ("%2", "node", False)]) == "%2"
    # Only a shell left: no agent, so nothing may be typed there.
    assert pick_agent_pane([("%1", "bash", True)]) is None
    assert pick_agent_pane([]) is None


@pytest.fixture
def private_tmux(tmp_path, monkeypatch):
    tmux_dir = tmp_path / "tmux"
    tmux_dir.mkdir()
    monkeypatch.setenv("TMUX_TMPDIR", str(tmux_dir))
    monkeypatch.delenv("TMUX", raising=False)
    yield
    subprocess.run(["tmux", "kill-server"], capture_output=True)


def _start_agent(tmp_path, name: str, program: str = "claude") -> str:
    """A tmux session whose pane runs the reader under `program`'s name.
    Returns the file the reader writes its input to."""
    reader = tmp_path / "reader.py"
    reader.write_text(READER)
    link = tmp_path / program
    if not link.exists():
        link.symlink_to(os.path.realpath(sys.executable))
    out = tmp_path / f"{name}.out"
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", name, "-x", "100", "-y", "30",
         f"{link} {reader} {out}"],
        check=True,
    )
    _wait_for(lambda: os.path.exists(f"{out}.ready"), "the reader to start")
    return str(out)


def _wait_for(check, what: str, timeout: float = 5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def _read_until(path: str, ending: bytes) -> bytes:
    def check():
        try:
            data = open(path, "rb").read()
        except FileNotFoundError:
            return None
        return data if data.endswith(ending) else None
    return _wait_for(check, f"input ending with {ending!r}")


def _request(conn) -> SimpleNamespace:
    config = HubConfig(hub_id="t", db_path=":memory:")
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db=conn, config=config)))


async def _session(conn, sid, tmux, status="idle"):
    await db.upsert_session(
        conn, session_id=sid, hub_id="h", hostname="h", cwd="/w",
        status=status, tmux_session=tmux,
    )


@needs_tmux
def test_paste_is_bracketed_and_enter_submits(tmp_path, private_tmux):
    out = _start_agent(tmp_path, "agent")

    async def run():
        pane = await tmux_input.agent_pane("agent")
        await tmux_input.paste(pane, "line one\nline two")
        await tmux_input.press(pane, "Enter")

    asyncio.run(run())
    # tmux turns the newline into CR inside the paste, as a terminal does.
    assert _read_until(out, b"\r") == b"\x1b[200~line one\rline two\x1b[201~\r"


@needs_tmux
def test_send_and_interrupt_endpoints(tmp_path, private_tmux):
    out = _start_agent(tmp_path, "agent")
    subprocess.run(["tmux", "new-session", "-d", "-s", "shell-only", "sh"], check=True)

    async def run():
        conn = await db.init_db(":memory:")
        try:
            await check(conn)
        finally:
            await conn.close()

    async def check(conn):
        req = _request(conn)
        await _session(conn, "s1", "agent")
        await _session(conn, "s2", "shell-only")

        res = await sessions_api.send_text(req, "s1", sessions_api.SendRequest(text="  修一下\r\nthen test  "))
        assert res == {"ok": True, "submitted": True}
        assert (await db.get_session(conn, "s1"))["seen_at"] is not None

        with pytest.raises(HTTPException) as e:
            await sessions_api.send_text(req, "s2", sessions_api.SendRequest(text="hello"))
        assert e.value.status_code == 409 and "No agent" in e.value.detail

        await db.update_session_pending_tool(conn, "s1", "Bash", "rm -rf build")
        with pytest.raises(HTTPException) as e:
            await sessions_api.send_text(req, "s1", sessions_api.SendRequest(text="y"))
        assert e.value.status_code == 409
        await db.update_session_pending_tool(conn, "s1", None, None)

        with pytest.raises(HTTPException) as e:
            await sessions_api.send_text(req, "s1", sessions_api.SendRequest(text=" \n "))
        assert e.value.status_code == 400

        sessions_api._last_interrupt.clear()
        assert (await sessions_api.interrupt(req, "s1"))["ignored"] is False
        assert (await sessions_api.interrupt(req, "s1"))["ignored"] is True

    asyncio.run(run())
    expected = "\x1b[200~修一下\rthen test\x1b[201~\r\x1b".encode()
    assert _read_until(out, b"\x1b") == expected


def test_messages_seen_and_notify():
    async def run(tmp):
        conn = await db.init_db(":memory:")
        try:
            await check(conn, tmp)
        finally:
            await conn.close()

    async def check(conn, tmp):
        req = _request(conn)
        await _session(conn, "s1", "t1")
        path = os.path.join(tmp, "s1.jsonl")
        with open(path, "w") as f:
            f.write(json.dumps({"type": "user", "timestamp": "t", "message": {"content": "hi"}}) + "\n")
        await conn.execute("UPDATE sessions SET transcript_path = ? WHERE session_id = 's1'", (path,))
        await conn.commit()
        res = await sessions_api.session_messages(req, "s1", limit=10)
        assert res["items"] == [{"role": "user", "text": "hi", "ts": "t"}]

        with pytest.raises(HTTPException) as e:
            await sessions_api.mark_seen(req, "nope")
        assert e.value.status_code == 404

        await attention.start(conn)
        try:
            res = await sessions_api.send_notification(sessions_api.NotifyRequest(message=" done ", title="train"))
            assert res["notice"]["message"] == "done"
            with pytest.raises(HTTPException):
                await sessions_api.send_notification(sessions_api.NotifyRequest(message="  "))
        finally:
            await attention.stop()

    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(run(tmp))


def test_stream_sends_snapshot_then_changes():
    async def run():
        conn = await db.init_db(":memory:")
        await _session(conn, "s1", "t1")
        await db.record_turn_end(conn, "s1", "All done.")
        await attention.start(conn)
        try:
            response = await stream_api.stream()
            assert response.media_type == "text/event-stream"
            body = response.body_iterator
            assert await body.__anext__() == "retry: 5000\n\n"
            first = await body.__anext__()
            kind, data = _parse_event(first)
            assert kind == "snapshot"
            assert [(s["session_id"], s["unseen"], s["last_message"]) for s in data["sessions"]] == [("s1", True, "All done.")]

            await sessions_api.mark_seen(_request(conn), "s1")
            kind, data = _parse_event(await asyncio.wait_for(body.__anext__(), timeout=3))
            assert kind == "session" and data["session_id"] == "s1" and data["unseen"] is False
            await body.aclose()
            assert not attention._subscribers, "closing the stream unsubscribes"
        finally:
            await attention.stop()
            await conn.close()

    asyncio.run(run())


def _parse_event(text: str):
    lines = text.strip().split("\n")
    assert lines[0].startswith("event: ") and lines[1].startswith("data: ")
    return lines[0][7:], json.loads(lines[1][6:])
