from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "src/agent_hub/web"
BASE = (WEB / "templates/base.html").read_text()
DASHBOARD = (WEB / "templates/dashboard.html").read_text()
SESSION_ROW = (WEB / "templates/_session_card.html").read_text()
HUB_JS = (WEB / "static/hub.js").read_text()
WORKSPACE_JS = (WEB / "static/workspace.js").read_text()
HUB_CSS = (WEB / "static/hub.css").read_text()


def test_theme_catalog_has_five_dark_and_five_light_palettes() -> None:
    entries = re.findall(
        r"\{id:'([^']+)', name:'([^']+)', tone:'(dark|light)'",
        HUB_JS,
    )

    assert len(entries) == 10
    assert len({theme_id for theme_id, _, _ in entries}) == 10
    assert sum(tone == "dark" for _, _, tone in entries) == 5
    assert sum(tone == "light" for _, _, tone in entries) == 5
    # Every theme the picker offers has a token block in the stylesheet.
    for theme_id, _, _ in entries:
        if theme_id != "graphite":
            assert f':root[data-theme="{theme_id}"]' in HUB_CSS


def test_no_utility_framework_or_cdn_in_the_shell() -> None:
    assert "tailwindcss" not in BASE
    assert "cdn." not in BASE
    assert "/static/hub.css" in BASE


def test_terminal_embed_url_carries_theme_and_parent_origin() -> None:
    assert "embed: '1'" in WORKSPACE_JS
    assert "theme, parent_origin: location.origin" in WORKSPACE_JS
    assert "agent-hub:set-theme" in WORKSPACE_JS
    assert "webterminal:connected" in WORKSPACE_JS
    assert "webterminal:disconnected" in WORKSPACE_JS
    # only windows the Hub created itself are listened to; each frame is
    # matched by its own contentWindow
    assert "frameForWindow(event.source)" in WORKSPACE_JS
    assert "if (name === previewState.tmux) setStageConn(frame.state)" in WORKSPACE_JS
    assert "const MAX_LIVE_FRAMES" in WORKSPACE_JS


def test_ended_rows_offer_resume() -> None:
    assert 'data-action="resume"' in SESSION_ROW
    assert "/api/sessions/${sid}/resume" in WORKSPACE_JS
    assert "case 'resume': resumeSession(sid); break;" in WORKSPACE_JS


def test_close_workspace_tab_is_view_only() -> None:
    match = re.search(
        r"function closeWorkspaceTab\(sid\) \{(?P<body>.*?)\n\}",
        WORKSPACE_JS,
        re.DOTALL,
    )

    assert match is not None
    body = match.group("body")
    assert "fetch(" not in body
    assert "api(" not in body
    assert "/api/" not in body
    assert "kill" not in body.lower()
    assert "delete" not in body.lower()


def test_mobile_terminal_mode_and_dense_session_hooks_exist() -> None:
    assert 'body[data-mobile-mode="terminal"]' in HUB_CSS
    assert "function setMobileMode(mode)" in WORKSPACE_JS
    assert "function syncTerminalVisibility()" in WORKSPACE_JS
    assert "mobileViewport.addEventListener?.('change'" in WORKSPACE_JS
    assert "function handleWorkspaceTabKey(event, sid)" in WORKSPACE_JS
    assert "event.key === 'Delete'" in WORKSPACE_JS
    assert "main.dataset.sid = sid" in WORKSPACE_JS
    assert "tab.dataset.sid === targetSid" in WORKSPACE_JS
    assert 'id="workspace-tabs"' in DASHBOARD
    assert 'id="needs-list"' in DASHBOARD
    assert 'data-role="state-label"' in SESSION_ROW
    assert 'data-role="session-detail"' in SESSION_ROW
    assert 'data-role="event-line"' in SESSION_ROW


def test_destructive_actions_are_separate_from_closing_a_view() -> None:
    # Kill tmux and Delete record go through confirm dialogs and distinct
    # endpoints; closing a tab never touches either.
    assert "'/api/tmux/kill'" in WORKSPACE_JS
    assert "api('DELETE', `/api/sessions/${sid}`)" in WORKSPACE_JS
    assert WORKSPACE_JS.count("H.confirmDialog(") >= 4
    assert "confirm(" not in WORKSPACE_JS.replace("confirmDialog(", "")
    assert "alert(" not in WORKSPACE_JS
