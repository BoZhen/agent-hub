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
   history. */
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
      render();
      if (openSid) { refreshView(false); markSeen(); schedulePoll(); }
    });
    source.addEventListener('session', e => {
      const s = JSON.parse(e.data);
      const before = sessions.get(s.session_id);
      sessions.set(s.session_id, s);
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

  function setConn(state) {
    const live = state === 'live';
    $('m-dot').className = 'dot ' + (live ? 'dot-active' : state === 'connecting' ? 'dot-idle' : 'dot-danger');
    $('m-conn').textContent = live ? 'Live' : state === 'connecting' ? 'Connecting' : 'Reconnecting';
  }

  // ── Routing ─────────────────────────────────────────────────

  function route() {
    const match = location.hash.match(/^#\/s\/(.+)$/);
    const sid = match ? decodeURIComponent(match[1]) : null;
    if (sid !== openSid) {
      if (openSid) drafts.set(openSid, $('m-input').value);
      detachTerminal();
      openSid = sid;
      openRow = null;
      limit = PAGE;
      threadKey = '';
      lastTool = '';
      $('m-thread').innerHTML = '';
      $('m-input').value = sid ? drafts.get(sid) || '' : '';
      autosize();
    }
    $('m-home').hidden = !!sid;
    $('m-session').hidden = !sid;
    if (sid) {
      if (!sessions.has(sid)) loadOpenRow();
      refreshView(true);
      markSeen();
    }
    schedulePoll();
    render();
  }

  function openSession(sid) {
    cameFromList = true;
    location.hash = '#/s/' + encodeURIComponent(sid);
  }

  /* The Android Back key calls this. Closes a dialog, or leaves the
     session view; false means there was nothing to close. */
  function back() {
    const dialog = document.querySelector('.modal-backdrop [data-cancel]');
    if (dialog) { dialog.click(); return true; }
    if (!openSid) return false;
    if (cameFromList) history.back();
    else location.replace('#/');
    cameFromList = false;
    return true;
  }

  // ── Rendering ───────────────────────────────────────────────

  function toolIcon(tool) {
    const family = /codex|^omx/.test(tool || '') ? 'codex' : 'claude';
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
      html += `<div class="m-empty"><strong>No live sessions</strong>Sessions you start in the workspace, or in tmux with hooks set up, appear here.<a class="btn" href="/">Open the workspace</a></div>`;
    }
    $('m-list').innerHTML = html;
  }

  function renderSession() {
    if (!openSid) return;
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

    const hint = !s ? 'This session is not running, so it cannot take a reply.'
      : s.pending ? 'Answer the approval first.' : '';
    $('m-input').disabled = !!hint;
    $('m-send').disabled = !!hint;
    $('m-esc').disabled = !s;
    $('m-hint').hidden = !hint;
    $('m-hint').textContent = hint;
  }

  function render() {
    if (openSid) renderSession();
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
    if (!openSid) return;
    if (mode === 'text') {
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
  function attachTerminal() {
    const s = sessions.get(openSid);
    const tmux = s && s.tmux_session;
    if (!tmux || !TERMINAL_URL) { detachTerminal(); renderStage(); return; }
    if (terminal && terminal.tmux === tmux) return;
    detachTerminal();
    const iframe = document.createElement('iframe');
    iframe.title = `Terminal of ${s.name}`;
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
    if (!s) text = 'This session is not running, so there is no terminal to show.';
    else if (!s.tmux_session) text = 'This session has no tmux to attach to.';
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
    if (type === 'webterminal:ready') {
      postToTerminal({type: 'agent-hub:set-theme', theme: H.currentTheme()});
      postToTerminal({type: 'agent-hub:visibility', visible: mode === 'terminal' && document.visibilityState === 'visible'});
    }
    if (type === 'webterminal:connected') terminal.state = 'connected';
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
    const button = e.target.closest('[data-action]');
    if (!button) return;
    switch (button.dataset.action) {
      case 'back': back(); break;
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

  function autosize() {
    const input = $('m-input');
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight + 2, window.innerHeight * 0.4) + 'px';
  }

  async function send() {
    const input = $('m-input');
    const text = input.value.trim();
    if (!text || !openSid) return;
    const sid = openSid;
    const result = await act($('m-send'), () => H.api('POST', sessionUrl('/send'), {text}), 'Sent');
    if (!result) return;
    drafts.delete(sid);
    if (sid === openSid) { input.value = ''; autosize(); }
    // In the Terminal view the reply shows up in the TUI; close the keyboard so it has the room.
    if (mode === 'terminal') input.blur();
    else setTimeout(() => loadMessages(true), 800);
  }

  /* Esc for the TUI, through the Hub (which ignores a second press within
     2 s: two Escapes open Claude Code's rewind menu). The press must not
     take focus from the terminal or the reply box, or the phone keyboard
     would close, so the button never takes focus and acts on release. */
  const escButton = $('m-esc');
  const pressEsc = () => { if (!escButton.disabled) act(escButton, () => H.api('POST', sessionUrl('/interrupt'))); };
  escButton.addEventListener('touchstart', e => e.preventDefault(), {passive: false});
  escButton.addEventListener('touchend', e => { e.preventDefault(); pressEsc(); });
  escButton.addEventListener('mousedown', e => e.preventDefault());
  escButton.addEventListener('click', e => { e.preventDefault(); pressEsc(); });

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
  connect();
  route();
})();
