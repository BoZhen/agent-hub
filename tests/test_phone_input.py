"""Input from the phone page: keys pressed in a session's agent pane
(POST /api/sessions/{id}/keys), typing into a tmux the Hub has no session
for yet (POST /api/tmux/input), and files uploaded for a reply
(POST /api/uploads, the `attachments` of a reply).

Runs against a private tmux server (TMUX_TMPDIR in a temp directory, $TMUX
unset); the agent there is a shell script named "claude" that writes what it
receives to a file.
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
from agent_hub.api import uploads as uploads_api
from agent_hub.config import HubConfig
from agent_hub.services import uploads

SID = "0a19b2c4-1111-4222-8333-944455556666"
pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")


@pytest.fixture
def private_tmux(tmp_path, monkeypatch):
    tmux_dir = tmp_path / "tmux"
    tmux_dir.mkdir()
    monkeypatch.setenv("TMUX_TMPDIR", str(tmux_dir))
    monkeypatch.delenv("TMUX", raising=False)
    yield
    subprocess.run(["tmux", "kill-server"], capture_output=True)


@pytest.fixture
def agent(tmp_path, private_tmux, monkeypatch):
    """tmux "cl-1" running a "claude" that records its input in raw mode."""
    received = tmp_path / "received"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "claude"
    script.write_text(f"#!/bin/sh\nstty raw -echo\ncat > '{received}'\n")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    subprocess.run(["tmux", "new-session", "-d", "-s", "cl-1", "claude"], check=True)
    time.sleep(0.5)
    return received


def _request(conn=None, uploads_dir="/nonexistent", body=b""):
    config = HubConfig(hub_id="t", db_path=":memory:", uploads_dir=str(uploads_dir))

    async def stream():
        for i in range(0, len(body), 4096):
            yield body[i:i + 4096]

    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db=conn, config=config)), stream=stream)


def _run(check):
    async def run():
        conn = await db.init_db(":memory:")
        try:
            await check(conn)
        finally:
            await conn.close()
    asyncio.run(run())


def _wait_for(path, size, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists() and len(path.read_bytes()) >= size:
            return path.read_bytes()
        time.sleep(0.05)
    return path.read_bytes() if path.exists() else b""


def test_keys_reach_the_agent_and_only_known_keys_pass(tmp_path, agent):
    async def check(conn):
        await db.upsert_session(conn, session_id=SID, hub_id="h", hostname="h", cwd=str(tmp_path),
                                status="idle", tmux_session="cl-1", tool="claude")
        keys = sessions_api.KeysRequest
        assert await sessions_api.press_keys(_request(conn), SID, keys(keys=["Left", "Right"])) == {"ok": True}
        assert await sessions_api.press_keys(_request(conn), SID, keys(keys=["Enter"])) == {"ok": True}
        for bad in ([], ["C-c"], ["Left"] * 9):
            with pytest.raises(HTTPException) as e:
                await sessions_api.press_keys(_request(conn), SID, keys(keys=bad))
            assert e.value.status_code == 400

    _run(check)
    assert _wait_for(agent, 7) == b"\x1b[D\x1b[C\r"


def test_enter_is_refused_while_an_approval_waits(tmp_path, agent):
    async def check(conn):
        await db.upsert_session(conn, session_id=SID, hub_id="h", hostname="h", cwd=str(tmp_path),
                                status="active", tmux_session="cl-1", tool="claude")
        await db.update_session_pending_tool(conn, SID, "Bash", "rm -rf build")
        with pytest.raises(HTTPException) as e:
            await sessions_api.press_keys(_request(conn), SID, sessions_api.KeysRequest(keys=["Enter"]))
        assert e.value.status_code == 409
        # Arrows only move the selection; they still go through.
        await sessions_api.press_keys(_request(conn), SID, sessions_api.KeysRequest(keys=["Down"]))

    _run(check)
    assert _wait_for(agent, 3) == b"\x1b[B"


def test_typing_into_a_tmux_without_a_session(agent):
    async def check():
        req = tmux_api.TmuxInputRequest
        assert await tmux_api.tmux_type(_request(), req(name="cl-1", text="hello there")) == {"ok": True}
        assert await tmux_api.tmux_type(_request(), req(name="cl-1", keys=["Escape"])) == {"ok": True}
        for bad, code in ((req(name="cl-1"), 400), (req(name="bad name"), 400),
                          (req(name="cl-1", keys=["C-c"]), 400), (req(name="no-such", text="x"), 404)):
            with pytest.raises(HTTPException) as e:
                await tmux_api.tmux_type(_request(), bad)
            assert e.value.status_code == code

    asyncio.run(check())
    assert _wait_for(agent, 13) == b"hello there\r\x1b"


def _upload(folder, name, body):
    return asyncio.run(uploads_api.upload(_request(uploads_dir=folder, body=body), name))


def test_upload_saves_the_file_under_a_safe_name(tmp_path):
    folder = tmp_path / "uploads"
    saved = _upload(folder, "Screenshot 2026-10-24 (1).png", b"\x89PNG fake")
    path = folder / saved["name"]
    assert saved["path"] == str(path) and path.read_bytes() == b"\x89PNG fake"
    assert saved["name"].endswith("-Screenshot-2026-10-24-1.png") and " " not in saved["name"]
    assert saved["image"] is True and saved["size"] == 9
    assert _upload(folder, "../../notes.pdf", b"%PDF")["name"].endswith("-notes.pdf")
    assert sorted(p.parent for p in folder.iterdir()) == [folder, folder]
    for body, code in ((b"", 400), (b"x" * (uploads.MAX_BYTES + 1), 400)):
        with pytest.raises(HTTPException) as e:
            _upload(folder, "a.png", body)
        assert e.value.status_code == code
    assert len(list(folder.iterdir())) == 2  # no partial file left behind


def test_old_uploads_are_removed(tmp_path):
    folder = tmp_path / "uploads"
    old = _upload(folder, "old.png", b"old")["path"]
    stale = time.time() - (uploads.KEEP_DAYS + 1) * 86400
    os.utime(old, (stale, stale))
    _upload(folder, "new.png", b"new")
    assert not os.path.exists(old) and len(list(folder.iterdir())) == 1


def test_attachments_are_pasted_before_the_text(tmp_path, agent, monkeypatch):
    monkeypatch.setattr(sessions_api.tmux_input, "ATTACH_DELAY_S", 0.05)
    folder = tmp_path / "uploads"
    shot = _upload(folder, "shot.png", b"png")["path"]
    notes = _upload(folder, "notes.txt", b"txt")["path"]
    outside = tmp_path / "secret.png"
    outside.write_bytes(b"png")

    async def check(conn):
        await db.upsert_session(conn, session_id=SID, hub_id="h", hostname="h", cwd=str(tmp_path),
                                status="idle", tmux_session="cl-1", tool="claude")
        send = sessions_api.SendRequest
        request = _request(conn, uploads_dir=folder)
        assert (await sessions_api.send_text(request, SID, send(text="look", attachments=[shot, notes])))["ok"]
        # Files alone are a message too.
        assert (await sessions_api.send_text(request, SID, send(attachments=[shot])))["ok"]
        for bad in ([str(outside)], [str(folder / "missing.png")], ["shot.png"], [shot] * 11):
            with pytest.raises(HTTPException) as e:
                await sessions_api.send_text(request, SID, send(text="x", attachments=bad))
            assert e.value.status_code == 400
        with pytest.raises(HTTPException) as e:
            await sessions_api.send_text(request, SID, send(text="  "))
        assert e.value.status_code == 400

    _run(check)
    expected = f"{shot} {notes} look\r{shot} \r".encode()
    assert _wait_for(agent, len(expected)) == expected


def test_attachments_reach_a_tmux_without_a_session(tmp_path, agent):
    folder = tmp_path / "uploads"
    shot = _upload(folder, "shot.png", b"png")["path"]

    async def check():
        req = tmux_api.TmuxInputRequest
        request = _request(uploads_dir=folder)
        assert await tmux_api.tmux_type(request, req(name="cl-1", attachments=[shot])) == {"ok": True}
        with pytest.raises(HTTPException) as e:
            await tmux_api.tmux_type(request, req(name="cl-1", attachments=["/etc/passwd"]))
        assert e.value.status_code == 400

    asyncio.run(check())
    assert _wait_for(agent, len(shot) + 2) == f"{shot} \r".encode()
