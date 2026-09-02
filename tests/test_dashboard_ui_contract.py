from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BASE = (ROOT / "src/agent_hub/web/templates/base.html").read_text()
DASHBOARD = (ROOT / "src/agent_hub/web/templates/dashboard.html").read_text()
SESSION_ROW = (ROOT / "src/agent_hub/web/templates/_session_card.html").read_text()


def test_theme_catalog_has_five_dark_and_five_light_palettes() -> None:
    entries = re.findall(
        r"\{id:'([^']+)', name:'([^']+)', tone:'(dark|light)'",
        BASE,
    )

    assert len(entries) == 10
    assert len({theme_id for theme_id, _, _ in entries}) == 10
    assert sum(tone == "dark" for _, _, tone in entries) == 5
    assert sum(tone == "light" for _, _, tone in entries) == 5


def test_terminal_embed_url_carries_theme_and_parent_origin() -> None:
    assert "embed: '1'" in DASHBOARD
    assert "theme, parent_origin: location.origin" in DASHBOARD
    assert "agent-hub:set-theme" in DASHBOARD
    assert "webterminal:connected" in DASHBOARD
    assert "webterminal:disconnected" in DASHBOARD
    assert "event.source !== iframe.contentWindow" in DASHBOARD
    assert "event.data.session !== previewState.tmux" in DASHBOARD


def test_close_workspace_tab_is_view_only() -> None:
    match = re.search(
        r"function closeWorkspaceTab\(sid\) \{(?P<body>.*?)\n\}",
        DASHBOARD,
        re.DOTALL,
    )

    assert match is not None
    body = match.group("body")
    assert "fetch(" not in body
    assert "/api/" not in body
    assert "kill" not in body.lower()
    assert "delete" not in body.lower()


def test_mobile_terminal_mode_and_dense_session_hooks_exist() -> None:
    assert 'data-mobile-mode="terminal"' in DASHBOARD
    assert "function setMobileMode(mode)" in DASHBOARD
    assert "workspace-tabs" in DASHBOARD
    assert "needs-list" in DASHBOARD
    assert "function syncTerminalVisibility()" in DASHBOARD
    assert "mobileViewport.addEventListener?.('change'" in DASHBOARD
    assert "function handleWorkspaceTabKey(event, sid)" in DASHBOARD
    assert "event.key === 'Delete'" in DASHBOARD
    assert "main.dataset.sid = sid" in DASHBOARD
    assert "tab.dataset.sid === targetSid" in DASHBOARD
    assert 'data-role="state-label"' in SESSION_ROW
    assert 'data-role="session-detail"' in SESSION_ROW
