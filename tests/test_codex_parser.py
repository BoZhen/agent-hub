"""Regression tests for the Codex approval parser.

Run with:
    uv run python tests/test_codex_parser.py

No pytest dependency — each fixture is a tmux pane capture saved
under `tests/fixtures/`, and the test asserts the expected parse
output. Add new fixtures when a real-world pane triggers a parser
edge case the current fixture set doesn't cover.
"""
from __future__ import annotations

import sys
from pathlib import Path

from agent_hub.services.session_manager import _parse_codex_approval_prompt

_FIXTURES_DIR = Path(__file__).parent / "fixtures"


# (fixture_filename, expected_parse_output)
# expected is None for an idle pane (no approval), otherwise a
# (tool_name, detail, always_label) tuple.
CASES: list[tuple[str, tuple[str, str, str | None] | None]] = [
    # Narrow-terminal MCP approval where the title wraps across two
    # lines ("Allow the omx_memory MCP server to run" / "tool ..."),
    # breaking the original single-line question-phrase match. Also
    # guards against the detail extractor's multi-space join bug
    # that used to drop the server name.
    (
        "codex-mcp-approval-wrapped.txt",
        ("MCP", "omx_memory: project_memory_read", "Always allow"),
    ),
    # Sandbox-retry / Edit approval — codex tried to write a file,
    # the sandbox blocked it, and codex is asking permission to
    # retry without sandbox. 3-option UI ("Yes, proceed" / "Yes,
    # and don't ask again for these files" / "No"), Always is
    # option 2 (Down + Enter, same as Bash).
    (
        "codex-edit-approval.txt",
        (
            "Edit",
            "command failed; retry without sandbox?",
            "Yes, and don't ask again for these files",
        ),
    ),
    # Bash approval with a long python heredoc body — the command
    # block is ~25 lines, pushing the title 29 lines above the
    # selector (beyond the original 15-line search window). Also
    # exercises the "pick the *closest* title above the selector"
    # path: an earlier approved block is still visible higher in
    # the capture, and iterating top-down would misattribute. The
    # detail is the joined first few continuation lines of the
    # heredoc. 2-option UI, so always_label is None.
    (
        "codex-bash-approval-long-heredoc.txt",
        (
            "Bash",
            "python3 - <<'PY' from pathlib import Path from datetime import datetime import shutil",
            None,
        ),
    ),
    # Generic / unknown UI variant — structural signals match
    # (selector + option 2 + footer) but no title phrase in the
    # classification table matches. The parser must NOT drop this
    # on the floor; it should classify as generic "Codex" and
    # surface a best-effort detail (nearest `$ ` line above the
    # selector). Always label is None because generic codex
    # approvals don't know which option holds Always.
    (
        "codex-generic-approval-unknown-ui.txt",
        (
            "Codex",
            "echo synthetic-command-body",
            None,
        ),
    ),
]


def _run() -> int:
    failures = 0
    for filename, expected in CASES:
        path = _FIXTURES_DIR / filename
        if not path.exists():
            print(f"FAIL {filename}: fixture missing at {path}")
            failures += 1
            continue
        pane = path.read_text()
        got = _parse_codex_approval_prompt(pane)
        if got != expected:
            print(f"FAIL {filename}")
            print(f"  expected: {expected}")
            print(f"  got:      {got}")
            failures += 1
        else:
            print(f"OK   {filename}: {got}")
    if failures:
        print(f"\n{failures} failure(s)")
        return 1
    print(f"\n{len(CASES)} fixture(s) passed")
    return 0


if __name__ == "__main__":
    sys.exit(_run())
