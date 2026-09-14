"""Bare-codex launcher, resume hints and the permission_mode → tool marker."""
import asyncio

from agent_hub import db
from agent_hub.api.tmux import (
    _ALLOWED_COMMANDS,
    _COMMAND_ARGS,
    _COMMAND_BIN,
    _NAME_PREFIX,
)
from agent_hub.services import session_manager
from agent_hub.services.restore import (
    _resume_hint_command,
    _resume_pane_command,
    _shell_pane_command,
    resume_argv,
)

UUID = "01a0656c-8b56-72b2-802f-a69ba1be7257"
BYPASS = "--dangerously-bypass-approvals-and-sandbox"


def test_launcher_runs_plain_claude_and_bare_codex():
    assert _ALLOWED_COMMANDS == {"claude", "codex-yolo"}
    assert _COMMAND_BIN["claude"] == "claude"
    assert _COMMAND_ARGS["claude"] == []
    assert _NAME_PREFIX["claude"] == "claude"
    assert _COMMAND_BIN["codex-yolo"] == "codex"
    assert _COMMAND_ARGS["codex-yolo"] == [BYPASS]
    assert _NAME_PREFIX["codex-yolo"] == "codex"


def test_claude_yolo_hint_keeps_flag():
    assert _resume_hint_command("claude-yolo", UUID) == (
        f"claude --resume {UUID} --dangerously-skip-permissions"
    )


def test_codex_yolo_hint_uses_real_id():
    assert _resume_hint_command("codex-yolo", UUID) == f"codex resume {UUID} {BYPASS}"


def test_codex_hint_without_yolo():
    assert _resume_hint_command("codex", UUID) == f"codex resume {UUID}"


def test_codex_synthetic_id_falls_back_to_picker():
    assert _resume_hint_command("codex-yolo", "codex-mytmux-1788407313") == f"codex resume {BYPASS}"


def test_legacy_omx_yolo_maps_to_bare_codex():
    assert _resume_hint_command("omx-yolo", UUID) == f"codex resume {UUID} {BYPASS}"


def _ensure(tool: str, payload: dict) -> str:
    async def run():
        conn = await db.init_db(":memory:")
        try:
            await session_manager.ensure_session(
                conn,
                session_id=UUID,
                hub_id="test-hub",
                hostname="test-host",
                cwd="/tmp",
                tmux_session="codex-test-1",
                payload={"hook_event_name": "SessionStart", **payload},
                tool=tool,
            )
            row = await db.get_session(conn, UUID)
            return row["tool"]
        finally:
            await conn.close()

    return asyncio.run(run())


def test_bypass_permissions_marks_codex_yolo():
    assert _ensure("codex", {"permission_mode": "bypassPermissions"}) == "codex-yolo"


def test_default_permissions_keep_plain_tool():
    assert _ensure("codex", {"permission_mode": "default"}) == "codex"


def test_claude_keeps_plain_tool_whatever_the_permission_mode():
    # Claude's permission mode comes from the user's settings; the Hub
    # never turns it into a flag.
    assert _ensure("claude", {"permission_mode": "bypassPermissions"}) == "claude"


def test_plain_claude_resume_has_no_flag():
    assert _resume_hint_command("claude", UUID) == f"claude --resume {UUID}"


def test_resume_argv_matches_hint():
    for tool, sid in (("claude-yolo", UUID), ("claude-sonnet", UUID), ("codex-yolo", UUID), ("codex", "codex-x-1"), ("omx-yolo", UUID)):
        argv = resume_argv(tool, sid)
        assert argv is not None
        assert " ".join(argv) == _resume_hint_command(tool, sid)
    assert resume_argv("something-else", UUID) is None


def test_resume_pane_command_keeps_argv_intact_and_falls_back_to_a_shell():
    cmd = _resume_pane_command(["/opt/bin/claude", "--resume", UUID, "--dangerously-skip-permissions"])
    assert cmd[:2] == ["bash", "-c"]
    assert 'exec "${SHELL:-bash}" -i' in cmd[2]
    # positional parameters: $1 is the printable command, the rest is argv
    assert cmd[3] == "_"
    assert cmd[4] == f"/opt/bin/claude --resume {UUID} --dangerously-skip-permissions"
    assert cmd[5:] == ["/opt/bin/claude", "--resume", UUID, "--dangerously-skip-permissions"]


def test_shell_pane_command_prints_the_hint():
    cmd = _shell_pane_command("claude-yolo", UUID)
    assert cmd[:2] == ["bash", "-c"]
    assert f"claude --resume {UUID} --dangerously-skip-permissions" in cmd[2]
    assert "exec ${SHELL:-bash} -i" in cmd[2]
