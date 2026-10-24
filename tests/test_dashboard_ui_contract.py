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


def test_theme_catalog_has_six_dark_and_six_light_palettes() -> None:
    entries = re.findall(
        r"\{id:'([^']+)', name:'([^']+)', tone:'(dark|light)'",
        HUB_JS,
    )

    assert len(entries) == 12
    assert len({theme_id for theme_id, _, _ in entries}) == 12
    assert sum(tone == "dark" for _, _, tone in entries) == 6
    assert sum(tone == "light" for _, _, tone in entries) == 6
    # Every theme the picker offers has a token block in the stylesheet.
    for theme_id, _, _ in entries:
        if theme_id != "graphite":
            assert f':root[data-theme="{theme_id}"]' in HUB_CSS
        # The page applies a stored theme before the first paint.
        assert f"'{theme_id}'" in BASE


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
    assert "H.restartSession(sid, name)" in WORKSPACE_JS
    # A session in the middle of a turn needs a second confirmation.
    assert "await api('POST', `${url}?force=true`);" in HUB_JS
    assert "api('POST', '/api/restart-sessions', {tool: family})" in WORKSPACE_JS


def test_phone_session_screen_can_restart_its_cli() -> None:
    mobile_html = (WEB / "templates/mobile.html").read_text()
    mobile_js = (WEB / "static/mobile.js").read_text()
    # From the ⓘ sheet, which a running CLI with an id to resume offers it in.
    assert 'id="m-more" data-action="sheet"' in mobile_html and 'id="m-restart"' not in mobile_html
    assert "s && s.tmux_session && resumable ? ['restart', 'refresh', 'Restart'] : null" in mobile_js
    assert "if (await H.restartSession(sid, name)) H.toast(`Restarted ${name}`, 'success');" in mobile_js


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


def test_phone_terminal_view_has_esc_and_arrows_instead_of_touch_keys() -> None:
    # The phone's own keyboard types into the TUI; the only extra keys are
    # Esc and the four arrows, on a row above the reply box. Esc goes through
    # the Hub's interrupt (2 s double-press guard), the arrows through
    # /keys; none takes focus, so the phone keyboard stays open.
    assert 'id="m-esc" data-key="Escape"' in PHONE
    assert all(f'data-key="{key}"' in PHONE for key in ("Up", "Down", "Left", "Right"))
    assert ".m-keys { display: flex; flex-basis: 100%;" in HUB_CSS
    assert '#m-session[data-view="text"] .m-keys' in HUB_CSS
    assert "key === 'Escape' && openSid ? H.api('POST', sessionUrl('/interrupt')) : pressKeys([key])" in PHONE_JS
    assert "button.addEventListener('touchstart', e => e.preventDefault(), {passive: false});" in PHONE_JS
    assert "button.addEventListener('mousedown', e => e.preventDefault());" in PHONE_JS


def test_phone_page_is_all_the_app_needs() -> None:
    # The app shows only this page: it starts sessions, picks the theme,
    # lists ended sessions to resume, and has the drawer's session actions.
    assert 'data-action="new"' in PHONE and 'id="m-new"' in PHONE
    assert 'data-action="theme"' in PHONE and "function openThemes()" in PHONE_JS
    assert 'id="m-more" data-action="sheet"' in PHONE and "async function openSheet()" in PHONE_JS
    assert 'id="m-resume"' in PHONE and "'/api/sessions?status=stopped&limit=20'" in PHONE_JS
    assert "if (window.AgentHubAndroid) $('m-workspace').hidden = true;" in PHONE_JS
    # An empty reply box submits what was typed into the terminal itself.
    assert "if (inTerminal) act($('m-send'), () => pressKeys(['Enter']));" in PHONE_JS
    # A tmux started here turns into its session once the CLI reports it.
    assert "if (openTmux && s.tmux_session === openTmux) { route(); return; }" in PHONE_JS


def test_phone_reply_carries_files() -> None:
    # A phone screenshot cannot reach the CLI through Ctrl+V: the paperclip
    # (or a paste into the reply box) uploads it, and the reply sends its path.
    assert 'id="m-attach"' in PHONE and '<input type="file" id="m-file-input" multiple hidden>' in PHONE
    assert "fetch('/api/uploads?name=' + encodeURIComponent(name)" in PHONE_JS
    assert "$('m-input').addEventListener('paste'" in PHONE_JS
    assert "typeText(text, sent.map(f => f.path))" in PHONE_JS
    assert '<symbol id="i-paperclip"' in BASE


def test_phone_page_reloads_when_the_hub_serves_a_newer_one() -> None:
    # The page lives on the Hub and changes without app updates; an open one
    # compares its version with the stream's snapshot after every reconnect.
    stream = (ROOT / "src/agent_hub/api/stream.py").read_text()
    assert 'snapshot["page_version"] = _static_version()' in stream
    assert "data.page_version !== PAGE_VERSION) reloadWhenIdle()" in PHONE_JS


