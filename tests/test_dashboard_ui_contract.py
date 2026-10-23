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


def test_rows_follow_sessions_the_websocket_never_reports() -> None:
    # The dead-tmux sweep and deletes elsewhere send nothing over the
    # WebSocket; the page checks its live rows against the Hub instead.
    assert "async function reconcileRows()" in WORKSPACE_JS
    assert "setInterval(reconcileRows, 15000)" in WORKSPACE_JS
    assert "scheduleTmuxRefresh(); reconcileRows(); }" in WORKSPACE_JS
    assert "if (/not found/i.test(e.message)) dropRow(sid);" in WORKSPACE_JS


def test_sidebar_can_be_hidden_on_desktop() -> None:
    # The top bar's toggle hides the sidebar; the choice survives a reload
    # (applied before the first paint) and Ctrl+K brings the sidebar back.
    assert 'id="sidebar-toggle" data-action="toggle-sidebar"' in DASHBOARD
    assert "localStorage.getItem('agentHubSidebar') === 'hidden'" in DASHBOARD
    assert "case 'toggle-sidebar': setSidebarHidden(!sidebarHidden()); break;" in WORKSPACE_JS
    assert "else if (sidebarHidden()) setSidebarHidden(false);" in WORKSPACE_JS
    # Phones keep their own sessions screen.
    assert "@media (min-width: 700px) {\n  .shell.is-sidebar-hidden" in HUB_CSS


def test_sessions_can_be_restarted_one_by_one_or_per_tool() -> None:
    # A running row's hover buttons and its details restart that session;
    # the More menu restarts every running Claude or Codex session.
    assert 'data-action="restart" title="Restart the CLI' in SESSION_ROW
    assert "status != 'stopped'" in SESSION_ROW.split('data-action="restart"')[0].rsplit("{% if", 1)[1]
    assert 'data-action="restart-all" data-tool="claude"' in DASHBOARD
    assert 'data-action="restart-all" data-tool="codex"' in DASHBOARD
    assert "case 'restart': restartSession(sid, btn); break;" in WORKSPACE_JS
    assert "case 'restart-all': restartAll(btn.dataset.tool); break;" in WORKSPACE_JS
    # A session in the middle of a turn needs a second confirmation.
    assert "`/api/sessions/${sid}/restart?force=true`" in WORKSPACE_JS
    assert "api('POST', '/api/restart-sessions', {tool: family})" in WORKSPACE_JS


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


PHONE = (WEB / "templates/mobile.html").read_text()
PHONE_JS = (WEB / "static/mobile.js").read_text()


def test_phone_page_follows_the_stream_not_the_websocket() -> None:
    # /m gets session states from GET /api/stream and opens no WebSocket.
    assert 'data-ws="off"' in PHONE
    assert "new EventSource('/api/stream')" in PHONE_JS
    for kind in ("snapshot", "session", "removed", "notice"):
        assert f"addEventListener('{kind}'" in PHONE_JS
    assert "WebSocket" not in PHONE_JS
    # The on-screen keyboard shrinks the page so the reply box stays visible.
    assert "interactive-widget=resizes-content" in PHONE


def test_phone_page_answers_through_the_session_endpoints() -> None:
    for path in ("'/send'", "'/interrupt'", "'/approve'", "'/approve?always=true'", "/seen`", "/messages?limit="):
        assert path in PHONE_JS
    # Stop asks first; Deny and Approve are one tap on a visible card.
    assert "case 'stop'" in PHONE_JS and "H.confirmDialog(" in PHONE_JS
    assert "confirm(" not in PHONE_JS.replace("confirmDialog(", "")
    assert "alert(" not in PHONE_JS


def test_phone_page_escapes_text_and_shares_the_reply_formatter() -> None:
    # Replies go through hub.js's escaping formatter, the same one the
    # Monitor page uses; everything else is escaped or set as text.
    assert "function renderMarkdown(text)" in HUB_JS
    assert "H.renderMarkdown(" in PHONE_JS
    assert "H.renderMarkdown(content)" in (WEB / "templates/agent.html").read_text()
    assert "${esc(item.text)}" in PHONE_JS


def test_phone_page_exposes_back_and_open_for_the_app() -> None:
    # The Android app's Back key and notification taps drive these.
    assert "window.AgentHubMobile = {back, openSession}" in PHONE_JS
    assert "#\\/s\\/" in PHONE_JS


def test_phone_page_is_served() -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from agent_hub.config import HubConfig
    from agent_hub.web import routes

    app = FastAPI()
    app.state.config = HubConfig(hub_id="t", terminal_port=7700)
    app.include_router(routes.router)
    res = TestClient(app).get("/m", headers={"host": "hub.example:7800"})
    assert res.status_code == 200
    assert 'id="m-home"' in res.text and 'id="m-session"' in res.text
    assert "/static/mobile.js" in res.text
    # The Terminal view embeds the Web Terminal on the host the page was opened on.
    assert 'data-terminal-url="http://hub.example:7700"' in res.text


def test_phone_session_opens_on_the_native_tui() -> None:
    # A session opens on its Terminal (the Web Terminal attached to its tmux,
    # embedded like the workspace's, but without touch keys); Text is the switch.
    assert "localStorage.getItem(MODE_KEY) === 'text' ? 'text' : 'terminal'" in PHONE_JS
    assert "embed: '1', theme: H.currentTheme(), parent_origin: location.origin, keys: 'off'" in PHONE_JS
    assert 'data-mode="terminal"' in PHONE and 'data-mode="text"' in PHONE
    assert 'id="m-stage"' in PHONE
    # Only the frame the page created is listened to, and it hears when it is hidden.
    assert "event.source !== terminal.iframe.contentWindow" in PHONE_JS
    assert "type: 'agent-hub:visibility'" in PHONE_JS
    # Leaving a session detaches its tmux client.
    assert "detachTerminal();\n      openSid = sid;" in PHONE_JS


def test_phone_terminal_view_has_esc_instead_of_touch_keys() -> None:
    # The phone's own keyboard types into the TUI; the only extra key is Esc
    # in the reply bar, sent through the Hub's interrupt (2 s double-press
    # guard) without taking focus, so the phone keyboard stays open.
    assert 'id="m-esc"' in PHONE
    assert '#m-session[data-view="text"] .m-esc' in HUB_CSS
    assert "act(escButton, () => H.api('POST', sessionUrl('/interrupt')))" in PHONE_JS
    assert "escButton.addEventListener('touchstart', e => e.preventDefault(), {passive: false});" in PHONE_JS
    assert "escButton.addEventListener('mousedown', e => e.preventDefault());" in PHONE_JS


def test_phone_page_reloads_when_the_hub_serves_a_newer_one() -> None:
    # The page lives on the Hub and changes without app updates; an open one
    # compares its version with the stream's snapshot after every reconnect.
    stream = (ROOT / "src/agent_hub/api/stream.py").read_text()
    assert 'snapshot["page_version"] = _static_version()' in stream
    assert "data.page_version !== PAGE_VERSION) reloadWhenIdle()" in PHONE_JS
