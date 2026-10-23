"""pi (pi-coding-agent) sessions: the launch, resume and restart commands,
the pane's process and busy line, pi's transcripts, and the extension that
reports pi's events to the Hub.

tmux tests run against a private tmux server (TMUX_TMPDIR in a temp
directory, $TMUX unset); the "pi" there is a shell script of that name. The
extension test runs scripts/pi-hub-extension.js under node with a stand-in
pi and a stand-in Hub.
"""
import asyncio
import json
import os
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_hub import db
from agent_hub.api import sessions as sessions_api
from agent_hub.api import tmux as tmux_api
from agent_hub.config import HubConfig
from agent_hub.services import event_processor, session_manager, tmux_input
from agent_hub.services.conversation import find_transcript, read_messages
from agent_hub.services.restore import resume_argv, tool_family
from agent_hub.services.session_manager import _pane_shows_working
from agent_hub.services.transcript_reader import read_usage

UUID = "01a15d5e-c013-76fe-8a4a-9d47d4b50823"
EXTENSION = Path(__file__).resolve().parent.parent / "scripts" / "pi-hub-extension.js"
needs_tmux = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")


@pytest.fixture
def private_tmux(tmp_path, monkeypatch):
    tmux_dir = tmp_path / "tmux"
    tmux_dir.mkdir()
    monkeypatch.setenv("TMUX_TMPDIR", str(tmux_dir))
    monkeypatch.delenv("TMUX", raising=False)
    yield
    subprocess.run(["tmux", "kill-server"], capture_output=True)


