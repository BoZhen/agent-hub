"""Regression tests for Hub-launched tmux command resolution.

Run with:
    uv run python tests/test_tmux_command_resolver.py
"""
from __future__ import annotations

import os
import stat
import sys
import tempfile
from pathlib import Path

from agent_hub.api.tmux import _resolve_command


def _touch_executable(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _run() -> int:
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        claude = home / ".nvm" / "versions" / "node" / "v24.12.0" / "bin" / "claude"
        _touch_executable(claude)
        got = _resolve_command("claude", path_env=os.defpath, home=home)
        if got != str(claude):
            print("FAIL nvm fallback")
            print(f"  expected: {claude}")
            print(f"  got:      {got}")
            failures += 1
        else:
            print(f"OK   nvm fallback: {got}")

    if failures:
        print(f"\n{failures} failure(s)")
        return 1
    print("\n1 resolver check passed")
    return 0


if __name__ == "__main__":
    sys.exit(_run())
