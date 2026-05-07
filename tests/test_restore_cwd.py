"""Tests for transcript cwd extraction used by restore-on-reboot.

Run with:
    uv run python tests/test_restore_cwd.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from agent_hub.services.restore import extract_cwd_from_transcript


def _write_jsonl(records: list[dict]) -> Path:
    f = tempfile.NamedTemporaryFile(
        mode="w", suffix=".jsonl", delete=False, encoding="utf-8"
    )
    for r in records:
        f.write(json.dumps(r) + "\n")
    f.flush()
    return Path(f.name)


def test_skips_metadata_records_finds_cwd() -> None:
    """Real Claude transcripts start with last-prompt / permission-mode
    records that lack `cwd`. The extractor must scan past them and
    return the first record that does carry the field."""
    path = _write_jsonl([
        {"type": "last-prompt", "sessionId": "x"},
        {"type": "permission-mode", "permissionMode": "default"},
        {"type": "attachment", "cwd": "/home/user/Git/agent-home"},
    ])
    try:
        assert (
            extract_cwd_from_transcript(str(path))
            == "/home/user/Git/agent-home"
        )
    finally:
        path.unlink()


def test_returns_none_when_no_cwd_in_window() -> None:
    """If `cwd` never appears within max_lines, return None — caller
    falls back to the DB column."""
    path = _write_jsonl([
        {"type": "last-prompt"} for _ in range(50)
    ])
    try:
        assert extract_cwd_from_transcript(str(path), max_lines=10) is None
    finally:
        path.unlink()


def test_handles_missing_file_silently() -> None:
    assert extract_cwd_from_transcript("/no/such/file.jsonl") is None


def test_handles_none_input() -> None:
    assert extract_cwd_from_transcript(None) is None


def test_skips_blank_and_malformed_lines() -> None:
    """Don't blow up on a half-flushed line; keep scanning."""
    f = tempfile.NamedTemporaryFile(
        mode="w", suffix=".jsonl", delete=False, encoding="utf-8"
    )
    f.write("\n")
    f.write("not-json-at-all\n")
    f.write(json.dumps({"type": "user", "cwd": "/tmp/proj"}) + "\n")
    f.flush()
    try:
        assert extract_cwd_from_transcript(f.name) == "/tmp/proj"
    finally:
        Path(f.name).unlink()


def test_ignores_non_string_cwd() -> None:
    path = _write_jsonl([
        {"type": "x", "cwd": 42},
        {"type": "y", "cwd": "/real/path"},
    ])
    try:
        assert extract_cwd_from_transcript(str(path)) == "/real/path"
    finally:
        path.unlink()


def main() -> None:
    tests = [
        test_skips_metadata_records_finds_cwd,
        test_returns_none_when_no_cwd_in_window,
        test_handles_missing_file_silently,
        test_handles_none_input,
        test_skips_blank_and_malformed_lines,
        test_ignores_non_string_cwd,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"ok   {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"ERR  {t.__name__}: {type(e).__name__}: {e}")
    if failed:
        print(f"\n{failed} failure(s)")
        sys.exit(1)
    print(f"\nall {len(tests)} tests passed")


if __name__ == "__main__":
    main()
