/* Agent Hub phone page (/m): the sessions that need you, each session's
   native TUI, and a box to answer it with the phone keyboard.

   A session opens on its Terminal: the Web Terminal attached to its tmux,
   embedded as on the desktop workspace. Text shows the latest exchange
   instead, read from GET /api/sessions/{id}/messages and read again every
   few seconds while the session works; it is easier to read on a small
   screen. The choice is remembered.

   Session states arrive over GET /api/stream (server-sent events, see
   services/attention.py): a snapshot on every connect, then one state per
   change. Routes live in the hash (#/s/<session id>), so the Android app can
   open a session from a notification and its Back key walks the same
   history. #/t/<tmux> shows a tmux started here before its CLI reports a
   session (Codex and opencode do with the first prompt); it turns into the
   session's route when the session appears.

   The page is all the Android app shows, so it also starts sessions, picks
   the theme, and has the session actions of the workspace's drawer. */
(() => {
  const H = window.AgentHub;
  const $ = id => document.getElementById(id);
  const esc = H.escapeHtml;

  const POLL_MS = 4000;
  const PAGE = 30;
  const DISMISSED_KEY = 'agentHubDismissedNotices';
  const MODE_KEY = 'agentHubPhoneView';
  // static_v() when this page was served (the ?v= of this script).
  const PAGE_VERSION = new URL(document.currentScript.src).searchParams.get('v');
  const TERMINAL_URL = document.body.dataset.terminalUrl || '';
  const TERMINAL_ORIGIN = TERMINAL_URL ? new URL(TERMINAL_URL).origin : '';
  const STAGE_TEXT = {
    connecting: 'Connecting to the terminal',
    reconnecting: 'Reconnecting to the terminal',
    disconnected: 'The terminal is disconnected. Tap to reconnect.',
  };

  const sessions = new Map();   // session_id → state from the stream
  const drafts = new Map();     // session_id → unsent reply
  const attached = new Map();   // session_id, or "t:<tmux>" → files for the next reply
  let notices = [];
  let openSid = null;           // session in the session view
  let openRow = null;           // GET /api/sessions/{id}, for a session the stream does not list
  let limit = PAGE;
  let threadKey = '';
  let lastTool = '';
  let pollTimer = null;
  let seenSent = null;          // turn_ended_at already reported as seen
  let cameFromList = false;
  let mode = readMode();         // 'terminal' or 'text'
  let terminal = null;          // {tmux, iframe, state} while the Terminal view is attached
  let openTmux = null;          // tmux in the session view that has no session yet (#/t/<tmux>)
  let startedTool = 'claude';   // the tool of the last session started here, for its icon
  let ended = [];               // recently ended sessions, GET /api/sessions?status=stopped
  let endedOpen = false;        // the Ended group is expanded
  const RESUMABLE = /^(claude|codex|omx|pi|opencode)/;

  // ── Stream ──────────────────────────────────────────────────

  function connect() {
    const source = new EventSource('/api/stream');
    source.onopen = () => setConn('live');
    source.onerror = () => {
      // EventSource retries by itself unless the server refused outright.
      if (source.readyState === EventSource.CLOSED) {
        setConn('down');
        setTimeout(connect, 5000);
      } else {
        setConn('reconnecting');
      }
    };
    source.addEventListener('snapshot', e => {
      const data = JSON.parse(e.data);
      if (data.page_version && PAGE_VERSION && data.page_version !== PAGE_VERSION) reloadWhenIdle();
      sessions.clear();
      for (const s of data.sessions) sessions.set(s.session_id, s);
      notices = data.notices || [];
      if (openSid && !sessions.has(openSid)) loadOpenRow();
      if (openTmux) { route(); return; }
      loadEnded();
      render();
      if (openSid) { refreshView(false); markSeen(); schedulePoll(); }
    });
    source.addEventListener('session', e => {
      const s = JSON.parse(e.data);
      const before = sessions.get(s.session_id);
      sessions.set(s.session_id, s);
      // The tmux started here has its session now.
      if (openTmux && s.tmux_session === openTmux) { route(); return; }
      if (!before) ended = ended.filter(x => x.session_id !== s.session_id);
      if (s.session_id === openSid) {
        const turnChanged = !before || before.status !== s.status || before.turn_ended_at !== s.turn_ended_at;
        if (turnChanged && mode === 'text') loadMessages(false);
        if (mode === 'terminal') attachTerminal();
        markSeen();
        schedulePoll();
      }
      render();
    });
    source.addEventListener('removed', e => {
      const sid = JSON.parse(e.data).session_id;
      sessions.delete(sid);
      if (sid === openSid) { loadOpenRow(); schedulePoll(); }
      loadEnded();
      render();
    });
    source.addEventListener('notice', e => {
      const n = JSON.parse(e.data);
      notices = notices.filter(x => x.id !== n.id).concat(n);
      H.toast(`${n.title}: ${n.message}`, '', 5000);
      render();
    });
  }

  /* The Hub serves a newer page than this one, as after an update: reload,
     but not while a reply is being typed. The hash keeps the open session. */
  function reloadWhenIdle() {
    if ($('m-input').value.trim()) { setTimeout(reloadWhenIdle, 10000); return; }
    location.reload();
  }

  /* The header shows nothing while the stream is up; "Reconnecting"
     appears only while it is down. */
  function setConn(state) {
    $('m-conn').hidden = state === 'live';
  }

  // ── Routing ─────────────────────────────────────────────────

  function route() {
    const match = location.hash.match(/^#\/s\/(.+)$/);
    const sid = match ? decodeURIComponent(match[1]) : null;
    const tmuxMatch = location.hash.match(/^#\/t\/(.+)$/);
    const tmux = tmuxMatch ? decodeURIComponent(tmuxMatch[1]) : null;
    const reported = tmux && [...sessions.values()].find(s => s.tmux_session === tmux);
    if (reported) { location.replace('#/s/' + encodeURIComponent(reported.session_id)); return; }
    if (sid !== openSid || tmux !== openTmux) {
      closeFile();
      if (openSid) drafts.set(openSid, $('m-input').value);
      // A tmux that turns into its session keeps its terminal, its reply and its files.
      const nextTmux = sid ? (sessions.get(sid) || {}).tmux_session : tmux;
      if (sid && openTmux && openTmux === nextTmux) {
        drafts.set(sid, $('m-input').value);
        if (attached.has('t:' + openTmux)) attached.set(sid, attached.get('t:' + openTmux));
        attached.delete('t:' + openTmux);
      }
      if (terminal && terminal.tmux !== nextTmux) detachTerminal();
      openSid = sid;
      openTmux = tmux;
      openRow = null;
      limit = PAGE;
      threadKey = '';
      lastTool = '';
      $('m-thread').innerHTML = '';
      $('m-input').value = sid ? drafts.get(sid) || '' : '';
      autosize();
      renderAttachments();
    }
    $('m-home').hidden = !!(sid || tmux);
    $('m-session').hidden = !(sid || tmux);
    if (sid) {
      if (!sessions.has(sid)) loadOpenRow();
      refreshView(true);
      markSeen();
    } else if (tmux) {
      refreshView(true);
    }
    schedulePoll();
    render();
  }

  function openSession(sid) {
    cameFromList = true;
    location.hash = '#/s/' + encodeURIComponent(sid);
  }

  /* The Android Back key calls this. Closes a dialog or the file view, or
     leaves the session view; false means there was nothing to close. */
  function back() {
    const dialog = document.querySelector('.modal-backdrop [data-cancel]');
    if (dialog) { dialog.click(); return true; }
    if (!$('m-new').hidden) { closeNew(); return true; }
    if (!$('m-file').hidden) { closeFile(); return true; }
    if (!openSid && !openTmux) return false;
    if (cameFromList) history.back();
    else location.replace('#/');
    cameFromList = false;
    return true;
  }

  // ── File view ───────────────────────────────────────────────

  /* A file path tapped in the terminal opens in web-file-manager over the
     session. Its address comes from AGENT_HUB_WEBFM_URL, else this host on
     port 7701 (the file manager's default). */
  const WEBFM_URL = (document.body.dataset.webfmUrl || `${location.protocol}//${location.hostname}:7701`).replace(/\/+$/, '');

  function openFile(path) {
    const url = `${WEBFM_URL}/?open=${encodeURIComponent(path)}`;
    $('m-file-name').textContent = path.replace(/\/+$/, '').split('/').pop() || path;
    $('m-file-path').textContent = path;
    $('m-file-open').href = url;
    $('m-file-frame').src = url;
    $('m-file').hidden = false;
  }

  function closeFile() {
    if ($('m-file').hidden) return;
    $('m-file').hidden = true;
    $('m-file-frame').src = 'about:blank';
  }

  // ── Rendering ───────────────────────────────────────────────

  function toolIcon(tool) {
    const family = /codex|^omx/.test(tool || '') ? 'codex' : /^pi/.test(tool || '') ? 'pi' : /^opencode/.test(tool || '') ? 'opencode' : 'claude';
    return $('m-icon-' + family).innerHTML;
  }

  function ago(iso) {
    const seconds = (Date.now() - Date.parse(iso)) / 1000;
    if (!isFinite(seconds)) return '';
    if (seconds < 60) return 'just now';
    if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)} h ago`;
    return `${Math.floor(seconds / 86400)} d ago`;
  }

  /* A reply's first line of prose for a one-line preview: headings, code
     fences and table rows are skipped, Markdown marks dropped. */
  function firstLine(text) {
    const lines = (text || '').split('\n').map(l => l.trim()).filter(Boolean);
    const prose = lines.find(l => !/^(#|```|\|)/.test(l)) || lines[0] || '';
    return prose.replace(/^([-*+>]|\d+[.)])\s+/, '').replace(/^#+\s*/, '').replace(/[`*_]/g, '');
  }

  function project(cwd) {
    return (cwd || '').split('/').filter(Boolean).pop() || cwd || '';
  }

  /* What a row and the session header say about a state. */
  function describe(s) {
    if (s.status === 'stopped') {
      return {kind: 'ended', dot: 'dot-stopped', label: `Ended ${ago(s.stopped_at)}`, line: project(s.cwd), mono: true};
    }
    if (s.pending) {
      const detail = s.pending.detail ? `${s.pending.tool}: ${s.pending.detail}` : s.pending.tool;
      return {kind: 'waiting', dot: 'dot-waiting', label: 'Waiting for approval', line: detail, mono: true};
    }
    if (s.unseen) {
      return {kind: 'unseen', dot: 'dot-unseen', label: `Finished ${ago(s.turn_ended_at)}`, line: firstLine(s.last_message) || project(s.cwd), mono: !s.last_message};
    }
    if (s.status === 'active') {
      return {kind: 'running', dot: 'dot-running', label: 'Working', line: project(s.cwd), mono: true};
    }
    const line = firstLine(s.last_message);
    return {kind: 'idle', dot: 'dot-idle', label: 'Idle', line: line || project(s.cwd), mono: !line};
  }

  /* The CLI's option text, "Yes, and don't ask again for …", reworded as
     what Always does. */
  function alwaysScope(label) {
    const rest = label.replace(/^yes,?\s*(and\s+)?/i, '');
    return rest.charAt(0).toUpperCase() + rest.slice(1);
  }

  function row(s) {
    const d = describe(s);
    return `<button type="button" class="m-row is-${d.kind}" data-sid="${esc(s.session_id)}">
      <span class="m-row-tool" aria-hidden="true">${toolIcon(s.tool)}</span>
      <span class="m-row-body">
        <span class="m-row-top"><span class="dot ${d.dot}"></span><span class="m-row-name">${esc(s.name)}</span><span class="m-row-state">${esc(d.label)}</span></span>
        <span class="m-row-line${d.mono ? ' is-mono' : ''}">${esc(d.line)}</span>
      </span>
    </button>`;
  }

  function section(title, list, extraClass = '') {
    if (!list.length) return '';
    return `<section class="m-section ${extraClass}">
      <h2 class="m-section-head">${esc(title)}<span class="count">${list.length}</span></h2>
      <div class="m-rows">${list.map(row).join('')}</div>
    </section>`;
  }

  function dismissed() {
    try { return new Set(JSON.parse(localStorage.getItem(DISMISSED_KEY) || '[]')); } catch (_) { return new Set(); }
  }

  function renderNotices() {
    const hidden = dismissed();
    const shown = notices.filter(n => !hidden.has(n.id)).slice(-5).reverse();
    if (!shown.length) return '';
    return `<section class="m-section">
      <h2 class="m-section-head">Notices</h2>
      <div class="m-rows">${shown.map(n => `
        <div class="m-notice">
          <div class="m-notice-top"><strong>${esc(n.title)}</strong><span class="m-notice-time">${esc(ago(n.ts))}</span>
            <button type="button" class="btn btn-ghost icon-btn" data-action="dismiss" data-id="${n.id}" aria-label="Dismiss"><svg class="icon"><use href="#i-x"/></svg></button></div>
          <p>${esc(n.message)}</p>
        </div>`).join('')}</div>
    </section>`;
  }

  function renderList() {
    const all = [...sessions.values()];
    const byName = (a, b) => a.name.localeCompare(b.name);
    const waiting = all.filter(s => s.pending).sort(byName);
    const unseen = all.filter(s => !s.pending && s.unseen).sort((a, b) => (b.turn_ended_at || '').localeCompare(a.turn_ended_at || ''));
    const working = all.filter(s => !s.pending && !s.unseen && s.status === 'active').sort(byName);
    const idle = all.filter(s => !s.pending && !s.unseen && s.status !== 'active').sort(byName);
    let html = renderNotices();
    html += section('Needs you', waiting.concat(unseen), 'is-attention');
    html += section('Working', working);
    html += section('Idle', idle);
    if (!all.length) {
      html += `<div class="m-empty"><strong>No live sessions</strong>Sessions you start here, or in tmux with hooks set up, appear here.<button type="button" class="btn" data-action="new">New session</button></div>`;
    }
    // Ended sessions, folded away: open one to resume it.
    if (ended.length) {
      html += `<details class="m-section m-ended"${endedOpen ? ' open' : ''}>
        <summary class="m-section-head">Ended<span class="count">${ended.length}</span></summary>
        <div class="m-rows">${ended.map(row).join('')}</div>
      </details>`;
    }
    $('m-list').innerHTML = html;
  }

  async function loadEnded() {
    try {
      const rows = await H.api('GET', '/api/sessions?status=stopped&limit=20');
      ended = rows.filter(r => !r.parent_session_id && !sessions.has(r.session_id)).map(r => ({
        session_id: r.session_id,
        name: r.title || r.tmux_session || r.session_id.slice(0, 8),
        tool: r.tool,
        cwd: r.cwd,
        status: 'stopped',
        stopped_at: r.stopped_at || r.last_seen_at,
      }));
    } catch (_) { return; }
    if (!openSid && !openTmux) renderList();
  }

  /* The session view of a tmux started here, until its CLI reports a session. */
  function renderStarting() {
    $('m-name').textContent = openTmux;
    $('m-tool').innerHTML = toolIcon(startedTool);
    $('m-state').textContent = 'Starting: send it your first message';
    $('m-state').className = 'm-state is-running';
    $('m-session').dataset.view = 'terminal';
    document.querySelector('.m-mode').hidden = true;
    $('m-more').hidden = true;
    $('m-working').hidden = true;
    $('m-approval').hidden = true;
    $('m-resume').hidden = true;
    $('m-composer').hidden = false;
    $('m-input').disabled = false;
    $('m-send').disabled = false;
    $('m-attach').disabled = false;
    document.querySelectorAll('.m-keys [data-key]').forEach(b => { b.disabled = false; });
    renderAttachments();
    $('m-hint').hidden = true;
    renderStage();
  }

  function renderSession() {
    if (openTmux) { renderStarting(); return; }
    if (!openSid) return;
    document.querySelector('.m-mode').hidden = false;
    $('m-more').hidden = false;
    const s = sessions.get(openSid);
    const name = s ? s.name : openRow ? (openRow.title || openRow.tmux_session || openSid.slice(0, 8)) : openSid.slice(0, 8);
    $('m-name').textContent = name;
    $('m-tool').innerHTML = toolIcon(s ? s.tool : openRow && openRow.tool);
    const state = $('m-state');
    if (s) {
      const d = describe(s);
      state.textContent = d.label;
      state.className = 'm-state is-' + d.kind;
    } else {
      state.textContent = openRow ? (openRow.status === 'stopped' ? 'Ended' : 'Not running') : '';
      state.className = 'm-state';
    }
    $('m-session').dataset.view = mode;
    document.querySelectorAll('.m-mode [data-mode]').forEach(b => b.classList.toggle('is-active', b.dataset.mode === mode));
    renderStage();

    // In the Terminal view the TUI shows its own progress and the reply bar has Esc.
    const working = mode === 'text' && !!s && s.status === 'active' && !s.pending;
    $('m-working').hidden = !working;
    $('m-working-tool').textContent = working ? lastTool || 'Working' : '';

    const approval = $('m-approval');
    if (s && s.pending) {
      const p = s.pending;
      approval.innerHTML = `
        <div class="m-approval-head"><span class="dot dot-waiting"></span>${esc(p.tool)} needs approval</div>
        ${p.detail ? `<div class="m-approval-detail">${esc(p.detail)}</div>` : ''}
        <div class="m-approval-actions">
          <button type="button" class="btn btn-approve" data-action="approve">Approve</button>
          ${p.always_label ? '<button type="button" class="btn btn-always" data-action="always">Always</button>' : ''}
          <button type="button" class="btn btn-danger" data-action="deny">Deny</button>
        </div>
        ${p.always_label ? `<p class="m-approval-scope">Always: ${esc(alwaysScope(p.always_label))}</p>` : ''}`;
      approval.hidden = false;
    } else {
      approval.hidden = true;
      approval.innerHTML = '';
    }

    // An ended session shows Resume instead of the reply box.
    const isEnded = !s && !!openRow && openRow.status === 'stopped' && RESUMABLE.test(openRow.tool || '');
    $('m-resume').hidden = !isEnded;
    $('m-composer').hidden = isEnded;
    const hint = isEnded ? ''
      : !s ? 'This session is not running, so it cannot take a reply.'
      : s.pending ? 'Answer the approval first.' : '';
    $('m-input').disabled = !!hint;
    $('m-send').disabled = !!hint;
    $('m-attach').disabled = !!hint;
    renderAttachments();
    document.querySelectorAll('.m-keys [data-key]').forEach(b => { b.disabled = !s; });
    $('m-hint').hidden = !hint;
    $('m-hint').textContent = hint;
  }

  function render() {
    if (openSid || openTmux) renderSession();
    else renderList();
  }

  // ── Messages ────────────────────────────────────────────────

  function threadHtml(items) {
    if (!items.length) {
      const s = sessions.get(openSid);
      if (s && s.last_message) return `<div class="msg-assistant">${H.renderMarkdown(s.last_message)}</div>`;
      return '<p class="m-empty-thread">No messages to show yet.</p>';
    }
    let html = items.length >= limit ? '<button type="button" class="btn m-earlier" data-action="earlier">Show earlier messages</button>' : '';
    for (let i = 0; i < items.length;) {
      const item = items[i];
      if (item.role === 'tool') {
        const group = [];
        while (i < items.length && items[i].role === 'tool') group.push(items[i++].text);
        html += `<details class="m-tools"><summary><span class="m-tools-count">${group.length} tool call${group.length > 1 ? 's' : ''}</span><span class="m-tools-last">${esc(group[group.length - 1])}</span></summary>
          <ol>${group.map(t => `<li>${esc(t)}</li>`).join('')}</ol></details>`;
        continue;
      }
      if (item.role === 'user') html += `<div class="msg-user">${esc(item.text)}</div>`;
      else if (item.role === 'assistant') html += `<div class="msg-assistant">${H.renderMarkdown(item.text)}</div>`;
      else html += `<p class="m-note">${esc(item.text)}</p>`;
      i++;
    }
    return html;
  }

  async function loadMessages(toEnd, keepPosition = false) {
    const sid = openSid;
    if (!sid) return;
    let data;
    try {
      data = await H.api('GET', `/api/sessions/${encodeURIComponent(sid)}/messages?limit=${limit}`);
    } catch (err) {
      if (sid === openSid && !threadKey) $('m-thread').innerHTML = `<p class="m-empty-thread">${esc(err.message)}</p>`;
      return;
    }
    if (sid !== openSid) return;
    const items = data.items || [];
    const key = JSON.stringify(items);
    if (key === threadKey) return;
    threadKey = key;
    lastTool = (items.filter(i => i.role === 'tool').pop() || {}).text || '';

    const thread = $('m-thread');
    const nearEnd = thread.scrollHeight - thread.scrollTop - thread.clientHeight < 80;
    const oldHeight = thread.scrollHeight;
    // Tool lists the reader opened stay open, counted from the end.
    const details = [...thread.querySelectorAll('details.m-tools')];
    const openFromEnd = new Set(details.map((d, i) => d.open ? details.length - i : 0).filter(Boolean));
    thread.innerHTML = threadHtml(items);
    const fresh = [...thread.querySelectorAll('details.m-tools')];
    fresh.forEach((d, i) => { if (openFromEnd.has(fresh.length - i)) d.open = true; });
    if (keepPosition) thread.scrollTop += thread.scrollHeight - oldHeight;
    else if (toEnd || nearEnd) thread.scrollTop = thread.scrollHeight;
    renderSession();
  }

  /* Re-read the messages while the open session works or waits. */
  function schedulePoll() {
    clearTimeout(pollTimer);
    const s = openSid && sessions.get(openSid);
    if (!s || document.hidden || mode !== 'text' || (s.status !== 'active' && !s.pending)) return;
    pollTimer = setTimeout(async () => {
      await loadMessages(false);
      schedulePoll();
    }, POLL_MS);
  }

  async function loadOpenRow() {
    const sid = openSid;
    try {
      const row = await H.api('GET', `/api/sessions/${encodeURIComponent(sid)}`);
      if (sid === openSid) { openRow = row; renderSession(); }
    } catch (_) { /* unknown session: the header shows the id */ }
  }

  /* Looking at a finished turn counts as seeing it. */
  function markSeen() {
    const s = openSid && sessions.get(openSid);
    if (!s || !s.unseen || document.visibilityState !== 'visible' || seenSent === s.turn_ended_at) return;
    seenSent = s.turn_ended_at;
    H.api('POST', `/api/sessions/${encodeURIComponent(s.session_id)}/seen`).catch(() => { seenSent = null; });
  }

  // ── Views: Terminal and Text ────────────────────────────────

  function readMode() {
    try { return localStorage.getItem(MODE_KEY) === 'text' ? 'text' : 'terminal'; } catch (_) { return 'terminal'; }
  }

  function setMode(next) {
    if (next === mode) return;
    mode = next;
    try { localStorage.setItem(MODE_KEY, mode); } catch (_) {}
    refreshView(true);
    schedulePoll();
    renderSession();
  }

  /* Load what the current view shows. */
  function refreshView(toEnd) {
    if (!openSid && !openTmux) return;
    if (mode === 'text' && !openTmux) {
      loadMessages(toEnd);
      postToTerminal({type: 'agent-hub:visibility', visible: false});
    } else {
      attachTerminal();
      postToTerminal({type: 'agent-hub:visibility', visible: document.visibilityState === 'visible'});
    }
  }

  function terminalSrc(tmux) {
    // keys=off: no touch-key bar or on-screen keyboard in the frame; the
    // phone's own keyboard types into the TUI and the reply bar has Esc.
    const params = new URLSearchParams({attach: tmux, embed: '1', theme: H.currentTheme(), parent_origin: location.origin, keys: 'off'});
    return `${TERMINAL_URL}/?${params}`;
  }

  /* Attach the open session's tmux in an iframe, once per tmux. The frame
     stays while the session view is open, so switching views is instant;
     leaving the session detaches it. */
  /* The tmux the session view shows. */
  function currentTmux() {
    if (openTmux) return openTmux;
    const s = sessions.get(openSid);
    return s ? s.tmux_session : null;
  }

  function attachTerminal() {
    const tmux = currentTmux();
    if (!tmux || !TERMINAL_URL) { detachTerminal(); renderStage(); return; }
    if (terminal && terminal.tmux === tmux) return;
    detachTerminal();
    const iframe = document.createElement('iframe');
    iframe.title = `Terminal of ${(sessions.get(openSid) || {}).name || tmux}`;
    iframe.src = terminalSrc(tmux);
    iframe.allow = 'clipboard-read; clipboard-write';
    $('m-stage').appendChild(iframe);
    terminal = {tmux, iframe, state: 'connecting'};
    renderStage();
  }

  function detachTerminal() {
    if (!terminal) return;
    terminal.iframe.remove();
    terminal = null;
  }

  function postToTerminal(message) {
    if (terminal && terminal.iframe.contentWindow) terminal.iframe.contentWindow.postMessage(message, TERMINAL_ORIGIN);
  }

  function renderStage() {
    const status = $('m-stage-status');
    const s = openSid && sessions.get(openSid);
    let text = '';
    if (!s && !openTmux) text = 'This session is not running, so there is no terminal to show.';
    else if (!currentTmux()) text = 'This session has no tmux to attach to.';
    else if (terminal) text = STAGE_TEXT[terminal.state] || '';
    status.textContent = text;
    status.hidden = !text;
    status.classList.toggle('is-action', !!terminal && terminal.state === 'disconnected');
    $('m-stage').dataset.conn = terminal ? terminal.state : 'idle';
  }

  window.addEventListener('message', event => {
    if (!terminal || event.origin !== TERMINAL_ORIGIN || event.source !== terminal.iframe.contentWindow) return;
    if (event.data?.source !== 'web-terminal') return;
    const type = event.data.type;
    if (type === 'webterminal:open-file') {
      if (typeof event.data.path === 'string' && event.data.path) openFile(event.data.path);
      return;
    }
    if (type === 'webterminal:ready') {
      postToTerminal({type: 'agent-hub:set-theme', theme: H.currentTheme()});
      postToTerminal({type: 'agent-hub:visibility', visible: mode === 'terminal' && document.visibilityState === 'visible'});
    }
    if (type === 'webterminal:connected') {
      terminal.state = 'connected';
      // opencode and pi chose their colours before this terminal was attached.
      const s = sessions.get(openSid);
      if (s && /^(opencode|pi)/.test(s.tool || '') && s.tmux_session === terminal.tmux) H.noticeTheme([openSid]);
    }
    if (type === 'webterminal:reconnecting') terminal.state = 'reconnecting';
    if (type === 'webterminal:disconnected') terminal.state = 'disconnected';
    renderStage();
  });

  // ── Actions ─────────────────────────────────────────────────

  async function act(button, request, done) {
    button.classList.add('is-busy');
    try {
      const result = await request();
      if (done) H.toast(typeof done === 'function' ? done(result) : done, 'success');
      return result;
    } catch (err) {
      H.toast(err.message, 'error', 5000);
    } finally {
      button.classList.remove('is-busy');
    }
  }

  const sessionUrl = path => `/api/sessions/${encodeURIComponent(openSid)}${path}`;

  document.addEventListener('click', async e => {
    const rowButton = e.target.closest('.m-row[data-sid]');
    if (rowButton) { openSession(rowButton.dataset.sid); return; }
    const modeButton = e.target.closest('.m-mode [data-mode]');
    if (modeButton) { setMode(modeButton.dataset.mode); return; }
    if (e.target.closest('#m-stage-status.is-action')) { detachTerminal(); attachTerminal(); return; }
    const toolChoice = e.target.closest('#mn-tools .choice');
    if (toolChoice) { setNewTool(toolChoice.dataset.tool); return; }
    const folder = e.target.closest('#m-new [data-path]');
    if (folder) { e.preventDefault(); browseNew(folder.dataset.path); return; }
    const button = e.target.closest('[data-action]');
    if (!button) return;
    switch (button.dataset.action) {
      case 'back': back(); break;
      case 'theme': openThemes(); break;
      case 'new': openNew(); break;
      case 'close-new': closeNew(); break;
      case 'create': createNew(); break;
      case 'sheet': openSheet(); break;
      case 'resume': resumeOpen(button); break;
      case 'close-file': closeFile(); break;
      case 'approve': act(button, () => H.api('POST', sessionUrl('/approve')), 'Approved'); break;
      case 'always': act(button, () => H.api('POST', sessionUrl('/approve?always=true')), 'Approved for good'); break;
      case 'deny': act(button, () => H.api('POST', sessionUrl('/interrupt')), 'Denied'); break;
      case 'stop': {
        const ok = await H.confirmDialog({
          title: 'Stop this turn?',
          body: 'Presses Esc in the agent. It stops what it is doing and waits for you.',
          confirmLabel: 'Stop',
          danger: true,
        });
        if (ok) act(button, () => H.api('POST', sessionUrl('/interrupt')), r => r.ignored ? 'Already stopping' : 'Stopped');
        break;
      }
      case 'earlier':
        limit = Math.min(limit + PAGE, 200);
        threadKey = '';
        loadMessages(false, true);
        break;
      case 'dismiss': {
        const ids = dismissed();
        ids.add(Number(button.dataset.id));
        try { localStorage.setItem(DISMISSED_KEY, JSON.stringify([...ids].slice(-50))); } catch (_) {}
        render();
        break;
      }
    }
  });

  // ── Sheets: session actions, theme ──────────────────────────

  /* A sheet from the bottom of the screen. Back and a tap beside it close
     it, as for the Hub's dialogs (an element with data-cancel). */
  function showSheet(label, html, onClick) {
    const backdrop = document.createElement('div');
    backdrop.className = 'modal-backdrop m-sheet-backdrop';
    backdrop.innerHTML = `<div class="m-sheet" role="dialog" aria-modal="true" aria-label="${esc(label)}">${html}</div>`;
    const close = () => backdrop.remove();
    backdrop.addEventListener('click', e => {
      if (e.target === backdrop || e.target.closest('[data-cancel]')) { close(); return; }
      onClick(e, close);
    });
    document.body.appendChild(backdrop);
  }

  function sheetHead(title, sub, icon = '') {
    return `<div class="m-sheet-head">${icon ? `<span class="m-tool">${icon}</span>` : ''}
      <span class="m-title"><span class="m-name">${esc(title)}</span>${sub ? `<span class="m-state">${esc(sub)}</span>` : ''}</span>
      <button type="button" class="btn btn-ghost icon-btn" data-cancel aria-label="Close">${H.icon('x')}</button></div>`;
  }

  /* The open session's details and every action on it: what the desktop
     workspace has in its drawer. */
  async function openSheet() {
    const sid = openSid;
    if (!sid) return;
    let info;
    try {
      info = await H.api('GET', `/api/sessions/${encodeURIComponent(sid)}`);
    } catch (err) {
      H.toast(err.message, 'error', 5000);
      return;
    }
    if (sid !== openSid) return;
    const s = sessions.get(sid);
    const name = s ? s.name : info.title || info.tmux_session || sid.slice(0, 8);
    const resumable = RESUMABLE.test(info.tool || '') && !sid.startsWith('codex-');
    const tokens = info.output_tokens ? `${Number(info.output_tokens).toLocaleString()} out, ${Number(info.cache_read_tokens || 0).toLocaleString()} cached` : '';
    const actions = [
      ['rename', 'edit', 'Rename'],
      s && s.tmux_session && resumable ? ['restart', 'refresh', 'Restart'] : null,
      !s && info.status === 'stopped' && resumable ? ['resume', 'play', 'Resume'] : null,
      s && info.tmux_session ? ['kill', 'power', 'Kill tmux', true] : null,
      info.status !== 'active' ? ['delete', 'trash', 'Delete record', true] : null,
    ].filter(Boolean);
    showSheet('Session', `${sheetHead(name, s ? describe(s).label : info.status === 'stopped' ? 'Ended' : 'Not running', toolIcon(info.tool))}
      <dl class="kv">
        <dt>Tool</dt><dd>${esc(info.tool || '')}${info.model ? ' / ' + esc(info.model) : ''}</dd>
        <dt>Folder</dt><dd>${esc(info.cwd || '')}</dd>
        <dt>tmux</dt><dd>${esc(info.tmux_session || '—')}</dd>
        ${tokens ? `<dt>Tokens</dt><dd>${esc(tokens)}</dd>` : ''}
      </dl>
      <div class="m-sheet-actions">${actions.map(([action, icon, label, danger]) =>
        `<button type="button" class="btn${danger ? ' btn-danger' : ''}" data-sheet="${action}">${H.icon(icon)}${esc(label)}</button>`).join('')}</div>`,
    (e, close) => {
      const b = e.target.closest('[data-sheet]');
      if (!b) return;
      close();
      sessionAction(b.dataset.sheet, info, name);
    });
  }

  async function sessionAction(action, info, name) {
    const sid = info.session_id;
    const url = `/api/sessions/${encodeURIComponent(sid)}`;
    try {
      if (action === 'rename') {
        const next = await H.promptDialog({title: 'Rename session', label: `Shown instead of the tmux name (${info.tmux_session || 'none'}). Leave empty to use the tmux name.`, value: info.title || '', placeholder: info.tmux_session || '', confirmLabel: 'Rename'});
        if (next === null || next === (info.title || '')) return;
        const data = await H.api('POST', `${url}/title`, {title: next});
        const s = sessions.get(sid);
        if (s) s.name = data.title || info.tmux_session || sid.slice(0, 8);
        if (openRow && openRow.session_id === sid) openRow.title = data.title;
        render();
        H.toast(data.title ? `Renamed to ${data.title}` : 'Name reset to the tmux name', 'success');
      } else if (action === 'restart') {
        if (await H.restartSession(sid, name)) H.toast(`Restarted ${name}`, 'success');
      } else if (action === 'resume') {
        await resumeOpen($('m-resume').querySelector('button'));
      } else if (action === 'kill') {
        const ok = await H.confirmDialog({title: `Kill tmux ${info.tmux_session}?`, body: 'This ends the CLI process inside it. The session then shows as ended, and can be resumed.', confirmLabel: 'Kill tmux', danger: true});
        if (!ok) return;
        await H.api('POST', '/api/tmux/kill', {name: info.tmux_session});
        H.toast(`Killed ${info.tmux_session}`, 'success');
      } else if (action === 'delete') {
        const withTmux = info.status === 'idle' && info.tmux_session;
        const ok = await H.confirmDialog({title: 'Delete this session record?', body: withTmux ? 'Its tmux is still running and will be killed as well.' : 'Only the Hub record goes away. Transcripts on disk are untouched.', confirmLabel: 'Delete', danger: true});
        if (!ok) return;
        await H.api('DELETE', url);
        H.toast('Deleted', 'success');
        ended = ended.filter(x => x.session_id !== sid);
        if (openSid === sid) location.replace('#/');
      }
    } catch (err) {
      H.toast(err.message, 'error', 5000);
    }
  }

  /* An ended session comes back in a new tmux; the stream then lists it and
     the view attaches its terminal. */
  async function resumeOpen(button) {
    const sid = openSid;
    if (!sid) return;
    const result = await act(button, () => H.api('POST', `/api/sessions/${encodeURIComponent(sid)}/resume`),
      r => r.mode === 'resume' ? 'Resuming' : 'tmux recreated; run the resume command shown in it');
    if (result) { ended = ended.filter(x => x.session_id !== sid); loadOpenRow(); }
  }

  function openThemes() {
    const current = H.currentTheme();
    const groups = [['dark', 'Dark'], ['light', 'Light']].map(([tone, title]) =>
      `<div class="menu-heading">${title}</div><div class="theme-grid">${H.THEMES.filter(t => t.tone === tone).map(t =>
        `<button type="button" class="theme-option${t.id === current ? ' is-active' : ''}" data-theme="${t.id}"><span class="swatches">${t.swatches.map(c => `<span class="swatch" style="background:${c}"></span>`).join('')}</span><span>${esc(t.name)}</span></button>`).join('')}</div>`).join('');
    showSheet('Theme', sheetHead('Theme') + groups, (e, close) => {
      const b = e.target.closest('[data-theme]');
      if (!b) return;
      H.setTheme(b.dataset.theme);
      close();
    });
  }

  // The terminal follows a theme change, and opencode and pi are told (H.noticeTheme).
  window.addEventListener('agenthub:theme', event => {
    postToTerminal({type: 'agent-hub:set-theme', theme: event.detail.theme});
    H.noticeTheme();
  });

  // ── New session ─────────────────────────────────────────────

  let newTool = 'claude';
  let newPath = null;

  function setNewTool(tool) {
    newTool = tool;
    document.querySelectorAll('#mn-tools .choice').forEach(b => b.classList.toggle('is-active', b.dataset.tool === tool));
  }

  function newError(message) {
    $('mn-error').textContent = message || '';
    $('mn-error').hidden = !message;
  }

  function openNew() {
    newError('');
    $('mn-title').value = '';
    $('mn-tmux').value = '';
    setNewTool(newTool);
    // The folders of recent sessions, newest first.
    const folders = [];
    for (const s of [...sessions.values(), ...ended]) {
      if (s.cwd && !folders.includes(s.cwd)) folders.push(s.cwd);
    }
    $('mn-recent').innerHTML = folders.slice(0, 6).map(f => `<button type="button" class="m-chip" data-path="${esc(f)}">${esc(project(f))}</button>`).join('');
    $('m-new').hidden = false;
    browseNew(newPath || '');
  }

  function closeNew() {
    $('m-new').hidden = true;
  }

  async function browseNew(path) {
    try {
      const data = await H.api('GET', '/api/browse' + (path ? '?path=' + encodeURIComponent(path) : ''));
      newPath = data.path;
      $('mn-path').value = data.path;
      const parts = data.path.split('/').filter(Boolean);
      let acc = '';
      $('mn-crumbs').innerHTML = '<a href="#" data-path="/">/</a>' + parts.map((p, i) => {
        acc += '/' + p;
        return i === parts.length - 1 ? `<span class="current">${esc(p)}</span>` : `<a href="#" data-path="${esc(acc)}">${esc(p)}</a><span>/</span>`;
      }).join('');
      const rows = (data.entries || []).map(e => {
        const child = data.path.endsWith('/') ? data.path + e.name : data.path + '/' + e.name;
        return `<button type="button" data-path="${esc(child)}">${esc(e.name)}/</button>`;
      });
      if (data.parent) rows.unshift(`<button type="button" data-path="${esc(data.parent)}">..</button>`);
      $('mn-dirs').innerHTML = rows.length ? rows.join('') : '<div class="empty">No subfolders</div>';
      newError('');
    } catch (err) {
      newError(err.message);
    }
  }

  async function createNew() {
    newError('');
    const typed = $('mn-path').value.trim();
    if (typed && typed !== newPath) await browseNew(typed);
    if (!newPath || !$('mn-error').hidden) { if (!newPath) newError('Pick a folder first'); return; }
    const name = $('mn-tmux').value.trim().replace(/[–—]/g, '-');
    const title = $('mn-title').value.trim();
    const result = await act($('mn-create'), () => H.api('POST', '/api/tmux/new', {name: name || null, cwd: newPath, command: newTool, title: title || null}));
    if (!result) return;
    closeNew();
    startedTool = newTool;
    H.toast(`Started ${result.title || result.name}`, 'success');
    cameFromList = true;
    location.hash = '#/t/' + encodeURIComponent(result.name);
  }

  $('mn-path').addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); browseNew($('mn-path').value.trim()); } });

  function autosize() {
    const input = $('m-input');
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight + 2, window.innerHeight * 0.4) + 'px';
  }

  /* Typing goes to the session's endpoints, or, for a tmux started here that
     has no session yet, to the tmux by name (POST /api/tmux/input). */
  function typeText(text, attachments) {
    return openSid ? H.api('POST', sessionUrl('/send'), {text, attachments}) : H.api('POST', '/api/tmux/input', {name: openTmux, text, attachments});
  }

  // ── Attachments ─────────────────────────────────────────────

  /* A phone's clipboard is not the workstation's, so Ctrl+V in the CLI
     cannot reach a phone screenshot. A file picked with the paperclip, or
     pasted into the reply box, is uploaded at once (POST /api/uploads); the
     reply carries the saved paths, and the Hub pastes each one into the CLI
     ahead of the text. Claude Code and Codex attach an image path as an
     image. */
  const attachKey = () => openSid || (openTmux ? 't:' + openTmux : null);

  function attachments() {
    const key = attachKey();
    if (!key) return [];
    if (!attached.has(key)) attached.set(key, []);
    return attached.get(key);
  }

  function renderAttachments() {
    const box = $('m-attachments');
    const files = attachKey() ? attachments() : [];
    box.hidden = !files.length || $('m-composer').hidden;
    box.innerHTML = files.map((f, i) => `
      <span class="m-attachment${f.path ? '' : ' is-uploading'}" title="${esc(f.name)}">
        ${f.thumb ? `<img src="${esc(f.thumb)}" alt="">` : '<svg class="icon"><use href="#i-file"/></svg>'}
        <span class="m-attachment-name">${f.path ? esc(f.name) : 'Uploading…'}</span>
        <button type="button" class="m-attachment-remove" data-remove="${i}" aria-label="Remove ${esc(f.name)}"><svg class="icon"><use href="#i-x"/></svg></button>
      </span>`).join('');
  }

  function forget(files, file) {
    const i = files.indexOf(file);
    if (i >= 0) files.splice(i, 1);
    if (file.thumb) URL.revokeObjectURL(file.thumb);
  }

  async function upload(blob) {
    const files = attachments();
    const name = blob.name || `pasted.${(blob.type.split('/')[1] || 'bin').replace(/\W.*/, '')}`;
    const file = {name, path: null, thumb: /^image\/(png|jpeg|gif|webp)$/.test(blob.type) ? URL.createObjectURL(blob) : ''};
    files.push(file);
    renderAttachments();
    try {
      const res = await fetch('/api/uploads?name=' + encodeURIComponent(name), {
        method: 'POST', body: blob, headers: {'Content-Type': blob.type || 'application/octet-stream'},
      });
      const data = await res.json().catch(() => null);
      if (!res.ok) throw new Error((data && data.detail) || res.statusText || 'Upload failed');
      file.path = data.path;
    } catch (err) {
      forget(files, file);
      H.toast(`${name}: ${err.message}`, 'error', 5000);
    }
    renderAttachments();
  }

  $('m-attach').addEventListener('click', () => $('m-file-input').click());
  $('m-file-input').addEventListener('change', e => {
    [...e.target.files].forEach(upload);
    e.target.value = '';
  });
  $('m-attachments').addEventListener('click', e => {
    const button = e.target.closest('[data-remove]');
    if (!button) return;
    const files = attachments();
    const file = files[Number(button.dataset.remove)];
    if (file) forget(files, file);
    renderAttachments();
  });
  // A screenshot pasted into the reply box from the phone's clipboard.
  $('m-input').addEventListener('paste', e => {
    const pasted = [...((e.clipboardData && e.clipboardData.files) || [])];
    if (!pasted.length) return;
    e.preventDefault();
    pasted.forEach(upload);
  });

  function pressKeys(keys) {
    return openSid ? H.api('POST', sessionUrl('/keys'), {keys}) : H.api('POST', '/api/tmux/input', {name: openTmux, keys});
  }

  async function send() {
    const input = $('m-input');
    const text = input.value.trim();
    if (!openSid && !openTmux) return;
    const inTerminal = mode === 'terminal' || !!openTmux;
    const files = attachments();
    if (files.some(f => !f.path)) { H.toast('Wait for the upload to finish'); return; }
    // Nothing in the box: what was typed into the terminal itself is
    // submitted with Enter.
    if (!text && !files.length) {
      if (inTerminal) act($('m-send'), () => pressKeys(['Enter']));
      return;
    }
    const sid = openSid;
    const sent = files.slice();
    const result = await act($('m-send'), () => typeText(text, sent.map(f => f.path)), 'Sent');
    if (!result) return;
    if (sid) drafts.delete(sid);
    sent.forEach(f => forget(files, f));
    renderAttachments();
    if (sid === openSid) { input.value = ''; autosize(); }
    // In the Terminal view the reply shows up in the TUI; close the keyboard so it has the room.
    if (inTerminal) input.blur();
    else setTimeout(() => loadMessages(true), 800);
  }

  /* Esc and the arrows for the TUI, through the Hub. Esc goes to POST
     /interrupt, which ignores a second press within 2 s (two Escapes open
     Claude Code's rewind menu); the arrows to POST /keys. A press must not
     take focus from the terminal or the reply box, or the phone keyboard
     would close, so these buttons never take focus and act on release. */
  document.querySelectorAll('.m-keys [data-key]').forEach(button => {
    const press = () => {
      if (button.disabled) return;
      const key = button.dataset.key;
      act(button, () => key === 'Escape' && openSid ? H.api('POST', sessionUrl('/interrupt')) : pressKeys([key]));
    };
    button.addEventListener('touchstart', e => e.preventDefault(), {passive: false});
    button.addEventListener('touchend', e => { e.preventDefault(); press(); });
    button.addEventListener('mousedown', e => e.preventDefault());
    button.addEventListener('click', e => { e.preventDefault(); press(); });
  });

  $('m-composer').addEventListener('submit', e => { e.preventDefault(); send(); });
  $('m-input').addEventListener('input', autosize);
  // Enter adds a line, as on a phone keyboard; Ctrl+Enter sends from a desktop browser.
  $('m-input').addEventListener('keydown', e => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); send(); }
  });

  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible' && openSid) { refreshView(false); markSeen(); }
    else postToTerminal({type: 'agent-hub:visibility', visible: false});
    schedulePoll();
  });
  window.addEventListener('hashchange', route);
  setInterval(() => { if (!document.hidden) render(); }, 30000);

  window.AgentHubMobile = {back, openSession};
  // In the Android app this page is all there is: no way out to the workspace.
  if (window.AgentHubAndroid) $('m-workspace').hidden = true;
  document.addEventListener('toggle', e => { if (e.target.classList && e.target.classList.contains('m-ended')) endedOpen = e.target.open; }, true);
  connect();
  route();
})();