@pytest.fixture
def pi(tmp_path, monkeypatch):
    """A `pi` on PATH that logs its cwd and argv to <tmp>/pi.log, then
    keeps running."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "pi"
    script.write_text(f'#!/bin/sh\nprintf "%s\\n" "$PWD" "$@" > "{tmp_path}/pi.log"\nsleep 600\n')
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return tmp_path / "pi.log"


def _request(conn=None):
    config = HubConfig(hub_id="t", db_path=":memory:")
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db=conn, config=config)))


def _run(check):
    async def run():
        conn = await db.init_db(":memory:")
        try:
            await check(conn)
        finally:
            await conn.close()
    asyncio.run(run())


def _read_log(path, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists() and path.read_text().strip():
            return path.read_text().splitlines()
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {path}")


def test_pi_family_and_resume_command():
    assert resume_argv("pi", UUID) == ["pi", "--session", UUID]
    assert [tool_family(t) for t in ("pi", "claude2", "codex-yolo", "omx-yolo", "tmux", None)] == [
        "pi", "claude", "codex", "codex", None, None,
    ]


def test_pi_pane_process_and_busy_line():
    assert tmux_input._is_agent(1, "pi")
    working = "● Reading files\n\n⠋ Working (escape to interrupt)\n\n────\n> \n────\n" + "\n" * 10
    assert _pane_shows_working(working)
    assert not _pane_shows_working("● Done.\n\n────\n> \n────\n")


@needs_tmux
def test_pi_launch_gives_pi_the_session_name(tmp_path, private_tmux, pi):
    async def check(conn):
        res = await tmux_api.new_tmux(_request(conn), tmux_api.NewTmuxRequest(
            name="pi-1", cwd=str(tmp_path), command="pi", title="Paper review"))
        assert res["command"] == "pi" and res["title"] == "Paper review"

    _run(check)
    assert _read_log(pi) == [str(tmp_path), "--name", "Paper review"]


@needs_tmux
def test_pi_restart_resumes_the_session_by_id(tmp_path, private_tmux, pi):
    subprocess.run(["tmux", "new-session", "-d", "-s", "pi-1", "-c", str(tmp_path), "pi"], check=True)
    _read_log(pi)
    pi.unlink()

    async def check(conn):
        await db.upsert_session(
            conn, session_id=UUID, hub_id="h", hostname="h", cwd=str(tmp_path),
            status="idle", tmux_session="pi-1", tool="pi",
        )
        res = await sessions_api.restart_all(_request(conn), sessions_api.RestartAllRequest(tool="claude"))
        assert res["restarted"] == [] and res["skipped"] == []
        res = await sessions_api.restart_all(_request(conn), sessions_api.RestartAllRequest(tool="pi"))
        assert [s["session_id"] for s in res["restarted"]] == [UUID]

    _run(check)
    assert _read_log(pi) == [str(tmp_path), "--session", UUID]


@needs_tmux
def test_pi_events_make_a_pi_session_and_never_a_pending_approval(tmp_path, private_tmux):
    # A pane that looks like a Claude approval prompt must not read as one.
    subprocess.run(["tmux", "new-session", "-d", "-s", "pi-1", "-x", "100", "-y", "30",
                    "printf 'Do you want to proceed?\\n❯ 1. Yes\\n  2. No\\n'; sleep 600"], check=True)

    async def check(conn):
        base = {"session_id": UUID, "cwd": str(tmp_path), "_tool": "pi"}
        await event_processor.process_event(
            conn, {**base, "hook_event_name": "SessionStart", "_tmux_session": "pi-1", "model": "deepseek-flash"}, "h", "host")
        await event_processor.process_event(conn, {**base, "hook_event_name": "UserPromptSubmit", "prompt": "hi"}, "h", "host")
        row = await db.get_session(conn, UUID)
        assert row["tool"] == "pi" and row["tmux_session"] == "pi-1" and row["status"] == "active"
        await session_manager.parse_one(conn, UUID)
        assert (await db.get_session(conn, UUID))["pending_tool"] is None
        await event_processor.process_event(
            conn, {**base, "hook_event_name": "Stop", "last_assistant_message": "Hello."}, "h", "host")
        row = await db.get_session(conn, UUID)
        assert row["status"] == "idle"

    _run(check)


def _write_pi_session(root: Path) -> Path:
    project = root / "--home-u-project--"
    project.mkdir(parents=True)
    path = project / f"2026-10-23T09-00-00-000Z_{UUID}.jsonl"
    entries = [
        {"type": "session", "version": 3, "id": UUID, "timestamp": "2026-10-23T09:00:00.000Z", "cwd": "/home/u/project"},
        {"type": "model_change", "id": "a1", "parentId": None, "timestamp": "2026-10-23T09:00:00.100Z",
         "provider": "deepseek", "modelId": "deepseek-flash"},
        {"type": "message", "id": "a2", "parentId": "a1", "timestamp": "2026-10-23T09:00:01.000Z",
         "message": {"role": "user", "content": [{"type": "text", "text": "run the tests"}], "timestamp": 1}},
        {"type": "message", "id": "a3", "parentId": "a2", "timestamp": "2026-10-23T09:00:02.000Z",
         "message": {"role": "assistant", "model": "deepseek-flash", "stopReason": "toolUse",
                     "usage": {"input": 100, "output": 20, "cacheRead": 50, "cacheWrite": 0, "totalTokens": 170},
                     "content": [{"type": "thinking", "thinking": "Run pytest."},
                                 {"type": "toolCall", "id": "c1", "name": "bash", "arguments": {"command": "pytest -q"}},
                                 {"type": "toolCall", "id": "c2", "name": "write", "arguments": {"path": "a.py", "content": "x"}}]}},
        {"type": "message", "id": "a4", "parentId": "a3", "timestamp": "2026-10-23T09:00:03.000Z",
         "message": {"role": "toolResult", "toolCallId": "c1", "content": [{"type": "text", "text": "3 passed"}]}},
        {"type": "message", "id": "a5", "parentId": "a4", "timestamp": "2026-10-23T09:00:04.000Z",
         "message": {"role": "assistant", "model": "deepseek-flash", "stopReason": "stop",
                     "usage": {"input": 200, "output": 10, "cacheRead": 150, "cacheWrite": 5, "totalTokens": 365},
                     "content": [{"type": "text", "text": "All 3 tests pass."}]}},
        {"type": "message", "id": "a6", "parentId": "a5", "timestamp": "2026-10-23T09:00:05.000Z",
         "message": {"role": "bashExecution", "command": "git status", "output": "", "exitCode": 0}},
        {"type": "message", "id": "a7", "parentId": "a6", "timestamp": "2026-10-23T09:00:06.000Z",
         "message": {"role": "assistant", "model": "deepseek-flash", "stopReason": "aborted", "content": []}},
    ]
    path.write_text("".join(json.dumps(e) + "\n" for e in entries))
    return path


def test_pi_transcript_messages_and_usage(tmp_path, monkeypatch):
    path = _write_pi_session(tmp_path / "sessions")
    monkeypatch.setenv("PI_CODING_AGENT_SESSION_DIR", str(tmp_path / "sessions"))
    assert find_transcript({"session_id": UUID, "tool": "pi"}, []) == str(path)

    messages = [(m["role"], m["text"]) for m in read_messages(str(path), "pi")]
    assert messages == [
        ("user", "run the tests"),
        ("tool", "$ pytest -q"),
        ("tool", "Write a.py"),
        ("assistant", "All 3 tests pass."),
        ("user", "! git status"),
        ("note", "Interrupted"),
    ]
    assert read_usage(str(path)) == {
        "input_tokens": 300, "output_tokens": 30, "cache_read_tokens": 200,
        "cache_create_tokens": 5, "model": "deepseek-flash",
    }


# ── The extension ────────────────────────────────────────────

HARNESS = """
import { pathToFileURL } from "node:url";
const extension = (await import(pathToFileURL(process.argv[2]).href)).default;
const handlers = {};
extension({ on: (name, fn) => { (handlers[name] ||= []).push(fn); } });
const branch = [];
const ctx = {
  mode: process.argv[3], cwd: "/home/u/project", model: { id: "deepseek-flash" },
  sessionManager: {
    getSessionId: () => "sid-1", getSessionFile: () => "/s/sid-1.jsonl",
    getSessionName: () => "Named", getBranch: () => branch,
  },
};
const fire = async (name, event) => { for (const fn of handlers[name] || []) await fn(event, ctx); };
await fire("session_start", { reason: "startup" });
await fire("before_agent_start", { messageType: "user", prompt: "run the tests" });
await fire("tool_execution_start", { toolCallId: "t1", toolName: "bash", args: { command: "pytest -q", timeout: 60 } });
await fire("tool_execution_start", { toolCallId: "t2", toolName: "write", args: { path: "a.py", content: "secret" } });
await fire("tool_execution_end", { toolCallId: "t1", toolName: "bash", result: {}, isError: false });
branch.push({ type: "message", message: { role: "user", content: "run the tests" } });
branch.push({ type: "message", message: { role: "assistant", content: [{ type: "thinking", thinking: "x" }, { type: "text", text: "All pass." }] } });
await fire("agent_settled", { aborted: false });
await fire("session_info_changed", { name: "Renamed" });
await new Promise((resolve) => setTimeout(resolve, 1000));
"""


@pytest.fixture
def hub():
    """A stand-in Hub that records every POST as (path, JSON body)."""
    posts = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            posts.append((self.path, json.loads(body)))
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}", posts
    server.shutdown()


def _run_extension(tmp_path, url, mode="tui", in_tmux=True):
    harness = tmp_path / "harness.mjs"
    harness.write_text(HARNESS)
    fake = tmp_path / "fakebin"
    fake.mkdir(exist_ok=True)
    (fake / "tmux").write_text("#!/bin/sh\necho pi-1\n")
    (fake / "tmux").chmod(0o755)
    env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
    env.update(PATH=f"{fake}{os.pathsep}{os.environ['PATH']}", AGENT_HUB_URL=url)
    if in_tmux:
        env.update(TMUX="/tmp/tmux-test/default,1,0", TMUX_PANE="%1")
    subprocess.run(["node", str(harness), str(EXTENSION), mode], env=env, check=True, timeout=30,
                   capture_output=True)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_extension_reports_pi_events_like_hooks(tmp_path, hub):
    url, posts = hub
    _run_extension(tmp_path, url)
    events = []
    for path, body in posts:
        if path.startswith("/api/events?"):
            assert "tool=pi" in path and "tmux_session=pi-1" in path
            assert body["session_id"] == "sid-1" and body["cwd"] == "/home/u/project"
            assert body["transcript_path"] == "/s/sid-1.jsonl"
            fields = {k: body[k] for k in ("prompt", "tool_name", "tool_input", "last_assistant_message", "model", "source") if k in body}
            events.append((body["hook_event_name"], fields))
        else:
            events.append((path, body))
    assert events == [
        ("SessionStart", {"model": "deepseek-flash", "source": "startup"}),
        ("/api/sessions/sid-1/title", {"title": "Named"}),
        ("UserPromptSubmit", {"prompt": "run the tests"}),
        ("PreToolUse", {"tool_name": "Bash", "tool_input": {"command": "pytest -q"}}),
        # File contents never leave pi.
        ("PreToolUse", {"tool_name": "Write", "tool_input": {"file_path": "a.py"}}),
        ("PostToolUse", {"tool_name": "Bash", "tool_input": {"command": "pytest -q"}}),
        ("Stop", {"last_assistant_message": "All pass."}),
        ("/api/sessions/sid-1/title", {"title": "Renamed"}),
    ]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
@pytest.mark.parametrize("mode, in_tmux", [("rpc", True), ("print", True), ("tui", False)])
def test_extension_is_silent_outside_the_terminal_ui_in_tmux(tmp_path, hub, mode, in_tmux):
    url, posts = hub
    _run_extension(tmp_path, url, mode=mode, in_tmux=in_tmux)
    assert posts == []
