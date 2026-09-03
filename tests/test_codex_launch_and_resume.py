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
from agent_hub.services.restore import _resume_hint_command

UUID = "01a0656c-8b56-72b2-802f-a69ba1be7257"
BYPASS = "--dangerously-bypass-approvals-and-sandbox"


def test_launcher_runs_bare_codex():
    assert _ALLOWED_COMMANDS == {"claude-yolo", "codex-yolo"}
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
