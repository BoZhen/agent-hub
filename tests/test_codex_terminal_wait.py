"""A Codex pane the Hub starts waits for a terminal before Codex runs.

Codex asks the terminal for its background colour once, at startup, and
shades your prompts only if it gets an answer. tmux answers only while a
terminal is attached, so the pane asks first and starts Codex once a
terminal answers.

Runs against a private tmux server (TMUX_TMPDIR in a temp directory, $TMUX
unset). The terminal is a pty running `tmux attach` that answers tmux's
colour queries, as xterm.js does. The "codex" asks for the background colour
itself and writes down the answer.
"""
import os
import pty
import shutil
import struct
import subprocess
import sys
import termios
import threading
import time
import fcntl

import pytest

from agent_hub.api import tmux as tmux_api
from agent_hub.services.restore import _resume_pane_command

pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None or shutil.which("bash") is None, reason="tmux and bash needed"
)

BG = b"rgb:1212/3434/5656"

CODEX = r"""
import os, select, sys, termios, time, tty
out = sys.argv[1]
open(out + ".started", "w").close()
fd = sys.stdin.fileno(); old = termios.tcgetattr(fd); tty.setraw(fd)
os.write(1, b"\033]11;?\033\\")
reply, end = b"", time.time() + 0.25
while time.time() < end and not reply.endswith(b"\033\\"):
    if select.select([fd], [], [], 0.05)[0]:
        reply += os.read(fd, 256)
termios.tcsetattr(fd, termios.TCSADRAIN, old)
open(out, "wb").write(reply)
time.sleep(30)
"""


@pytest.fixture
def private_tmux(tmp_path, monkeypatch):
    tmux_dir = tmp_path / "tmux"
    tmux_dir.mkdir()
    monkeypatch.setenv("TMUX_TMPDIR", str(tmux_dir))
    monkeypatch.delenv("TMUX", raising=False)
    yield
    subprocess.run(["tmux", "kill-server"], capture_output=True)


@pytest.fixture
def codex(tmp_path):
    script = tmp_path / "codex.py"
    script.write_text(CODEX)
    out = tmp_path / "reply"
    return [sys.executable, str(script), str(out)], out


def _attach(name):
    """A terminal on `name` that answers colour queries. Returns its pid."""
    pid, fd = pty.fork()
    if pid == 0:
        fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
        os.execvpe("tmux", ["tmux", "attach", "-t", name], {**os.environ, "TERM": "xterm-256color"})

    def answer():
        while True:
            try:
                data = os.read(fd, 4096)
            except OSError:
                return
            if not data:
                return
            if b"\033]10;?" in data:
                os.write(fd, b"\033]10;rgb:eeee/eeee/eeee\033\\")
            if b"\033]11;?" in data:
                os.write(fd, b"\033]11;" + BG + b"\033\\")

    threading.Thread(target=answer, daemon=True).start()
    return pid


def _wait_for(path, seconds):
    end = time.time() + seconds
    while time.time() < end:
        if path.exists():
            return True
        time.sleep(0.1)
    return False


def _new_session(name, cwd, command):
    subprocess.run(["tmux", "new-session", "-d", "-s", name, "-x", "80", "-y", "24", "-c", str(cwd), *command], check=True)


def test_codex_starts_once_a_terminal_answers(private_tmux, codex, tmp_path):
    argv, out = codex
    _new_session("w", tmp_path, tmux_api.wait_for_terminal(argv))
    started = out.with_name("reply.started")
    assert not _wait_for(started, 1.5), "codex ran before any terminal was attached"
    client = _attach("w")
    try:
        assert _wait_for(out, 5)
        assert BG in out.read_bytes()
    finally:
        os.kill(client, 9)
        os.waitpid(client, 0)


def test_codex_starts_without_a_terminal_after_the_wait(private_tmux, codex, tmp_path, monkeypatch):
    monkeypatch.setattr(tmux_api, "CODEX_TERMINAL_WAIT_S", 1)
    argv, out = codex
    t0 = time.time()
    _new_session("w", tmp_path, tmux_api.wait_for_terminal(argv))
    assert _wait_for(out, 5)
    assert time.time() - t0 >= 0.9
    assert out.read_bytes() == b""  # no terminal, no colour: Codex draws without shading


def test_resume_waits_for_a_terminal_too(private_tmux, codex, tmp_path):
    argv, out = codex
    _new_session("w", tmp_path, _resume_pane_command(argv, wait_for_terminal=True))
    assert not _wait_for(out.with_name("reply.started"), 1.5)
    client = _attach("w")
    try:
        assert _wait_for(out, 5)
        assert BG in out.read_bytes()
    finally:
        os.kill(client, 9)
        os.waitpid(client, 0)


def test_only_codex_waits():
    cmd = _resume_pane_command(["/opt/bin/claude", "--resume", "x"])
    assert "11;?" not in cmd[2]
    cmd = _resume_pane_command(["/opt/bin/codex", "resume", "x"], wait_for_terminal=True)
    assert cmd[2].startswith(tmux_api.terminal_wait_sh())
    assert cmd[3:] == ["_", "/opt/bin/codex resume x", "/opt/bin/codex", "resume", "x"]
