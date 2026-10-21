"""Recent messages read from Claude and Codex transcripts
(services/conversation.py)."""
import json

from agent_hub.services import conversation
from agent_hub.services.conversation import find_transcript, read_messages


def _write(path, records):
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")
    return str(path)


CLAUDE = [
    {"type": "permission-mode", "permissionMode": "auto"},
    {"type": "user", "isMeta": True, "message": {"role": "user", "content": "<local-command-caveat>Caveat</local-command-caveat>"}},
    {"type": "user", "timestamp": "t1", "message": {"role": "user", "content": "fix the failing test"}},
    {"type": "assistant", "timestamp": "t2", "message": {"content": [{"type": "thinking", "thinking": "hmm"}]}},
    {"type": "assistant", "timestamp": "t3", "message": {"content": [{"type": "text", "text": "Running the suite."}]}},
    {"type": "assistant", "timestamp": "t4", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "pytest -q"}}]}},
    {"type": "user", "timestamp": "t5", "message": {"content": [{"type": "tool_result", "content": "1 failed"}]}},
    {"type": "assistant", "isSidechain": True, "message": {"content": [{"type": "text", "text": "sub-agent text"}]}},
    {"type": "user", "timestamp": "t6", "message": {"content": "<command-name>/compact</command-name>\n<command-args>keep tests</command-args>"}},
    {"type": "user", "timestamp": "t7", "message": {"content": "<local-command-stdout>Compacted</local-command-stdout>"}},
    {"type": "user", "timestamp": "t8", "message": {"content": [{"type": "text", "text": "[Request interrupted by user]"}]}},
    {"type": "user", "timestamp": "t9", "message": {"content": [{"type": "text", "text": "看这张图"}, {"type": "image", "source": {}}]}},
    {"type": "assistant", "timestamp": "t10", "message": {"content": [{"type": "text", "text": "Fixed. 12 passed."}]}},
]


def test_claude_messages(tmp_path):
    path = _write(tmp_path / "s.jsonl", CLAUDE)
    items = read_messages(path, "claude", limit=50)
    assert [(i["role"], i["text"]) for i in items] == [
        ("user", "fix the failing test"),
        ("assistant", "Running the suite."),
        ("tool", "$ pytest -q"),
        ("user", "/compact keep tests"),
        ("note", "Request interrupted by user"),
        ("user", "看这张图\n[image]"),
        ("assistant", "Fixed. 12 passed."),
    ]
    assert items[0]["ts"] == "t1"
    last_two = read_messages(path, "claude", limit=2)
    assert [i["text"] for i in last_two] == ["看这张图\n[image]", "Fixed. 12 passed."]


def _done(item):
    return {"type": "event_msg", "timestamp": "ts", "payload": {"type": "item_completed", "item": item}}


CODEX = [
    {"type": "session_meta", "payload": {"id": "x"}},
    {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "# AGENTS.md instructions ..."}]}},
    _done({"type": "UserMessage", "content": [{"type": "text", "text": "check the tun mode"}]}),
    _done({"type": "Reasoning", "summary_text": ["..."]}),
    _done({"type": "CommandExecution", "command": ["/usr/bin/bash", "-lc", "ip   addr show\nMeta"]}),
    _done({"type": "McpToolCall", "server": "docs", "tool": "search"}),
    _done({"type": "Extension", "kind": "web.search", "query": "mihomo dns"}),
    _done({"type": "AgentMessage", "content": [{"type": "Text", "text": "TUN is on."}], "phase": "final_answer"}),
    {"type": "event_msg", "payload": {"type": "task_complete", "last_agent_message": "TUN is on."}},
]


def test_codex_messages(tmp_path):
    path = _write(tmp_path / "rollout.jsonl", CODEX)
    items = read_messages(path, "codex", limit=50)
    assert [(i["role"], i["text"]) for i in items] == [
        ("user", "check the tun mode"),
        ("tool", "$ ip addr show Meta"),
        ("tool", "MCP docs.search"),
        ("tool", "WebSearch mihomo dns"),
        ("assistant", "TUN is on."),
    ]


def test_backwards_reader_across_small_blocks(tmp_path, monkeypatch):
    monkeypatch.setattr(conversation, "BLOCK", 40)
    records = [{"n": i, "pad": "y" * (i * 13 % 70)} for i in range(25)]
    path = _write(tmp_path / "r.jsonl", records)
    assert [r["n"] for r in conversation._records_backwards(path)] == list(range(24, -1, -1))


def test_find_transcript(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    stored = tmp_path / "stored.jsonl"
    stored.write_text("{}\n")
    assert find_transcript({"session_id": "x", "transcript_path": str(stored)}, []) == str(stored)

    sid = "0d4c43b3-5c0e-4e4b-9d3c-111111111111"
    claude_dir = tmp_path / ".claude-alt" / "projects" / "-w"
    claude_dir.mkdir(parents=True)
    (claude_dir / f"{sid}.jsonl").write_text("{}\n")
    found = find_transcript({"session_id": sid, "tool": "claude2"}, ["~/.claude", "~/.claude-alt"])
    assert found == str(claude_dir / f"{sid}.jsonl")

    codex_dir = tmp_path / ".codex" / "sessions" / "2026" / "10" / "21"
    codex_dir.mkdir(parents=True)
    rollout = codex_dir / f"rollout-2026-10-21T08-00-00-{sid}.jsonl"
    rollout.write_text("{}\n")
    assert find_transcript({"session_id": sid, "tool": "codex"}, []) == str(rollout)
    # A session known only from pane scanning has a made-up id.
    assert find_transcript({"session_id": "codex-w-1-1791384559", "tool": "codex"}, []) is None