def test_brand_mark_is_the_panes_icon_everywhere() -> None:
    macros = (WEB / "templates/_macros.html").read_text()
    mobile_html = (WEB / "templates/mobile.html").read_text()
    assert "{% macro brand_mark() -%}" in macros and 'class="bm-light"' in macros
    assert "{{ brand_mark() }}Agent Hub" in DASHBOARD
    assert "{{ brand_mark() }}Agent Hub" in mobile_html
    assert "&gt;_" not in DASHBOARD + mobile_html
    # The browser tab gets the same mark.
    assert '<link rel="icon" type="image/svg+xml" href="/static/icon.svg' in BASE
    assert (WEB / "static/icon.svg").is_file()


def test_file_paths_from_a_terminal_open_in_a_preview_panel() -> None:
    mobile_html = (WEB / "templates/mobile.html").read_text()
    mobile_js = (WEB / "static/mobile.js").read_text()
    # Desktop: the right-hand panel shows web-file-manager.
    assert "'webterminal:open-file'" in WORKSPACE_JS
    assert 'id="file-preview"' in DASHBOARD and 'id="file-preview-frame"' in DASHBOARD
    assert "`${WEBFM_URL}/?open=${encodeURIComponent(path)}`" in WORKSPACE_JS
    # A click elsewhere closes it, a click into a terminal frame included.
    assert "if (filePreviewOpen() && !event.target.closest('#file-preview, [data-action=\"open-folder\"]')) closeFilePreview();" in WORKSPACE_JS
    assert "if (active?.tagName === 'IFRAME' && active.id !== 'file-preview-frame') closeFilePreview();" in WORKSPACE_JS
    # Guarded: the running Hub reloads templates before it reloads Python.
    assert 'data-webfm-url="{{ webfm_url() if webfm_url is defined else \'\' }}"' in BASE
    # Phone: a full-screen view over the session; Back closes it.
    assert 'id="m-file"' in mobile_html
    assert "if (!$('m-file').hidden) { closeFile(); return true; }" in mobile_js
    assert "webterminal:open-file" in mobile_js


def test_no_live_label_only_a_reconnecting_notice() -> None:
    mobile_html = (WEB / "templates/mobile.html").read_text()
    mobile_js = (WEB / "static/mobile.js").read_text()
    assert ">Live<" not in DASHBOARD + mobile_html
    assert 'id="ws-conn" hidden' in DASHBOARD and 'id="m-conn" hidden' in mobile_html
    assert "if (conn) conn.hidden = state !== 'down';" in HUB_JS
    assert "$('m-conn').hidden = state === 'live';" in mobile_js


def test_tab_bar_folder_button_browses_the_session_directory() -> None:
    # The tab bar's "+" duplicated the sidebar's New session; its place is
    # the open session's folder in the preview panel.
    tabbar = DASHBOARD.split('class="tabbar-end"')[1].split("</div>")[0]
    assert 'data-action="new-session"' not in tabbar
    assert 'id="preview-folder" data-action="open-folder"' in tabbar
    assert "case 'open-folder': toggleSessionFolder(); break;" in WORKSPACE_JS
    assert "else openFilePreview(previewState.cwd);" in WORKSPACE_JS


def test_new_session_dialog_names_tmux_and_the_agent_session_apart() -> None:
    assert '<label for="nc-display">Agent session name</label>' in DASHBOARD
    assert '<label for="nc-name">tmux session name</label>' in DASHBOARD
    assert "title: title || null" in WORKSPACE_JS
    # A launched session's plain tmux row shows the name until the agent reports in.
    assert "const shown = s.title || s.name;" in WORKSPACE_JS


def test_row_buttons_are_redrawn_when_a_session_starts_or_stops_running() -> None:
    # Buttons are drawn with the row; Delete appears only on idle and ended rows.
    assert "LIVE_STATES.has(before) !== LIVE_STATES.has(status)) redrawRow(card.dataset.sid);" in WORKSPACE_JS
    assert "await replaceCard(sid);" in WORKSPACE_JS.split("function redrawRow(sid)")[1]


def test_sidebar_width_can_be_dragged_and_is_remembered() -> None:
    assert 'id="sidebar-resizer" role="separator"' in DASHBOARD
    # Applied before the first paint, clamped like the drag.
    assert "localStorage.getItem('agentHubSidebarWidth')" in DASHBOARD
    assert "Math.min(px, 560, window.innerWidth * 0.45)" in WORKSPACE_JS
    # Terminal frames do not take the pointer while dragging.
    assert ".shell.is-resizing iframe { pointer-events: none; }" in HUB_CSS
    assert "--sidebar-w: 300px;" in HUB_CSS
