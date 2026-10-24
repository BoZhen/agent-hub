"""opencode 2 sessions: the launch, resume and restart commands, the pane's
process, busy line and permission prompt, the keys for Approve and Esc,
opencode's database, and the plugin that reports opencode's events.

tmux tests run against a private tmux server (TMUX_TMPDIR in a temp
directory, $TMUX unset); the "opencode" there is a shell script of that
name. The database is a stand-in with the columns the Hub reads. The plugin
test runs scripts/opencode-hub-plugin.js under node with a stand-in
opencode and a stand-in Hub.
"""
import asyncio
import json
import os
import shutil
import sqlite3
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from agent_hub import db
from agent_hub.api import sessions as sessions_api
from agent_hub.api import tmux as tmux_api
from agent_hub.config import HubConfig
from agent_hub.services import event_processor, session_manager, tmux_input
from agent_hub.services.conversation import find_transcript, read_messages
from agent_hub.services.restore import resume_argv, tool_family
from agent_hub.services.session_manager import _pane_shows_working, _parse_opencode_permission_prompt

SID = "ses_e92e569cbffeg8FKRqe0iW6Aw8"
PLUGIN = Path(__file__).resolve().parent.parent / "scripts" / "opencode-hub-plugin.js"
needs_tmux = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

# The bottom of opencode's terminal UI, as tmux captures it.
BUSY = (
    "  ┃  $ sleep 20\n"
    "  ┃\n"
    "     Press ctrl+b to move running work to the background\n"
    "  ┃\n"
    "  ┃  Build · GLM 5.3 Flash zhipu\n"
    "  ╹▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀\n"
    "   ⬝■■■■■■⬝ esc interrupt                              ↓ 1 shell ·\n"
)
IDLE = (
    "     Tool finished.\n"
    "     Build · GLM 5.3 Flash · 1.9s · 7.7 tok/s\n"
    "  ┃\n"
    "  ┃  Build · GLM 5.3 Flash zhipu\n"
    "  ╹▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀\n"
    "  ~/project  127 (0%)  ctrl+p commands\n"
)
GAP = " " * 60
SHELL_PROMPT_WIDE = (
    f"  ┃  ⠧ echo hi\n\n"
    f"  ┃{GAP}Connect from 75+ providers\n"
    f"  ┃  △ Permission required{GAP}use other models, including\n"
    f"  ┃{GAP}Claude, GPT, Gemini etc\n"
    f"  ┃  $ rm -rf build\n"
    f"  ┃{GAP}Connect provider   /connect\n"
    f"  ┃\n"
    f"  ┃   Allow once   Always allow   Reject          ctrl+f fullscreen  ⇆ select  enter confirm\n"
    f"  ┃{GAP}~/project\n"
    + "\n" * 8
)
READ_PROMPT = (
    "  ┃  △ Permission required\n"
    "  ┃    → Read .env\n"
    "  ┃\n"
    "  ┃  Path: .env\n"
    "  ┃\n"
    "  ┃   Allow once   Always allow   Reject          ctrl+f fullscreen  ⇆ select  enter confirm\n"
    "  ┃\n"
)


@pytest.fixture
def private_tmux(tmp_path, monkeypatch):
    tmux_dir = tmp_path / "tmux"
    tmux_dir.mkdir()
    monkeypatch.setenv("TMUX_TMPDIR", str(tmux_dir))
    monkeypatch.delenv("TMUX", raising=False)
    yield
    subprocess.run(["tmux", "kill-server"], capture_output=True)


@pytest.fixture
def opencode(tmp_path, monkeypatch):
    """An `opencode` on PATH that logs its cwd, AGENT_HUB_TITLE and argv to
    <tmp>/opencode.log, then keeps running."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "opencode"
    script.write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$PWD" "title=$AGENT_HUB_TITLE" "$@" > "{tmp_path}/opencode.log"\nsleep 600\n'
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return tmp_path / "opencode.log"


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


def _read_bytes(path, size, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists() and len(path.read_bytes()) >= size:
            return path.read_bytes()
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {size} bytes in {path}")


def test_opencode_family_and_resume_command():
    assert resume_argv("opencode", SID) == ["opencode", "--standalone", "-s", SID]
    assert tool_family("opencode") == "opencode"
    # "opencode" contains no "codex": it must not read as a Codex session.
    assert tool_family("codex") == "codex"


def test_opencode_pane_process_and_busy_line():
    assert tmux_input._is_agent(1, "opencode")
    assert _pane_shows_working(BUSY)
    assert _pane_shows_working(BUSY.replace("esc interrupt", "esc again to interrupt"))
    assert not _pane_shows_working(IDLE)
    # Free text that mentions the phrase is not a status line.
    assert not _pane_shows_working("  ┃  press esc interrupt to stop\n")


def test_opencode_permission_prompt():
    label = "Allow always in this project"
    # On a wide terminal opencode's sidebar shares the prompt's rows.
    assert _parse_opencode_permission_prompt(SHELL_PROMPT_WIDE) == ("Bash", "rm -rf build", label)
    assert _parse_opencode_permission_prompt(READ_PROMPT) == ("Read", ".env", label)
    assert _parse_opencode_permission_prompt(BUSY) is None
    assert _parse_opencode_permission_prompt(IDLE) is None


@needs_tmux
def test_opencode_launch_runs_a_private_server_and_passes_the_name(tmp_path, private_tmux, opencode):
    async def check(conn):
        res = await tmux_api.new_tmux(_request(conn), tmux_api.NewTmuxRequest(
            name="oc-1", cwd=str(tmp_path), command="opencode", title="Paper review"))
        assert res["command"] == "opencode" and res["title"] == "Paper review"

    _run(check)
    assert _read_log(opencode) == [str(tmp_path), "title=Paper review", "--standalone"]


@needs_tmux
def test_opencode_restart_reopens_the_session_by_id(tmp_path, private_tmux, opencode):
    subprocess.run(["tmux", "new-session", "-d", "-s", "oc-1", "-c", str(tmp_path), "opencode"], check=True)
    _read_log(opencode)
    opencode.unlink()

    async def check(conn):
        await db.upsert_session(
            conn, session_id=SID, hub_id="h", hostname="h", cwd=str(tmp_path),
            status="idle", tmux_session="oc-1", tool="opencode",
        )
        res = await sessions_api.restart_all(_request(conn), sessions_api.RestartAllRequest(tool="pi"))
        assert res["restarted"] == [] and res["skipped"] == []
        res = await sessions_api.restart_all(_request(conn), sessions_api.RestartAllRequest(tool="opencode"))
        assert [s["session_id"] for s in res["restarted"]] == [SID]

    _run(check)
    assert _read_log(opencode) == [str(tmp_path), "title=", "--standalone", "-s", SID]


@needs_tmux
@pytest.mark.parametrize("always, keys", [(False, b"\r"), (True, b"\x1b[C\r")])
def test_opencode_prompt_waits_and_approve_presses_its_buttons(tmp_path, private_tmux, monkeypatch, always, keys):
    # An approval sent in another test must not hide this prompt.
    monkeypatch.setattr(session_manager, "_APPROVED_SUPPRESS", {})
    # The pane shows a permission prompt, then records the keys it gets.
    keys_log = tmp_path / "keys.log"
    script = tmp_path / "prompt.sh"
    script.write_text(
        f"printf '%s' '{SHELL_PROMPT_WIDE}'\n"
        f"stty raw -echo\ndd bs=1 count={len(keys)} of='{keys_log}' 2>/dev/null\nsleep 600\n"
    )
    subprocess.run(["tmux", "new-session", "-d", "-s", "oc-1", "-x", "160", "-y", "30",
                    f"sh {script}"], check=True)
    time.sleep(0.5)

    async def check(conn):
        base = {"session_id": SID, "cwd": str(tmp_path), "_tool": "opencode"}
        await event_processor.process_event(
            conn, {**base, "hook_event_name": "SessionStart", "_tmux_session": "oc-1"}, "h", "host")
        await session_manager.parse_one(conn, SID)
        row = await db.get_session(conn, SID)
        assert (row["pending_tool"], row["pending_detail"]) == ("Bash", "rm -rf build")
        assert row["pending_always_label"] == "Allow always in this project"
        res = await sessions_api.approve_session(_request(conn), SID, always=always)
        assert res["ok"]

    _run(check)
    assert _read_bytes(keys_log, len(keys)) == keys


@needs_tmux
def test_opencode_esc_presses_escape_twice(tmp_path, private_tmux, monkeypatch):
    # opencode stops a turn on the second Escape only.
    keys_log = tmp_path / "keys.log"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "opencode"
    script.write_text(f"#!/bin/sh\nstty raw -echo\ndd bs=1 count=2 of='{keys_log}' 2>/dev/null\nsleep 600\n")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    subprocess.run(["tmux", "new-session", "-d", "-s", "oc-1", "opencode"], check=True)
    time.sleep(0.5)

    async def check(conn):
        await db.upsert_session(
            conn, session_id=SID, hub_id="h", hostname="h", cwd=str(tmp_path),
            status="active", tmux_session="oc-1", tool="opencode",
        )
        res = await sessions_api.interrupt(_request(conn), SID)
        assert res == {"ok": True, "ignored": False}

    _run(check)
    assert _read_bytes(keys_log, 2) == b"\x1b\x1b"


@needs_tmux
def test_theme_notice_reaches_opencode_and_pi_panes_only(tmp_path, private_tmux, monkeypatch):
    # Panes that record what they are sent. Claude Code would read the
    # notice as typed text, so its pane must get nothing.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("opencode", "pi", "claude"):
        script = bin_dir / tool
        script.write_text(f"#!/bin/sh\nstty raw -echo\ndd bs=1 count=9 of='{tmp_path}/{tool}.keys' 2>/dev/null\nsleep 600\n")
        script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    rows = (("ses_oc", "oc-1", "opencode"), ("pi-sid", "pi-1", "pi"), ("cl-sid", "cl-1", "claude"))
    for _, name, tool in rows:
        subprocess.run(["tmux", "new-session", "-d", "-s", name, tool], check=True)
    time.sleep(0.5)

    async def check(conn):
        for sid, name, tool in rows:
            await db.upsert_session(
                conn, session_id=sid, hub_id="h", hostname="h", cwd=str(tmp_path),
                status="idle", tmux_session=name, tool=tool,
            )
        notice = sessions_api.theme_notice
        res = await notice(_request(conn), sessions_api.ThemeNotice(tone="light"))
        assert sorted(res["notified"]) == ["pi-sid", "ses_oc"]
        assert await notice(_request(conn), sessions_api.ThemeNotice(tone="dark", session_ids=["other"])) == {"notified": []}
        with pytest.raises(HTTPException):
            await notice(_request(conn), sessions_api.ThemeNotice(tone="dim"))

    _run(check)
    assert _read_bytes(tmp_path / "opencode.keys", 9) == b"\x1b[?997;2n"
    assert _read_bytes(tmp_path / "pi.keys", 9) == b"\x1b[?997;2n"
    time.sleep(0.3)
    assert (tmp_path / "claude.keys").read_bytes() == b""


# ── opencode's database ──────────────────────────────────────


def _write_opencode_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE session_v2 (id TEXT PRIMARY KEY, parent_id TEXT, title TEXT, model TEXT,"
        " tokens_input INTEGER, tokens_output INTEGER, tokens_reasoning INTEGER,"
        " tokens_cache_read INTEGER, tokens_cache_write INTEGER);"
        "CREATE TABLE session_message (id TEXT PRIMARY KEY, session_id TEXT, type TEXT, seq INTEGER,"
        " time_created INTEGER, time_updated INTEGER, data TEXT);"
    )
    conn.execute(
        "INSERT INTO session_v2 VALUES (?, NULL, 'Tests', ?, 360, 21, 0, 200, 5)",
        (SID, json.dumps({"id": "glm-5.3-flash", "providerID": "zhipu"})),
    )
    messages = [
        ("user", {"text": "run the tests", "files": []}),
        ("assistant", {"content": [
            {"type": "reasoning", "text": "Run pytest."},
            {"type": "tool", "id": "c1", "name": "shell", "state": {"status": "completed", "input": {"command": "pytest -q"}}},
            {"type": "tool", "id": "c2", "name": "write", "state": {"status": "completed", "input": {"path": "a.py", "content": "x"}}},
        ], "finish": "tool-calls"}),
        ("assistant", {"content": [{"type": "text", "text": "All 3 tests pass."}], "finish": "stop"}),
        ("idle", {"outcome": "succeeded"}),
        ("shell", {"command": "git status", "status": "exited"}),
        ("agent-switched", {"agent": "plan"}),
        ("idle", {"outcome": "interrupted"}),
        ("idle", {"outcome": "failed", "error": {"type": "provider", "message": "Rate limited"}}),
    ]
    for seq, (kind, data) in enumerate(messages):
        conn.execute(
            "INSERT INTO session_message VALUES (?, ?, ?, ?, ?, ?, ?)",
            (f"msg_{seq}", SID, kind, seq, 1792830669396 + seq, 1792830669396 + seq, json.dumps(data)),
        )
    conn.execute(
        "INSERT INTO session_message VALUES ('other', 'ses_other', 'user', 0, 1, 1, ?)",
        (json.dumps({"text": "another session"}),),
    )
    conn.commit()
    conn.close()


def test_opencode_messages_and_usage_come_from_its_database(tmp_path, monkeypatch):
    path = tmp_path / "opencode.db"
    _write_opencode_db(path)
    monkeypatch.setenv("OPENCODE_DB", str(path))
    session = {"session_id": SID, "tool": "opencode"}
    assert find_transcript(session, []) == str(path)

    messages = [(m["role"], m["text"]) for m in read_messages(str(path), "opencode", session_id=SID)]
    assert messages == [
        ("user", "run the tests"),
        ("tool", "$ pytest -q"),
        ("tool", "Write a.py"),
        ("assistant", "All 3 tests pass."),
        ("user", "! git status"),
        ("note", "Interrupted"),
        ("note", "Rate limited"),
    ]
    assert [m["text"] for m in read_messages(str(path), "opencode", limit=2, session_id=SID)] == [
        "Interrupted", "Rate limited",
    ]

    async def check(conn):
        await event_processor.process_event(conn, {
            "session_id": SID, "hook_event_name": "SessionStart", "cwd": str(tmp_path),
            "_tool": "opencode", "_tmux_session": "oc-1",
        }, "h", "host")
        row = await db.get_session(conn, SID)
        assert (row["tool"], row["model"]) == ("opencode", "glm-5.3-flash")
        assert (row["input_tokens"], row["output_tokens"], row["cache_read_tokens"], row["cache_create_tokens"]) == (360, 21, 200, 5)

    _run(check)


# ── The plugin ───────────────────────────────────────────────

# Runs the plugin's setup with a stand-in opencode: ctx.session.get knows
# the sessions in SESSIONS, ctx.event.subscribe yields the events in
# EVENTS, and an event {"title_request": id} asks the title hook for a
# title, as opencode does before titling a new session.
HARNESS = """
import { pathToFileURL } from "node:url";
const plugin = (await import(pathToFileURL(process.argv[2]).href)).default;
const { SESSIONS, EVENTS } = JSON.parse(process.env.STAND_IN);
const hooks = {};
const ctx = {
  location: { directory: "/home/u/project" },
  session: {
    get: async ({ sessionID }) => {
      if (!SESSIONS[sessionID]) throw new Error("not found");
      return SESSIONS[sessionID];
    },
    hook: async (name, fn) => { hooks[name] = fn; return { dispose() {} }; },
  },
  event: {
    subscribe: () => (async function* () {
      for (const event of EVENTS) {
        if (event.title_request) {
          const request = { sessionID: event.title_request };
          if (hooks.title) await hooks.title(request);
          yield { type: "session.renamed", data: { sessionID: event.title_request, title: request.result || "Generated" } };
        } else {
          yield event;
        }
      }
    })(),
  },
};
const cleanup = await plugin.setup(ctx);
await new Promise((resolve) => setTimeout(resolve, 1000));
if (cleanup) cleanup();
"""

# Starts the harness as the child of a process whose command line is the
# terminal UI's, `opencode <args>`, as opencode starts its private server.
PARENT = """
import { spawnSync } from "node:child_process";
const [harness, plugin, ...serverArgs] = JSON.parse(process.env.CHILD);
spawnSync(process.execPath, [harness, plugin, ...serverArgs], { stdio: "inherit" });
"""

CREATED = {"type": "session.created", "location": {"directory": "/home/u/project"},
           "data": {"sessionID": "ses_1", "location": {"directory": "/home/u/project"}, "slug": "x",
                    "model": {"id": "glm-5.3-flash", "providerID": "zhipu"}}}
TURN = [
    {"type": "session.inbox.enqueued", "data": {"sessionID": "ses_1", "item": {"type": "user", "payload": {"text": "run the tests"}}}},
    {"type": "session.execution.started", "data": {"sessionID": "ses_1"}},
    {"title_request": "ses_1"},
    {"type": "session.tool.input.started", "data": {"sessionID": "ses_1", "id": "c1", "name": "shell"}},
    {"type": "session.tool.called", "data": {"sessionID": "ses_1", "id": "c1", "input": {"command": "pytest -q", "description": "Run"}}},
    {"type": "session.tool.input.started", "data": {"sessionID": "ses_1", "id": "c2", "name": "write"}},
    {"type": "session.tool.called", "data": {"sessionID": "ses_1", "id": "c2", "input": {"path": "a.py", "content": "secret"}}},
    {"type": "session.tool.success", "data": {"sessionID": "ses_1", "id": "c1"}},
    {"type": "session.tool.failed", "data": {"sessionID": "ses_1", "id": "c2"}},
    # A subagent's session is not reported.
    {"type": "session.created", "data": {"sessionID": "ses_sub", "parentID": "ses_1", "slug": "y"}},
    {"type": "session.inbox.enqueued", "data": {"sessionID": "ses_sub", "item": {"type": "user", "payload": {"text": "sub"}}}},
    {"type": "session.execution.succeeded", "data": {"sessionID": "ses_sub"}},
    {"type": "session.text.ended", "data": {"sessionID": "ses_1", "text": "All pass."}},
    {"type": "session.execution.succeeded", "data": {"sessionID": "ses_1"}},
]


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


def _run_plugin(tmp_path, url, ui_args, events, sessions=None, server_args=("serve", "--stdio"),
                in_tmux=True, title=""):
    harness = tmp_path / "harness.mjs"
    harness.write_text(HARNESS)
    parent = tmp_path / "parent.mjs"
    parent.write_text(PARENT)
    fake = tmp_path / "fakebin"
    fake.mkdir(exist_ok=True)
    (fake / "tmux").write_text("#!/bin/sh\necho oc-1\n")
    (fake / "tmux").chmod(0o755)
    env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE", "AGENT_HUB_TITLE")}
    env.update(
        PATH=f"{fake}{os.pathsep}{os.environ['PATH']}", AGENT_HUB_URL=url,
        STAND_IN=json.dumps({"SESSIONS": sessions or {}, "EVENTS": events}),
        CHILD=json.dumps([str(harness), str(PLUGIN), *server_args]),
    )
    if in_tmux:
        env.update(TMUX="/tmp/tmux-test/default,1,0", TMUX_PANE="%1")
    if title:
        env["AGENT_HUB_TITLE"] = title
    # `exec -a` gives the parent the command line `opencode <ui_args>`;
    # node reads its script from stdin ("-").
    subprocess.run(
        ["bash", "-c", 'exec -a opencode node --input-type=module - "$@" < "$PARENT_JS"', "_", *ui_args],
        env={**env, "PARENT_JS": str(parent)}, check=True, timeout=30, capture_output=True,
    )


def _events(posts):
    out = []
    for path, body in posts:
        if path.startswith("/api/events?"):
            assert "tool=opencode" in path and "tmux_session=oc-1" in path
            assert body["cwd"] == "/home/u/project"
            fields = {k: body[k] for k in ("prompt", "tool_name", "tool_input", "last_assistant_message", "model", "source") if k in body}
            out.append((body["hook_event_name"], body["session_id"], fields))
        else:
            out.append((path, body))
    return out


@needs_node
def test_plugin_reports_opencode_events_like_hooks(tmp_path, hub):
    url, posts = hub
    _run_plugin(tmp_path, url, ["--standalone"], [CREATED, *TURN], title="Hub name")
    assert _events(posts) == [
        ("SessionStart", "ses_1", {"source": "startup", "model": "glm-5.3-flash"}),
        ("UserPromptSubmit", "ses_1", {"prompt": "run the tests"}),
        # The Hub's name becomes opencode's title for the session.
        ("/api/sessions/ses_1/title", {"title": "Hub name"}),
        ("PreToolUse", "ses_1", {"tool_name": "Bash", "tool_input": {"command": "pytest -q"}}),
        # File contents never leave opencode.
        ("PreToolUse", "ses_1", {"tool_name": "Write", "tool_input": {"file_path": "a.py"}}),
        ("PostToolUse", "ses_1", {"tool_name": "Bash", "tool_input": {"command": "pytest -q"}}),
        ("PostToolUseFailure", "ses_1", {"tool_name": "Write", "tool_input": {"file_path": "a.py"}}),
        ("Stop", "ses_1", {"last_assistant_message": "All pass."}),
    ]


@needs_node
def test_plugin_reports_a_session_opened_with_s(tmp_path, hub):
    url, posts = hub
    sessions = {"ses_1": {"id": "ses_1", "title": "Earlier title", "model": {"id": "glm-5.3-flash"}}}
    _run_plugin(tmp_path, url, ["--standalone", "-s", "ses_1"], TURN, sessions=sessions, title="Hub name")
    events = _events(posts)
    assert events[:3] == [
        ("SessionStart", "ses_1", {"source": "resume", "model": "glm-5.3-flash"}),
        ("/api/sessions/ses_1/title", {"title": "Earlier title"}),
        ("UserPromptSubmit", "ses_1", {"prompt": "run the tests"}),
    ]
    # A reopened session keeps its title: the Hub's name is not applied.
    assert ("/api/sessions/ses_1/title", {"title": "Generated"}) in events
    assert all(body.get("title") != "Hub name" for path, body in posts)


@needs_node
@pytest.mark.parametrize("ui_args, server_args, in_tmux", [
    (["run", "--standalone", "hello"], ("serve", "--stdio"), True),   # opencode run
    (["acp"], ("serve", "--stdio"), True),                            # opencode acp
    (["--standalone"], ("serve", "--service"), True),                 # the background service
    (["--standalone"], ("serve", "--stdio"), False),                  # outside tmux
])
def test_plugin_is_silent_unless_a_terminal_ui_in_tmux_runs_it(tmp_path, hub, ui_args, server_args, in_tmux):
    url, posts = hub
    _run_plugin(tmp_path, url, ui_args, [CREATED, *TURN], server_args=server_args, in_tmux=in_tmux)
    assert posts == []
