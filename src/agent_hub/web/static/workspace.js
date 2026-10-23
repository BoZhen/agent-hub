/* Agent Hub workspace: sidebar navigator, workspace tabs, terminal stage,
   inspector drawer, new-session dialog. Loaded only by dashboard.html.
   Top-level functions on purpose: scripts/browser_ui_contract_check.py
   drives previewSession / closeWorkspaceTab / postToTerminal directly. */

const H = window.AgentHub;
const esc = H.escapeHtml;
const icon = H.icon;
const toast = H.toast;
const api = H.api;
const $ = id => document.getElementById(id);

const TERMINAL_URL = document.body.dataset.terminalUrl;
const TERMINAL_ORIGIN = new URL(TERMINAL_URL, location.href).origin;
const TERMINAL_EVENT_TYPES = new Set(['webterminal:ready', 'webterminal:connected', 'webterminal:reconnecting', 'webterminal:disconnected', 'webterminal:focus', 'webterminal:open-file']);
const FILTER_KEY = 'agentHubToolFilter';
const WORKSPACE_KEY = 'agentHubWorkspaceTabs';
const ACTIVE_TAB_KEY = 'agentHubActiveTab';
const GROUPS_KEY = 'agentHubGroups';
const RAW = 'tmux:'; // key prefix for rows that are bare tmux sessions, not Hub sessions
const ENDED_PREVIEW = 3; // ended rows shown per project before "more"

const previewState = {sid: null, tmux: null, cwd: ''};
const STATE_LABELS = {waiting: 'Waiting', running: 'Running', active: 'Active', idle: 'Idle', stopped: 'Ended'};
const STATUS_RANK = {waiting: 0, running: 1, active: 1, idle: 2, stopped: 3};
const VERBS = {
  PreToolUse: e => e.tool_name || 'Tool',
  PostToolUse: e => (e.tool_name || 'Tool') + ' done',
  PostToolUseFailure: e => (e.tool_name || 'Tool') + ' failed',
  UserPromptSubmit: () => 'Prompt',
  Stop: () => 'Idle',
  SessionStart: () => 'Started',
  SubagentStop: () => 'Subagent done',
  Notification: () => 'Notice',
  PreCompact: () => 'Compacting',
};
const EVENT_CLASS = {PreToolUse: 'ev-pre', PostToolUse: 'ev-post', PostToolUseFailure: 'ev-fail', UserPromptSubmit: 'ev-user', Stop: 'ev-stop'};
const mobileViewport = window.matchMedia('(max-width: 699px)');

function verbFor(e) { const f = VERBS[e.event_type]; return f ? f(e) : e.event_type; }
function basename(path) { const parts = String(path || '').split('/').filter(Boolean); return parts[parts.length - 1] || path || ''; }
function rankOf(card) { return STATUS_RANK[card.dataset.status] ?? 2; }

/* ------------------------------------------------------------------ */
/* Rows                                                                */
/* ------------------------------------------------------------------ */
function cardFor(sid) { return sid ? document.getElementById('card-' + sid) : null; }
function allRows() { return Array.from(document.querySelectorAll('.session-row')); }
function rowInfo(card) {
  const d = card.dataset;
  return {
    sid: d.sid, tmux: d.tmux || '', name: d.sessionName || (d.sid || '').slice(0, 8), cwd: d.cwd || '', model: d.model || '',
    host: d.host || '', project: d.project || '', status: d.status || 'idle', tool: d.tool || '', raw: d.raw === '1', pinned: d.pinned === '1',
    title: d.title || '',
  };
}
const LIVE_STATES = new Set(['waiting', 'running', 'active']);
function setRowStatus(card, status, label) {
  const before = card.dataset.status;
  const changed = before !== status;
  card.dataset.status = status;
  const dot = card.querySelector('[data-role="dot"]');
  if (dot) dot.className = 'dot dot-' + status;
  const state = card.querySelector('[data-role="state-label"]');
  if (state) state.textContent = label || STATE_LABELS[status] || status;
  if (changed) placeCard(card);
  renderWorkspaceTabs();
  // A row's buttons follow its state (Delete on idle and ended rows, Pin
  // and Restart until it ends), and they are drawn with the row: redraw it
  // when it starts or stops running.
  if (card.dataset.raw !== '1' && LIVE_STATES.has(before) !== LIVE_STATES.has(status)) redrawRow(card.dataset.sid);
}
/* Fetch a row again from the Hub, at most once per burst of events. */
const rowRedraws = new Set();
function redrawRow(sid) {
  if (rowRedraws.has(sid)) return;
  rowRedraws.add(sid);
  setTimeout(async () => {
    rowRedraws.delete(sid);
    try {
      await replaceCard(sid);
      if (previewState.sid === sid) highlightRow(sid);
    } catch (_) { /* the next reconcile tries again */ }
  }, 400);
}
function setRowEvent(card, data) {
  const line = card.querySelector('[data-role="event-line"]');
  if (!line) return;
  line.innerHTML = `<span class="verb">${esc(verbFor(data))}</span> ${esc(data.summary || '')}`;
  line.classList.remove('flash');
  void line.offsetWidth;
  line.classList.add('flash');
}
function setRowPending(card, pendingTool, detail, alwaysLabel, hasAlways, tmux) {
  if (tmux !== undefined) card.dataset.tmux = tmux || '';
  const host = card.querySelector('[data-role="session-detail"]');
  const approveHost = card.querySelector('[data-role="approve-host"]');
  if (host) host.innerHTML = '';
  if (approveHost) approveHost.innerHTML = '';
  if (pendingTool) {
    card.dataset.pending = '1';
    card.classList.add('is-waiting');
    setRowStatus(card, 'waiting');
    if (host && detail) host.insertAdjacentHTML('beforeend', `<span class="session-line2 is-detail" data-role="pending-detail">${esc(detail)}</span>`);
    if (host && alwaysLabel) host.insertAdjacentHTML('beforeend', `<span class="session-line2 is-scope" data-role="always-label" title="${esc(alwaysLabel)}">Always: ${esc(alwaysLabel)}</span>`);
    if (approveHost && card.dataset.tmux) {
      approveHost.insertAdjacentHTML('beforeend', '<button type="button" class="btn btn-sm btn-approve" data-action="approve">Approve</button>');
      if (hasAlways !== false) approveHost.insertAdjacentHTML('beforeend', `<button type="button" class="btn btn-sm btn-always" data-action="approve-always"${alwaysLabel ? ` title="${esc(alwaysLabel)}"` : ''}>Always</button>`);
    }
  } else {
    card.dataset.pending = '0';
    card.classList.remove('is-waiting');
    setRowStatus(card, 'active');
  }
  renderNeedsAction();
}
function setRowName(card, title) {
  card.dataset.title = title || '';
  const name = title || card.dataset.tmux || card.dataset.host || card.dataset.sid.slice(0, 8);
  card.dataset.sessionName = name;
  const el = card.querySelector('[data-role="name"]');
  if (el) el.textContent = name;
  const select = card.querySelector('.session-select');
  if (select) select.setAttribute('aria-label', `Open ${name}`);
  if (previewState.sid === card.dataset.sid) $('preview-name').textContent = name;
  renderWorkspaceTabs();
  renderNeedsAction();
}
function highlightRow(sid) {
  document.querySelectorAll('.session-row.is-selected').forEach(el => el.classList.remove('is-selected'));
  const card = cardFor(sid);
  if (card) card.classList.add('is-selected');
}

/* ------------------------------------------------------------------ */
/* Project groups                                                      */
/* ------------------------------------------------------------------ */
let groupState = {};
try { groupState = JSON.parse(localStorage.getItem(GROUPS_KEY) || '{}') || {}; } catch (_) { groupState = {}; }
function saveGroupState() { localStorage.setItem(GROUPS_KEY, JSON.stringify(groupState)); }

function projectGroupFor(project) {
  const panel = $('session-panel');
  let group = Array.from(panel.querySelectorAll('.group')).find(g => g.dataset.project === project);
  if (group) return group;
  group = document.createElement('section');
  group.className = 'group';
  group.dataset.project = project;
  group.dataset.collapsed = groupState['project:' + project] ? '1' : '0';
  group.innerHTML = `<button type="button" class="group-head" data-action="toggle-group" aria-expanded="${group.dataset.collapsed === '0'}">${icon('chevron-down', 'icon icon-sm chev')}<span class="group-name" title="${esc(project)}">${esc(project)}</span><span class="group-count"></span></button><div class="group-body"></div>`;
  panel.appendChild(group);
  return group;
}
/* Insert a row into its project at the right spot: waiting, live, idle,
   ended; pinned first within a band; otherwise server order. */
function placeCard(card) {
  const group = projectGroupFor(card.dataset.project || 'Other');
  const body = group.querySelector('.group-body');
  if (!card.dataset.order) card.dataset.order = '0';
  const key = c => [rankOf(c), c.dataset.pinned === '1' ? 0 : 1, Number(c.dataset.order || 0)];
  const mine = key(card);
  const rows = Array.from(body.querySelectorAll('.session-row')).filter(r => r !== card);
  const before = rows.find(r => {
    const k = key(r);
    for (let i = 0; i < mine.length; i++) { if (mine[i] !== k[i]) return mine[i] < k[i]; }
    return false;
  });
  if (before) body.insertBefore(card, before); else body.appendChild(card);
  updateGroupCounts();
}
function rebuildGroups() {
  const panel = $('session-panel');
  const cards = Array.from(panel.querySelectorAll('.session-row'));
  cards.forEach((card, i) => { card.dataset.order = String(i + 1); });
  panel.querySelectorAll('.group').forEach(g => g.remove());
  cards.forEach(card => {
    const group = projectGroupFor(card.dataset.project || 'Other');
    group.querySelector('.group-body').appendChild(card);
  });
  Array.from(panel.querySelectorAll('.group')).forEach(group => {
    const body = group.querySelector('.group-body');
    Array.from(body.querySelectorAll('.session-row')).forEach(placeCard);
  });
  updateGroupCounts();
}
function updateGroupCounts() {
  const panel = $('session-panel');
  const groups = Array.from(panel.querySelectorAll('.group'));
  groups.forEach(group => {
    const body = group.querySelector('.group-body');
    const rows = Array.from(body.querySelectorAll('.session-row'));
    const visible = rows.filter(r => !r.classList.contains('is-hidden'));
    group.querySelector('.group-count').textContent = visible.length;
    group.hidden = visible.length === 0;
    const minRank = visible.length ? Math.min(...visible.map(rankOf)) : 9;
    group.dataset.rank = String(minRank);
    group.classList.toggle('is-cold', minRank >= 3);
    // Projects with only ended sessions start folded unless the user
    // chose otherwise; they unfold on their own when something goes live.
    const userChoice = groupState['project:' + group.dataset.project];
    if (userChoice === undefined) {
      const fold = minRank >= 3;
      group.dataset.collapsed = fold ? '1' : '0';
      group.querySelector('.group-head').setAttribute('aria-expanded', fold ? 'false' : 'true');
    }
    // Ended rows beyond the first few fold behind a "more" line.
    const ended = visible.filter(r => r.dataset.status === 'stopped');
    ended.forEach((r, i) => r.classList.toggle('is-overflow', i >= ENDED_PREVIEW));
    rows.filter(r => r.dataset.status !== 'stopped').forEach(r => r.classList.remove('is-overflow'));
    let more = group.querySelector('.group-more');
    const hidden = Math.max(0, ended.length - ENDED_PREVIEW);
    if (hidden > 0) {
      if (!more) { more = document.createElement('button'); more.type = 'button'; more.className = 'group-more'; more.dataset.action = 'toggle-ended'; group.appendChild(more); }
      more.textContent = group.dataset.showEnded === '1' ? 'Show fewer ended' : `${hidden} more ended`;
    } else if (more) {
      more.remove();
    }
  });
  // Projects with something live float up; ended-only projects sink.
  groups.sort((a, b) => (Number(a.dataset.rank) - Number(b.dataset.rank)) || a.dataset.project.localeCompare(b.dataset.project))
    .forEach(g => panel.appendChild(g));
  const any = panel.querySelectorAll('.session-row').length > 0;
  $('panel-empty').hidden = any;
}
function toggleGroup(group) {
  const collapsed = group.dataset.collapsed === '1';
  group.dataset.collapsed = collapsed ? '0' : '1';
  group.querySelector('.group-head').setAttribute('aria-expanded', collapsed ? 'true' : 'false');
  groupState['project:' + group.dataset.project] = !collapsed;
  saveGroupState();
}

/* ------------------------------------------------------------------ */
/* Filters                                                             */
/* ------------------------------------------------------------------ */
function applyFilters() {
  // The pi filter exists only while pi is installed.
  const saved = localStorage.getItem(FILTER_KEY) || 'all';
  const filter = document.querySelector(`[data-tool-filter="${CSS.escape(saved)}"]`) ? saved : 'all';
  const query = ($('session-search').value || '').trim().toLowerCase();
  allRows().forEach(card => {
    const fam = card.dataset.toolFamily || 'other';
    const toolOk = filter === 'all' || filter === fam;
    const hay = `${card.dataset.sessionName} ${card.dataset.tmux} ${card.dataset.project} ${card.dataset.cwd} ${card.dataset.model} ${card.dataset.host}`.toLowerCase();
    const queryOk = !query || hay.includes(query);
    card.classList.toggle('is-hidden', !(toolOk && queryOk));
  });
  document.querySelectorAll('[data-tool-filter]').forEach(btn => btn.classList.toggle('is-active', btn.dataset.toolFilter === filter));
  updateGroupCounts();
}
document.querySelectorAll('[data-tool-filter]').forEach(btn => btn.addEventListener('click', () => {
  localStorage.setItem(FILTER_KEY, btn.dataset.toolFilter);
  applyFilters();
}));
$('session-search').addEventListener('input', applyFilters);

/* ------------------------------------------------------------------ */
/* Needs action                                                        */
/* ------------------------------------------------------------------ */
let orphanCount = 0;
function renderNeedsAction() {
  const section = $('needs-section');
  const host = $('needs-list');
  const cards = allRows().filter(c => c.dataset.pending === '1');
  const total = cards.length + (orphanCount ? 1 : 0);
  $('needs-count').textContent = total;
  section.hidden = total === 0;
  host.innerHTML = '';
  cards.forEach(card => {
    const info = rowInfo(card);
    const detail = card.querySelector('[data-role="pending-detail"]')?.textContent || 'Approval required';
    const item = document.createElement('div');
    item.className = 'attention-item';
    item.innerHTML = `<button type="button" class="attention-main" data-action="open" data-sid="${esc(info.sid)}"><span class="attention-name">${esc(info.name)}</span><span class="attention-detail">${esc(detail)}</span></button>`
      + (info.tmux
        ? `<button type="button" class="btn btn-sm btn-approve" data-action="approve" data-sid="${esc(info.sid)}">Approve</button>`
        : `<button type="button" class="btn btn-sm" data-action="inspect" data-sid="${esc(info.sid)}">Details</button>`);
    host.appendChild(item);
  });
  if (orphanCount) {
    const item = document.createElement('div');
    item.className = 'attention-item';
    item.innerHTML = `<div class="attention-main"><span class="attention-name">${orphanCount} session${orphanCount > 1 ? 's' : ''} lost tmux</span><span class="attention-detail">Recreate tmux and resume the CLI</span></div><button type="button" class="btn btn-sm" data-action="restore-all">Resume</button>`;
    host.appendChild(item);
  }
}
async function refreshOrphanCount() {
  try {
    const data = await api('GET', '/api/orphan-sessions');
    const next = (data.orphans || []).length;
    if (next !== orphanCount) { orphanCount = next; renderNeedsAction(); }
  } catch (_) { /* hub unreachable; the WebSocket chip already says so */ }
}
async function restoreAll(btn) {
  const ok = await H.confirmDialog({title: 'Resume lost sessions?', body: 'Each one gets a new tmux with its CLI resumed inside.', confirmLabel: 'Resume'});
  if (!ok) return;
  btn.classList.add('is-busy');
  try {
    const data = await api('POST', '/api/restore-orphans');
    toast(`Restored ${data.restored} of ${data.total}`, 'success');
    setTimeout(() => location.reload(), 1200);
  } catch (e) { toast(e.message, 'error'); btn.classList.remove('is-busy'); }
}

/* ------------------------------------------------------------------ */
/* WebSocket updates                                                   */
/* ------------------------------------------------------------------ */
let statsTimer = null;
function scheduleStats() {
  if (statsTimer) return;
  statsTimer = setTimeout(async () => {
    statsTimer = null;
    try {
      const s = await api('GET', '/api/stats');
      $('stat-active').textContent = s.active_sessions;
      $('stat-active-wrap').classList.toggle('is-zero', !s.active_sessions);
      $('stat-waiting').textContent = s.waiting_sessions;
      $('stat-waiting-wrap').classList.toggle('is-zero', !s.waiting_sessions);
    } catch (_) {}
  }, 1500);
}
function onWsMessage(data) {
  if (data.type === 'pending') {
    const card = cardFor(data.session_id);
    if (card) {
      const wasPending = card.dataset.pending === '1';
      setRowPending(card, data.pending_tool, data.pending_detail, data.always_label, data.has_always, data.tmux_session);
      if (data.pending_tool && !wasPending) toast(`${rowInfo(card).name} needs your approval`);
    } else if (data.pending_tool) {
      ensureCardExists(data.session_id);
    }
    if (typeof data.waiting_count === 'number') {
      $('stat-waiting').textContent = data.waiting_count;
      $('stat-waiting-wrap').classList.toggle('is-zero', !data.waiting_count);
    }
    if (inspectorSid === data.session_id) openInspector(data.session_id);
    return;
  }
  if (data.type !== 'event') return;
  const card = cardFor(data.session_id);
  if (!card) {
    ensureCardExists(data.session_id);
  } else {
    setRowEvent(card, data);
    if (card.dataset.pending !== '1' && data.status) setRowStatus(card, data.status);
  }
  if (data.event_type === 'SessionStart') scheduleTmuxRefresh();
  inspectorOnEvent(data);
  scheduleStats();
}
window.onWsMessage = onWsMessage;
window.addEventListener('agenthub:ws-open', () => { scheduleStats(); refreshOrphanCount(); scheduleTmuxRefresh(); reconcileRows(); });

const _cardFetches = new Set();
async function ensureCardExists(sessionId) {
  if (_cardFetches.has(sessionId) || cardFor(sessionId)) return;
  _cardFetches.add(sessionId);
  try {
    const data = await api('GET', `/partials/session-card/${sessionId}`);
    if (cardFor(sessionId)) return;
    const wrapper = document.createElement('div');
    wrapper.innerHTML = (data.html || '').trim();
    const card = wrapper.firstElementChild;
    if (!card) return;
    placeCard(card);
    card.querySelector('[data-role="event-line"]')?.classList.add('flash');
    // A tab opened on the bare tmux before the Hub knew the session now
    // belongs to the session itself; keep the terminal attached.
    const rawKey = RAW + (card.dataset.tmux || '');
    if (card.dataset.tmux && workspaceTabs.includes(rawKey)) {
      workspaceTabs = workspaceTabs.map(k => k === rawKey ? sessionId : k);
      if (previewState.sid === rawKey) { previewState.sid = sessionId; highlightRow(sessionId); }
      cardFor(rawKey)?.remove();
    }
    applyFilters();
    renderNeedsAction();
    renderWorkspaceTabs();
  } catch (e) {
    console.error('ensureCardExists failed', e);
  } finally {
    _cardFetches.delete(sessionId);
  }
}

/* ------------------------------------------------------------------ */
/* Bare tmux sessions, filed under their project                        */
/* ------------------------------------------------------------------ */
let tmuxTimer = null;
function scheduleTmuxRefresh() {
  if (tmuxTimer) return;
  tmuxTimer = setTimeout(() => { tmuxTimer = null; loadBareTmux(); }, 1200);
}
function rawRowHtml(s) {
  const status = s.dead ? 'stopped' : (s.attached ? 'active' : 'idle');
  const key = RAW + s.name;
  const label = s.dead ? 'Dead' : (s.attached ? 'Attached' : 'Detached');
  // A name given at launch shows until the agent reports in (Codex does
  // with its first prompt).
  const shown = s.title || s.name;
  return `<div class="session-row" id="card-${esc(key)}" data-sid="${esc(key)}" data-raw="1" data-tool="tmux" data-tool-family="other" data-project="${esc(basename(s.cwd) || 'Other')}" data-cwd="${esc(s.cwd || '')}" data-host="" data-session-name="${esc(shown)}" data-title="" data-model="" data-status="${status}" data-pending="0" data-pinned="0" data-tmux="${s.dead ? '' : esc(s.name)}">`
    + `<button type="button" class="session-select" data-action="open" aria-label="Open ${esc(shown)}"><span class="session-tool" aria-hidden="true">${icon('terminal')}</span><span class="session-body"><span class="session-line1"><span class="dot dot-${status}" data-role="dot"></span><span class="session-name" data-role="name" title="${esc(s.name)}">${esc(shown)}</span><span class="badge" title="tmux session without a Hub record">shell</span><span class="session-state" data-role="state-label">${label}</span></span><span class="session-line2" data-role="event-line">${esc(s.cwd || '')}</span></span></button>`
    + '<div class="session-approve" data-role="approve-host"></div>'
    + `<div class="session-actions">${s.dead ? '' : `<a class="btn icon-btn" href="${TERMINAL_URL}/?attach=${encodeURIComponent(s.name)}" target="_blank" title="Open in a new tab">${icon('external', 'icon icon-sm')}</a>`}<button type="button" class="btn icon-btn btn-danger" data-action="kill" data-tmux="${esc(s.name)}" title="Kill tmux">${icon('power', 'icon icon-sm')}</button></div></div>`;
}
async function loadBareTmux() {
  try {
    const data = await api('GET', '/api/tmux/list');
    const list = data.sessions || [];
    const keep = new Set(list.map(s => RAW + s.name));
    document.querySelectorAll('.session-row[data-raw="1"]').forEach(row => { if (!keep.has(row.dataset.sid)) row.remove(); });
    list.forEach(s => {
      const existing = cardFor(RAW + s.name);
      const wrapper = document.createElement('div');
      wrapper.innerHTML = rawRowHtml(s);
      const fresh = wrapper.firstElementChild;
      if (existing) {
        if (existing.dataset.status === fresh.dataset.status) return;
        existing.replaceWith(fresh);
      }
      placeCard(fresh);
    });
    if (previewState.sid) highlightRow(previewState.sid);
    applyFilters();
    renderWorkspaceTabs();
  } catch (_) { /* tmux unavailable; nothing to file */ }
}

/* ------------------------------------------------------------------ */
/* Workspace tabs                                                      */
/* ------------------------------------------------------------------ */
let workspaceTabs = [];
try { workspaceTabs = JSON.parse(localStorage.getItem(WORKSPACE_KEY) || '[]'); } catch (_) { workspaceTabs = []; }
if (!Array.isArray(workspaceTabs)) workspaceTabs = [];

function saveWorkspaceTabs() {
  localStorage.setItem(WORKSPACE_KEY, JSON.stringify(workspaceTabs));
  if (previewState.sid) localStorage.setItem(ACTIVE_TAB_KEY, previewState.sid); else localStorage.removeItem(ACTIVE_TAB_KEY);
}
function openWorkspaceTab(sid) {
  if (!workspaceTabs.includes(sid)) workspaceTabs.push(sid);
  previewState.sid = sid;
  saveWorkspaceTabs();
  renderWorkspaceTabs();
}
function renderWorkspaceTabs() {
  const host = $('workspace-tabs');
  if (!host) return;
  workspaceTabs = workspaceTabs.filter(sid => cardFor(sid) || sid.startsWith(RAW));
  host.innerHTML = '';
  workspaceTabs.forEach(sid => {
    const card = cardFor(sid);
    if (!card) return;
    const info = rowInfo(card);
    const active = previewState.sid === sid;
    const tab = document.createElement('div');
    tab.className = 'tab' + (active ? ' is-active' : '');
    const main = document.createElement('button');
    main.type = 'button';
    main.className = 'tab-main';
    main.setAttribute('role', 'tab');
    main.dataset.sid = sid;
    main.setAttribute('aria-selected', active ? 'true' : 'false');
    main.setAttribute('aria-label', `Open ${info.name}`);
    main.tabIndex = active ? 0 : -1;
    main.innerHTML = `<span class="dot dot-${esc(info.status)}"></span><span class="tab-name">${esc(info.name)}</span>`;
    main.addEventListener('click', () => previewSession(card));
    main.addEventListener('keydown', event => handleWorkspaceTabKey(event, sid));
    const close = document.createElement('button');
    close.type = 'button';
    close.className = 'tab-close';
    close.setAttribute('aria-label', `Close ${info.name} view`);
    close.innerHTML = icon('x', 'icon icon-sm');
    close.addEventListener('click', () => closeWorkspaceTab(sid));
    tab.append(main, close);
    host.appendChild(tab);
  });
  saveWorkspaceTabs();
}
function handleWorkspaceTabKey(event, sid) {
  const tabs = Array.from(document.querySelectorAll('#workspace-tabs [role="tab"]'));
  const index = tabs.indexOf(event.currentTarget);
  let target = null;
  if (event.key === 'ArrowRight') target = tabs[(index + 1) % tabs.length];
  if (event.key === 'ArrowLeft') target = tabs[(index - 1 + tabs.length) % tabs.length];
  if (event.key === 'Home') target = tabs[0];
  if (event.key === 'End') target = tabs[tabs.length - 1];
  if (target) {
    event.preventDefault();
    const targetSid = target.dataset.sid;
    target.click();
    requestAnimationFrame(() => {
      Array.from(document.querySelectorAll('#workspace-tabs [role="tab"]')).find(tab => tab.dataset.sid === targetSid)?.focus();
    });
  }
  if (event.key === 'Delete' || event.key === 'Backspace') {
    event.preventDefault();
    closeWorkspaceTab(sid);
    requestAnimationFrame(() => document.querySelector('#workspace-tabs [role="tab"][tabindex="0"]')?.focus());
  }
}
/* Closing a tab only changes what this browser shows. The tmux session
   and the CLI inside it keep running. */
function closeWorkspaceTab(sid) {
  const index = workspaceTabs.indexOf(sid);
  const card = cardFor(sid);
  const tmux = card ? rowInfo(card).tmux : null;
  workspaceTabs = workspaceTabs.filter(item => item !== sid);
  // Closing the last tab on a tmux drops its frame; the tmux itself lives on.
  if (tmux && !tmuxInUse(tmux)) dropFrame(tmux);
  if (previewState.sid === sid) {
    const next = workspaceTabs[Math.max(0, index - 1)] || workspaceTabs[0];
    const nextCard = cardFor(next);
    if (nextCard) previewSession(nextCard); else clearTerminalStage();
  } else {
    renderWorkspaceTabs();
  }
  saveWorkspaceTabs();
  requestAnimationFrame(() => document.querySelector('#workspace-tabs [role="tab"][aria-selected="true"]')?.focus());
}

/* ------------------------------------------------------------------ */
/* Terminal stage: one iframe per open tab, only the active one shown  */
/* ------------------------------------------------------------------ */
const MAX_LIVE_FRAMES = 6;
const frames = new Map(); // tmux name → {iframe, state, lastUsed}
const STAGE_TEXT = {connecting: 'Connecting to the terminal', reconnecting: 'Reconnecting to the terminal', disconnected: 'Terminal disconnected. Click to reconnect.'};
const STAGE_DOT = {connected: 'dot-active', connecting: 'dot-running', reconnecting: 'dot-waiting', disconnected: 'dot-danger'};

function setStageConn(state) {
  $('preview-pane').dataset.conn = state;
  $('stage-status-text').textContent = STAGE_TEXT[state] || '';
  $('terminal-status-dot').className = 'dot ' + (STAGE_DOT[state] || 'dot-stopped');
}
function terminalSrc(tmux) {
  const theme = H.currentTheme();
  const params = new URLSearchParams({attach: tmux, embed: '1', theme, parent_origin: location.origin});
  return `${TERMINAL_URL}/?${params.toString()}`;
}
function activeFrame() { return previewState.tmux ? frames.get(previewState.tmux) || null : null; }
function frameForWindow(win) {
  for (const frame of frames.values()) if (frame.iframe.contentWindow === win) return frame;
  return null;
}
function tmuxInUse(tmux) {
  return workspaceTabs.some(sid => { const card = cardFor(sid); return !!card && rowInfo(card).tmux === tmux; });
}
function dropFrame(tmux) {
  const frame = frames.get(tmux);
  if (!frame) return;
  frame.iframe.remove();
  frames.delete(tmux);
}
function ensureFrame(tmux) {
  let frame = frames.get(tmux);
  if (frame) return frame;
  // Keep the pool bounded: the frame unused for longest goes first. It is
  // recreated (and reconnects) the next time its tab is opened.
  while (frames.size >= MAX_LIVE_FRAMES) {
    let oldest = null;
    frames.forEach((f, name) => { if (name !== previewState.tmux && (!oldest || f.lastUsed < oldest.frame.lastUsed)) oldest = {name, frame: f}; });
    if (!oldest) break;
    dropFrame(oldest.name);
  }
  const iframe = document.createElement('iframe');
  iframe.className = 'stage-frame';
  iframe.title = 'Session terminal';
  iframe.setAttribute('allow', 'clipboard-read; clipboard-write');
  iframe.setAttribute('referrerpolicy', 'no-referrer');
  iframe.dataset.tmux = tmux;
  iframe.hidden = true;
  iframe.src = terminalSrc(tmux);
  $('stage-frames').appendChild(iframe);
  frame = {iframe, state: 'connecting', lastUsed: Date.now()};
  frames.set(tmux, frame);
  return frame;
}
function activateFrame(tmux) {
  const frame = ensureFrame(tmux);
  frame.lastUsed = Date.now();
  previewState.tmux = tmux;
  frames.forEach((f, name) => { f.iframe.hidden = name !== tmux; });
  setStageConn(frame.state);
}
function reloadTerminal() {
  const frame = activeFrame();
  if (!frame) return;
  const src = frame.iframe.src;
  frame.iframe.src = 'about:blank';
  requestAnimationFrame(() => { frame.iframe.src = src; frame.state = 'connecting'; setStageConn('connecting'); });
}
$('stage-status-text').addEventListener('click', () => { if ($('preview-pane').dataset.conn === 'disconnected') reloadTerminal(); });

function previewSession(el) {
  const card = el && el.closest ? el.closest('.session-row') : null;
  if (!card) return;
  const info = rowInfo(card);
  if (!info.tmux || info.status === 'stopped') { openInspector(info.sid); return; }

  const theme = H.currentTheme();
  activateFrame(info.tmux);
  previewState.sid = info.sid;
  $('stage-frames').classList.remove('hidden');
  $('preview-empty').classList.add('hidden');

  $('preview-name').textContent = info.name;
  const model = $('preview-model');
  model.textContent = info.model;
  model.hidden = !info.model;
  const cwd = $('preview-cwd');
  cwd.textContent = info.cwd || info.project;
  cwd.title = info.host ? `${info.cwd} on ${info.host}` : info.cwd;
  $('preview-detail').hidden = false;
  previewState.cwd = info.cwd;
  $('preview-folder').hidden = !info.cwd;
  const standalone = $('preview-standalone');
  standalone.hidden = false;
  standalone.href = `${TERMINAL_URL}/?attach=${encodeURIComponent(info.tmux)}&theme=${encodeURIComponent(theme)}`;

  openWorkspaceTab(info.sid);
  highlightRow(info.sid);
  if (inspectorSid && inspectorSid !== info.sid) openInspector(info.sid);
  if (mobileViewport.matches) setMobileMode('terminal');
  syncTerminalVisibility();
}
function clearTerminalStage() {
  Array.from(frames.keys()).forEach(dropFrame);
  $('stage-frames').classList.add('hidden');
  $('preview-empty').classList.remove('hidden');
  $('preview-name').textContent = 'No session';
  $('preview-model').hidden = true;
  $('preview-cwd').textContent = '';
  $('preview-detail').hidden = true;
  $('preview-standalone').hidden = true;
  $('preview-folder').hidden = true;
  previewState.cwd = '';
  setStageConn('idle');
  previewState.sid = null;
  previewState.tmux = null;
  highlightRow(null);
  renderWorkspaceTabs();
  saveWorkspaceTabs();
  syncTerminalVisibility();
}

/* Sidebar toggle (desktop). The page applies a stored 'hidden' before the
   first paint; this keeps the button in step and remembers the choice. */
const SIDEBAR_KEY = 'agentHubSidebar';
function sidebarHidden() { return $('shell').classList.contains('is-sidebar-hidden'); }
function setSidebarHidden(hidden) {
  $('shell').classList.toggle('is-sidebar-hidden', hidden);
  const btn = $('sidebar-toggle');
  btn.setAttribute('aria-expanded', String(!hidden));
  btn.title = hidden ? 'Show sidebar' : 'Hide sidebar';
  try { localStorage.setItem(SIDEBAR_KEY, hidden ? 'hidden' : 'shown'); } catch (_) {}
}

/* Sidebar width (desktop): drag its right edge, or use the arrow keys on
   it; double-click returns to the default. The width is remembered and
   applied before the first paint (dashboard.html). */
const SIDEBAR_WIDTH_KEY = 'agentHubSidebarWidth';
function clampSidebarWidth(px) { return Math.round(Math.max(220, Math.min(px, 560, window.innerWidth * 0.45))); }
function setSidebarWidth(px, save = true) {
  const shell = $('shell');
  if (px == null) shell.style.removeProperty('--sidebar-w');
  else shell.style.setProperty('--sidebar-w', clampSidebarWidth(px) + 'px');
  if (!save) return;
  try {
    if (px == null) localStorage.removeItem(SIDEBAR_WIDTH_KEY);
    else localStorage.setItem(SIDEBAR_WIDTH_KEY, String(clampSidebarWidth(px)));
  } catch (_) {}
}
(() => {
  const handle = $('sidebar-resizer');
  const width = () => $('sidebar').getBoundingClientRect().width;
  handle.addEventListener('pointerdown', event => {
    if (event.button !== 0) return;
    event.preventDefault();
    handle.setPointerCapture(event.pointerId);
    $('shell').classList.add('is-resizing');
    const left = $('shell').getBoundingClientRect().left;
    const move = e => setSidebarWidth(e.clientX - left, false);
    const end = e => {
      handle.removeEventListener('pointermove', move);
      handle.removeEventListener('pointerup', end);
      handle.removeEventListener('pointercancel', end);
      $('shell').classList.remove('is-resizing');
      setSidebarWidth(e.clientX - left);
    };
    handle.addEventListener('pointermove', move);
    handle.addEventListener('pointerup', end);
    handle.addEventListener('pointercancel', end);
  });
  handle.addEventListener('dblclick', () => setSidebarWidth(null));
  // A narrower window keeps the remembered width within 45 % of it.
  window.addEventListener('resize', () => {
    const set = parseFloat($('shell').style.getPropertyValue('--sidebar-w'));
    if (set) setSidebarWidth(set, false);
  });
  handle.addEventListener('keydown', event => {
    const step = event.shiftKey ? 48 : 16;
    if (event.key === 'ArrowLeft') { event.preventDefault(); setSidebarWidth(width() - step); }
    if (event.key === 'ArrowRight') { event.preventDefault(); setSidebarWidth(width() + step); }
  });
})();

function setMobileMode(mode) {
  document.body.dataset.mobileMode = mode === 'terminal' ? 'terminal' : 'sessions';
  syncTerminalVisibility();
}
function terminalIsVisible() {
  return !!previewState.sid && (!mobileViewport.matches || document.body.dataset.mobileMode === 'terminal');
}
/* Only the active frame is told it is visible; hidden ones skip resize
   traffic and stay connected quietly. */
function syncTerminalVisibility() {
  const visible = terminalIsVisible();
  requestAnimationFrame(() => frames.forEach((frame, name) => postToTerminal({type: 'agent-hub:visibility', visible: visible && name === previewState.tmux}, frame)));
}
mobileViewport.addEventListener?.('change', syncTerminalVisibility);
window.addEventListener('resize', syncTerminalVisibility);

function postToTerminal(message, frame = activeFrame()) {
  if (frame?.iframe.contentWindow) frame.iframe.contentWindow.postMessage(message, TERMINAL_ORIGIN);
}
function broadcastToTerminals(message) {
  frames.forEach(frame => postToTerminal(message, frame));
}
window.addEventListener('message', event => {
  if (event.origin !== TERMINAL_ORIGIN || event.data?.source !== 'web-terminal') return;
  const frame = frameForWindow(event.source);
  if (!frame) return;
  const type = event.data.type;
  if (!TERMINAL_EVENT_TYPES.has(type)) return;
  if (type === 'webterminal:open-file') {
    if (typeof event.data.path === 'string' && event.data.path) openFilePreview(event.data.path);
    return;
  }
  const name = frame.iframe.dataset.tmux;
  if (type === 'webterminal:ready') {
    postToTerminal({type: 'agent-hub:set-theme', theme: H.currentTheme()}, frame);
    postToTerminal({type: 'agent-hub:visibility', visible: terminalIsVisible() && name === previewState.tmux}, frame);
  }
  if (type === 'webterminal:connected') frame.state = 'connected';
  if (type === 'webterminal:reconnecting') frame.state = 'reconnecting';
  if (type === 'webterminal:disconnected') frame.state = 'disconnected';
  if (name === previewState.tmux) setStageConn(frame.state);
});

/* ------------------------------------------------------------------ */
/* Inspector drawer                                                    */
/* ------------------------------------------------------------------ */
let inspectorSid = null;
let orphanCache = {at: 0, ids: new Set()};
async function getOrphanIds() {
  if (Date.now() - orphanCache.at < 10000) return orphanCache.ids;
  try {
    const data = await api('GET', '/api/orphan-sessions');
    orphanCache = {at: Date.now(), ids: new Set((data.orphans || []).map(o => o.session_id))};
  } catch (_) {}
  return orphanCache.ids;
}
function eventItem(e) {
  const t = String(e.created_at || '').replace('T', ' ').slice(11, 16);
  return `<li><time>${esc(t)}</time><span class="ev ${EVENT_CLASS[e.event_type] || 'ev-start'}">${esc(verbFor(e))}</span><span class="sum">${esc(e.summary || '—')}</span></li>`;
}
async function openInspector(sid) {
  if (filePreviewOpen()) closeFilePreview();
  const drawer = $('inspector');
  inspectorSid = sid;
  drawer.classList.add('is-open');
  drawer.setAttribute('aria-hidden', 'false');
  const body = $('inspector-body');
  body.innerHTML = '<div class="group-loading">Loading</div>';
  const card = cardFor(sid);
  $('inspector-rename').hidden = sid.startsWith(RAW);
  if (sid.startsWith(RAW)) {
    const info = card ? rowInfo(card) : {name: sid.slice(RAW.length), cwd: '', tmux: sid.slice(RAW.length)};
    $('inspector-title').textContent = info.name;
    body.innerHTML = `<div class="drawer-section"><dl class="kv"><dt>Kind</dt><dd>tmux session without a Hub record</dd><dt>Directory</dt><dd>${esc(info.cwd || '—')}</dd><dt>tmux</dt><dd>${esc(info.name)}</dd></dl></div>`
      + `<div class="drawer-section"><div class="drawer-actions">${info.tmux ? `<a class="btn" href="${TERMINAL_URL}/?attach=${encodeURIComponent(info.tmux)}" target="_blank">${icon('external')}New tab</a>` : ''}<button type="button" class="btn btn-danger" data-action="kill" data-tmux="${esc(info.name)}">${icon('power')}Kill tmux</button></div></div>`
      + '<div class="drawer-section"><p class="group-empty">Start Claude, Codex or pi inside it and the Hub will track it as a session.</p></div>';
    return;
  }
  try {
    const [session, events, orphans] = await Promise.all([
      api('GET', `/api/sessions/${sid}`),
      api('GET', `/api/sessions/${sid}/events/latest?n=30`),
      getOrphanIds(),
    ]);
    if (inspectorSid !== sid) return;
    renderInspector(session, events, orphans.has(sid));
  } catch (e) {
    body.innerHTML = `<div class="group-empty">${esc(e.message)}</div>`;
  }
}
function renderInspector(s, events, isOrphan) {
  const status = s.pending_tool ? 'waiting' : s.status;
  const live = status !== 'stopped';
  const fmt = iso => String(iso || '').replace('T', ' ').slice(0, 19);
  const canFork = s.tool && s.tool.includes('claude') && s.tmux_session && !s.transferred && live;
  const canResume = (isOrphan || status === 'stopped') && /^(claude|codex|omx|pi)/.test(s.tool || '');
  const canRestart = live && !isOrphan && s.tmux_session && !s.parent_session_id && /^(claude|codex|omx|pi)/.test(s.tool || '') && !s.session_id.startsWith('codex-');
  $('inspector-title').textContent = s.title || s.tmux_session || s.hostname;
  let html = '';
  if (s.pending_tool) {
    html += `<div class="drawer-section"><div class="pending-box"><strong>Waiting for approval</strong><br>${esc(s.pending_tool)}${s.pending_detail ? '<br>' + esc(s.pending_detail) : ''}${s.pending_always_label ? '<br>Always: ' + esc(s.pending_always_label) : ''}</div>`;
    if (s.tmux_session) html += `<div class="drawer-actions" style="margin-top:8px"><button type="button" class="btn btn-approve" data-action="approve" data-sid="${esc(s.session_id)}">Approve</button>${s.pending_always_label ? `<button type="button" class="btn btn-always" data-action="approve-always" data-sid="${esc(s.session_id)}" title="${esc(s.pending_always_label)}">Always</button>` : ''}</div>`;
    html += '</div>';
  }
  const tokens = s.output_tokens ? `${Number(s.output_tokens).toLocaleString()} out / ${Number(s.cache_read_tokens || 0).toLocaleString()} cached` : '';
  html += `<div class="drawer-section"><dl class="kv">
    <dt>State</dt><dd>${esc(STATE_LABELS[status] || status)}${isOrphan ? ' (tmux missing)' : ''}</dd>
    <dt>Project</dt><dd>${esc(basename(s.cwd))}</dd>
    <dt>Directory</dt><dd>${esc(s.cwd)}</dd>
    <dt>Host</dt><dd>${esc(s.hostname)}</dd>
    <dt>Tool</dt><dd>${esc(s.tool || '—')}${s.model ? ' / ' + esc(s.model) : ''}</dd>
    <dt>tmux</dt><dd>${esc(s.tmux_session || '—')}</dd>
    <dt>Started</dt><dd>${esc(fmt(s.started_at))}</dd>
    <dt>Last seen</dt><dd>${esc(fmt(s.last_seen_at))}</dd>
    ${tokens ? `<dt>Tokens</dt><dd>${esc(tokens)}</dd>` : ''}
    <dt>Session id</dt><dd>${esc(s.session_id)}</dd>
    ${canResume && s.resume_command ? `<dt>Resume with</dt><dd><code>${esc(s.resume_command)}</code></dd>` : ''}
  </dl></div>`;
  html += '<div class="drawer-section"><div class="drawer-actions">'
    + `<button type="button" class="btn" data-action="rename" data-sid="${esc(s.session_id)}">${icon('edit')}Rename</button>`
    + (live ? `<button type="button" class="btn" data-action="pin" data-sid="${esc(s.session_id)}">${icon('pin')}${s.pinned ? 'Unpin' : 'Pin'}</button>` : '')
    + (s.tmux_session && live && !isOrphan ? `<a class="btn" href="${TERMINAL_URL}/?attach=${encodeURIComponent(s.tmux_session)}" target="_blank">${icon('external')}New tab</a>` : '')
    + (canFork ? `<button type="button" class="btn" data-action="fork" data-sid="${esc(s.session_id)}" data-tmux="${esc(s.tmux_session)}">${icon('fork')}Fork</button>` : '')
    + `<a class="btn" href="/sessions/${encodeURIComponent(s.session_id)}">${icon('book')}Full page</a>`
    + (canRestart ? `<button type="button" class="btn" data-action="restart" data-sid="${esc(s.session_id)}" title="Restart the CLI, resuming this conversation (picks up an update)">${icon('refresh')}Restart</button>` : '')
    + (canResume ? `<button type="button" class="btn btn-primary" data-action="resume" data-sid="${esc(s.session_id)}">${icon('play')}Resume</button>` : '')
    + (s.tmux_session && live && !isOrphan ? `<button type="button" class="btn btn-danger" data-action="kill" data-sid="${esc(s.session_id)}" data-tmux="${esc(s.tmux_session)}">${icon('power')}Kill tmux</button>` : '')
    + (status === 'idle' || status === 'stopped' ? `<button type="button" class="btn btn-danger" data-action="delete" data-sid="${esc(s.session_id)}">${icon('trash')}Delete record</button>` : '')
    + '</div></div>';
  html += `<div class="drawer-section"><h3>Recent events</h3>${events.length ? `<ul class="timeline" id="inspector-timeline">${events.map(eventItem).join('')}</ul>` : '<div class="timeline-empty">No events yet.</div>'}</div>`;
  $('inspector-body').innerHTML = html;
}
function inspectorOnEvent(data) {
  if (inspectorSid !== data.session_id) return;
  const ul = $('inspector-timeline');
  if (!ul) { openInspector(data.session_id); return; }
  ul.insertAdjacentHTML('afterbegin', eventItem({...data, created_at: new Date().toISOString()}));
}
/* ------------------------------------------------------------------ */
/* File preview                                                        */
/* ------------------------------------------------------------------ */
/* A file path clicked in a terminal opens in web-file-manager inside the
   right-hand panel. Its address comes from AGENT_HUB_WEBFM_URL, else this
   host on port 7701 (the file manager's default). */
const WEBFM_URL = (document.body.dataset.webfmUrl || `${location.protocol}//${location.hostname}:7701`).replace(/\/+$/, '');
let previewWatch = null;  // interval that closes the panel once a terminal has the focus
let previewPath = '';     // what the panel shows
function openFilePreview(path) {
  previewPath = path;
  $('preview-folder').setAttribute('aria-pressed', String(path === previewState.cwd));
  if (inspectorSid) closeInspector();
  const url = `${WEBFM_URL}/?open=${encodeURIComponent(path)}`;
  const title = $('file-preview-title');
  title.textContent = path.replace(/\/+$/, '').split('/').pop() || path;
  title.title = path;
  $('file-preview-open').href = url;
  $('file-preview-frame').src = url;
  const drawer = $('file-preview');
  drawer.classList.add('is-open');
  drawer.setAttribute('aria-hidden', 'false');
  // Like the menus, a click anywhere else closes the panel. A click in a
  // terminal never reaches this page, but it moves the focus into that
  // terminal's frame, also from the preview's own frame, which this page is
  // not told about; so take the focus here and watch where it goes.
  drawer.focus({preventScroll: true});
  clearInterval(previewWatch);
  previewWatch = setInterval(() => {
    const active = document.activeElement;
    if (active?.tagName === 'IFRAME' && active.id !== 'file-preview-frame') closeFilePreview();
  }, 200);
}
/* The tab bar's folder button: the open session's directory in the same
   panel, to browse its files and preview them. A second press closes it. */
function toggleSessionFolder() {
  if (!previewState.cwd) return;
  if (filePreviewOpen() && previewPath === previewState.cwd) closeFilePreview();
  else openFilePreview(previewState.cwd);
}
function filePreviewOpen() { return $('file-preview').classList.contains('is-open'); }
function closeFilePreview() {
  clearInterval(previewWatch);
  previewWatch = null;
  previewPath = '';
  $('preview-folder').setAttribute('aria-pressed', 'false');
  const drawer = $('file-preview');
  drawer.classList.remove('is-open');
  drawer.setAttribute('aria-hidden', 'true');
  $('file-preview-frame').src = 'about:blank';
}
function closeInspector() {
  inspectorSid = null;
  const drawer = $('inspector');
  drawer.classList.remove('is-open');
  drawer.setAttribute('aria-hidden', 'true');
}

/* ------------------------------------------------------------------ */
/* Session actions                                                     */
/* ------------------------------------------------------------------ */
async function approveSession(sid, always, btn) {
  const card = cardFor(sid);
  if (btn) { btn.classList.add('is-busy'); btn.textContent = 'Sending'; }
  try {
    const data = await api('POST', `/api/sessions/${sid}/approve` + (always ? '?always=true' : ''));
    if (!data.ok) throw new Error(data.detail || 'Approve failed');
    toast('Approval sent to the terminal', 'success');
    if (card) setRowPending(card, null);
    if (inspectorSid === sid) openInspector(sid);
  } catch (e) {
    toast(e.message, 'error');
    if (btn) { btn.classList.remove('is-busy'); btn.textContent = always ? 'Always' : 'Approve'; }
  }
}
async function renameSession(sid) {
  const card = cardFor(sid);
  if (!card || sid.startsWith(RAW)) return;
  const current = card.dataset.title || '';
  const next = await H.promptDialog({title: 'Rename session', label: `Shown instead of the tmux name (${card.dataset.tmux || 'none'}). Leave empty to use the tmux name.`, value: current, placeholder: card.dataset.tmux || '', confirmLabel: 'Rename'});
  if (next === null || next === current) return;
  try {
    const data = await api('POST', `/api/sessions/${sid}/title`, {title: next});
    setRowName(card, data.title || '');
    if (inspectorSid === sid) openInspector(sid);
    toast(data.title ? `Renamed to ${data.title}` : 'Name reset to the tmux name', 'success');
  } catch (e) { toast(e.message, 'error'); }
}
async function togglePin(sid, btn) {
  const card = cardFor(sid);
  const pinned = card ? card.dataset.pinned === '1' : /Unpin/.test(btn?.textContent || '');
  try {
    await api('POST', `/api/sessions/${sid}/pin`, {pinned: !pinned});
    if (card) {
      card.dataset.pinned = pinned ? '0' : '1';
      const pinBtn = card.querySelector('[data-action="pin"]');
      if (pinBtn) { pinBtn.classList.toggle('is-pinned', !pinned); pinBtn.title = pinned ? 'Pin to top of project' : 'Unpin'; }
      placeCard(card);
    }
    toast(pinned ? 'Unpinned' : 'Pinned to the top of its project', 'success');
    if (inspectorSid === sid) openInspector(sid);
  } catch (e) { toast(e.message, 'error'); }
}
async function forkSession(sid, tmuxName, btn) {
  const ok = await H.confirmDialog({title: `Fork ${tmuxName}?`, body: 'A copy of this conversation starts in a new tmux. The original keeps running.', confirmLabel: 'Fork'});
  if (!ok) return;
  btn?.classList.add('is-busy');
  try {
    const data = await api('POST', `/api/sessions/${sid}/fork`, {});
    toast(`Forked into ${data.name}`, 'success');
    await openRawTmux(data.name);
  } catch (e) { toast(e.message, 'error'); }
  finally { btn?.classList.remove('is-busy'); }
}
async function killTmux(name, sid) {
  const ok = await H.confirmDialog({title: `Kill tmux ${name}?`, body: 'This ends the CLI process inside it. The Hub record stays until it is swept.', confirmLabel: 'Kill tmux', danger: true});
  if (!ok) return;
  try {
    await api('POST', '/api/tmux/kill', {name});
    toast(`Killed ${name}`, 'success');
    orphanCache.at = 0;
    const card = cardFor(sid) || cardFor(RAW + name);
    if (card) { card.dataset.tmux = ''; setRowStatus(card, 'stopped'); }
    if (previewState.tmux === name) setStageConn('disconnected');
    if (inspectorSid) openInspector(inspectorSid);
    scheduleTmuxRefresh();
  } catch (e) { toast(e.message, 'error'); }
}
async function deleteRecord(sid) {
  const card = cardFor(sid);
  const idleWithTmux = card && card.dataset.status === 'idle' && card.dataset.tmux;
  const ok = await H.confirmDialog({title: 'Delete this session record?', body: idleWithTmux ? 'Its tmux is still running and will be killed as well.' : 'Only the Hub record goes away. Transcripts on disk are untouched.', confirmLabel: 'Delete', danger: true});
  if (!ok) return;
  try {
    await api('DELETE', `/api/sessions/${sid}`);
    toast('Deleted', 'success');
    dropRow(sid);
  } catch (e) { toast(e.message, 'error'); }
}
/* Take a session the Hub no longer has off the page. */
function dropRow(sid) {
  if (previewState.sid === sid) closeWorkspaceTab(sid);
  workspaceTabs = workspaceTabs.filter(k => k !== sid);
  cardFor(sid)?.remove();
  if (inspectorSid === sid) closeInspector();
  updateGroupCounts();
  renderNeedsAction();
  renderWorkspaceTabs();
  scheduleStats();
}
/* The WebSocket carries hook events and approvals only. A session the
   dead-tmux sweep ends, one deleted elsewhere (phone, API, another tab),
   and anything sent while the socket was down never reach this page, and
   a waiting row ignores later status changes. So compare the live rows
   with the Hub every 15 s and on reconnect, and re-render the rows that
   disagree. */
let reconciling = false;
async function reconcileRows() {
  const rows = allRows().filter(r => r.dataset.raw !== '1' && r.dataset.status !== 'stopped');
  if (reconciling || !rows.length) return;
  reconciling = true;
  try {
    const [active, idle] = await Promise.all([
      api('GET', '/api/sessions?status=active&limit=200'),
      api('GET', '/api/sessions?status=idle&limit=200'),
    ]);
    const live = new Map([...active, ...idle].map(s => [s.session_id, s]));
    for (const card of rows) {
      const sid = card.dataset.sid;
      const s = live.get(sid);
      if (s && (card.dataset.pending === '1') === !!s.pending_tool) continue;
      try {
        await replaceCard(sid);
        if (previewState.sid === sid) highlightRow(sid);
        if (inspectorSid === sid) openInspector(sid);
      } catch (e) {
        if (/not found/i.test(e.message)) dropRow(sid);
      }
    }
  } catch (_) {
    // Hub unreachable; the next round tries again.
  } finally {
    reconciling = false;
  }
}
/* Fetch a fresh row for `sid` and swap it into the sidebar. */
async function replaceCard(sid) {
  const data = await api('GET', `/partials/session-card/${sid}`);
  const wrapper = document.createElement('div');
  wrapper.innerHTML = (data.html || '').trim();
  const card = wrapper.firstElementChild;
  if (!card) return null;
  cardFor(sid)?.remove();
  placeCard(card);
  applyFilters();
  renderNeedsAction();
  renderWorkspaceTabs();
  return card;
}
/* Bring an ended (or tmux-less) session back: the Hub recreates its tmux
   and runs `claude --resume` / `codex resume` inside, then the terminal
   opens on it. */
async function resumeSession(sid) {
  const before = cardFor(sid);
  const name = before ? rowInfo(before).name : sid.slice(0, 8);
  try {
    const data = await api('POST', `/api/sessions/${sid}/resume`);
    if (data.mode === 'resume') toast(`Resuming ${name}`, 'success');
    else toast(`tmux recreated for ${name}; run the resume command shown in it`, 'success');
    orphanCache.at = 0;
    refreshOrphanCount();
    const card = await replaceCard(sid);
    if (card) previewSession(card);
    if (inspectorSid === sid) openInspector(sid);
  } catch (e) { toast(e.message, 'error'); }
}
/* Restart a running session's CLI to pick up an update (see
   H.restartSession). */
async function restartSession(sid, btn) {
  const card = cardFor(sid);
  const name = card ? rowInfo(card).name : sid.slice(0, 8);
  btn?.classList.add('is-busy');
  try {
    if (await H.restartSession(sid, name)) toast(`Restarted ${name}`, 'success');
  } catch (e) { toast(e.message, 'error'); }
  finally { btn?.classList.remove('is-busy'); }
}
/* Restart every running session of one CLI: Claude, Codex or pi. The Hub
   skips sessions in the middle of a turn and says which. */
async function restartAll(family) {
  closePopovers();
  const label = {claude: 'Claude', codex: 'Codex', pi: 'pi'}[family] || family;
  const count = allRows().filter(r => r.dataset.raw !== '1' && r.dataset.toolFamily === family && r.dataset.status !== 'stopped' && r.dataset.tmux && !r.dataset.parentSid).length;
  if (!count) { toast(`No running ${label} sessions`); return; }
  const ok = await H.confirmDialog({title: `Restart ${count} ${label} session${count > 1 ? 's' : ''}?`, body: `Each ${label} CLI exits and resumes its conversation in the same tmux, so it runs the version installed now. Sessions in the middle of a turn are left alone.`, confirmLabel: 'Restart'});
  if (!ok) return;
  try {
    const data = await api('POST', '/api/restart-sessions', {tool: family});
    const n = data.restarted.length;
    let message = `Restarted ${n} ${label} session${n === 1 ? '' : 's'}`;
    if (data.skipped.length) message += ` · skipped ${data.skipped.map(s => `${s.name} (${s.reason})`).join(', ')}`;
    toast(message, data.skipped.length ? '' : 'success', data.skipped.length ? 8000 : 2800);
  } catch (e) { toast(e.message, 'error'); }
}
async function clearStopped() {
  const ok = await H.confirmDialog({title: 'Clear all ended sessions?', body: 'Removes every ended session and its events from the Hub. Transcripts on disk are untouched.', confirmLabel: 'Clear', danger: true});
  if (!ok) return;
  try {
    await api('POST', '/api/sessions/clear-stopped');
    toast('Ended sessions cleared', 'success');
    document.querySelectorAll('.session-row[data-status="stopped"]:not([data-raw="1"])').forEach(row => {
      if (inspectorSid === row.dataset.sid) closeInspector();
      row.remove();
    });
    workspaceTabs = workspaceTabs.filter(k => cardFor(k));
    updateGroupCounts();
    renderWorkspaceTabs();
    scheduleStats();
  } catch (e) { toast(e.message, 'error'); }
}
async function openRawTmux(name) {
  await loadBareTmux();
  const row = cardFor(RAW + name);
  if (row) {
    const group = row.closest('.group');
    if (group && group.dataset.collapsed === '1') toggleGroup(group);
    previewSession(row);
  }
}

/* Every data-action button in the page routes through here. */
document.addEventListener('click', event => {
  const btn = event.target.closest('[data-action]');
  if (!btn) return;
  const action = btn.dataset.action;
  const row = btn.closest('.session-row');
  const sid = btn.dataset.sid || row?.dataset.sid || null;
  const tmux = btn.dataset.tmux || row?.dataset.tmux || '';
  switch (action) {
    case 'open': { const card = cardFor(sid); if (card) previewSession(card); break; }
    case 'approve': approveSession(sid, false, btn); break;
    case 'approve-always': approveSession(sid, true, btn); break;
    case 'pin': togglePin(sid, btn); break;
    case 'rename': renameSession(sid); break;
    case 'rename-current': if (inspectorSid) renameSession(inspectorSid); break;
    case 'inspect': openInspector(sid); break;
    case 'fork': forkSession(sid, tmux, btn); break;
    case 'kill': killTmux(tmux, sid); break;
    case 'delete': deleteRecord(sid); break;
    case 'restore':
    case 'resume': resumeSession(sid); break;
    case 'restore-all': restoreAll(btn); break;
    case 'restart': restartSession(sid, btn); break;
    case 'restart-all': restartAll(btn.dataset.tool); break;
    case 'clear-stopped': clearStopped(); break;
    case 'toggle-group': toggleGroup(btn.closest('.group')); break;
    case 'toggle-ended': { const g = btn.closest('.group'); g.dataset.showEnded = g.dataset.showEnded === '1' ? '0' : '1'; updateGroupCounts(); break; }
    case 'new-session': ncOpen(); break;
    case 'mobile-sessions': setMobileMode('sessions'); break;
    case 'toggle-sidebar': setSidebarHidden(!sidebarHidden()); break;
    case 'close-inspector': closeInspector(); break;
    case 'close-file-preview': closeFilePreview(); break;
    case 'open-folder': toggleSessionFolder(); break;
    case 'inspect-current': if (previewState.sid) openInspector(previewState.sid); break;
    default: break;
  }
});

/* ------------------------------------------------------------------ */
/* Popovers: theme picker, utilities                                   */
/* ------------------------------------------------------------------ */
function renderThemeOptions() {
  const host = $('theme-panel');
  const current = H.currentTheme();
  host.innerHTML = '';
  ['dark', 'light'].forEach(tone => {
    const heading = document.createElement('div');
    heading.className = 'menu-heading';
    heading.textContent = tone === 'dark' ? 'Dark' : 'Light';
    host.appendChild(heading);
    const grid = document.createElement('div');
    grid.className = 'theme-grid';
    H.THEMES.filter(t => t.tone === tone).forEach(t => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'theme-option' + (t.id === current ? ' is-active' : '');
      button.innerHTML = `<span class="swatches">${t.swatches.map(c => `<span class="swatch" style="background:${c}"></span>`).join('')}</span><span>${esc(t.name)}</span>`;
      button.addEventListener('click', () => { H.setTheme(t.id); renderThemeOptions(); closePopovers(); });
      grid.appendChild(button);
    });
    host.appendChild(grid);
  });
  $('theme-label').textContent = H.themeName(current);
}
function closePopovers() {
  document.querySelectorAll('.popover').forEach(p => { p.hidden = true; });
  document.querySelectorAll('[aria-haspopup]').forEach(b => b.setAttribute('aria-expanded', 'false'));
}
function togglePopover(button, panel) {
  const open = panel.hidden;
  closePopovers();
  panel.hidden = !open;
  button.setAttribute('aria-expanded', open ? 'true' : 'false');
}
$('theme-button').addEventListener('click', () => togglePopover($('theme-button'), $('theme-panel')));
$('utility-button').addEventListener('click', () => togglePopover($('utility-button'), $('utility-menu')));
document.addEventListener('click', event => {
  if (!event.target.closest('.popover, [aria-haspopup]')) closePopovers();
  if (filePreviewOpen() && !event.target.closest('#file-preview, [data-action="open-folder"]')) closeFilePreview();
});
// A click inside a terminal frame never reaches this document, but it takes
// the focus away from this window, so close the menus then as well.
window.addEventListener('blur', closePopovers);
window.addEventListener('agenthub:theme', event => {
  $('theme-label').textContent = H.themeName(event.detail.theme);
  broadcastToTerminals({type: 'agent-hub:set-theme', theme: event.detail.theme});
  const standalone = $('preview-standalone');
  if (!standalone.hidden && previewState.tmux) standalone.href = `${TERMINAL_URL}/?attach=${encodeURIComponent(previewState.tmux)}&theme=${encodeURIComponent(event.detail.theme)}`;
});

/* ------------------------------------------------------------------ */
/* New session dialog                                                  */
/* ------------------------------------------------------------------ */
let ncBrowsePath = null;
let ncTool = 'claude';
function ncSetTool(tool) {
  ncTool = tool;
  document.querySelectorAll('#nc-tools .choice').forEach(el => el.classList.toggle('is-active', el.dataset.tool === tool));
}
function ncShowError(msg) { const el = $('nc-error'); el.textContent = msg; el.hidden = false; }
function ncClearError() { $('nc-error').hidden = true; }
async function ncBrowse(path) {
  const data = await api('GET', '/api/browse' + (path ? '?path=' + encodeURIComponent(path) : ''));
  ncBrowsePath = data.path;
  $('nc-path').value = data.path;
  ncRenderCrumb(data.path);
  ncRenderDirList(data);
}
function ncRenderCrumb(path) {
  const parts = path.split('/').filter(Boolean);
  const crumbs = [{name: '/', path: '/'}];
  let acc = '';
  for (const p of parts) { acc += '/' + p; crumbs.push({name: p, path: acc}); }
  const el = $('nc-breadcrumb');
  el.innerHTML = crumbs.map((c, i) => i === crumbs.length - 1
    ? `<span class="current">${esc(c.name)}</span>`
    : `<a href="#" data-path="${esc(c.path)}">${esc(c.name)}</a><span>/</span>`).join('');
  el.querySelectorAll('a').forEach(a => a.addEventListener('click', ev => { ev.preventDefault(); ncBrowse(a.dataset.path).catch(e => ncShowError(e.message)); }));
}
function ncRenderDirList(data) {
  const el = $('nc-dir-list');
  const rows = [];
  if (data.parent) rows.push(`<button type="button" data-path="${esc(data.parent)}">..</button>`);
  for (const e of (data.entries || [])) {
    const child = data.path.endsWith('/') ? data.path + e.name : data.path + '/' + e.name;
    rows.push(`<button type="button" data-path="${esc(child)}">${esc(e.name)}/</button>`);
  }
  el.innerHTML = rows.length ? rows.join('') : '<div class="empty">No subdirectories</div>';
  el.querySelectorAll('button').forEach(btn => btn.addEventListener('click', () => ncBrowse(btn.dataset.path).catch(e => ncShowError(e.message))));
}
function ncOpen() {
  ncClearError();
  $('nc-name').value = '';
  $('nc-display').value = '';
  ncSetTool('claude');
  $('modal-new').hidden = false;
  ncBrowse(ncBrowsePath || '').catch(e => ncShowError(e.message));
  setTimeout(() => $('nc-display').focus(), 30);
}
function ncClose() { $('modal-new').hidden = true; }
async function ncCreate() {
  ncClearError();
  const typed = $('nc-path').value.trim();
  if (typed && typed !== ncBrowsePath) {
    try { await ncBrowse(typed); } catch (e) { ncShowError(e.message); return; }
  }
  if (!ncBrowsePath) { ncShowError('Pick a working directory first'); return; }
  const name = $('nc-name').value.trim().replace(/[–—]/g, '-');
  const title = $('nc-display').value.trim();
  const create = $('nc-create');
  create.classList.add('is-busy');
  try {
    const data = await api('POST', '/api/tmux/new', {name: name || null, cwd: ncBrowsePath, command: ncTool, title: title || null});
    ncClose();
    toast(`Started ${data.title || data.name}`, 'success');
    await openRawTmux(data.name);
  } catch (e) {
    ncShowError(e.message);
  } finally {
    create.classList.remove('is-busy');
  }
}
document.querySelectorAll('#nc-tools .choice').forEach(el => el.addEventListener('click', () => ncSetTool(el.dataset.tool)));
$('nc-cancel').addEventListener('click', ncClose);
$('nc-create').addEventListener('click', ncCreate);
$('nc-path').addEventListener('keydown', ev => { if (ev.key === 'Enter') { ev.preventDefault(); ncBrowse($('nc-path').value.trim()).catch(e => ncShowError(e.message)); } });
['nc-name', 'nc-display'].forEach(id => $(id).addEventListener('keydown', ev => { if (ev.key === 'Enter') { ev.preventDefault(); ncCreate(); } }));
$('modal-new').addEventListener('click', ev => { if (ev.target.id === 'modal-new') ncClose(); });

/* ------------------------------------------------------------------ */
/* Keyboard                                                            */
/* ------------------------------------------------------------------ */
document.addEventListener('keydown', event => {
  if (event.key === 'Escape') {
    if (!$('modal-new').hidden) { ncClose(); return; }
    if (Array.from(document.querySelectorAll('.popover')).some(p => !p.hidden)) { closePopovers(); return; }
    if (filePreviewOpen()) { closeFilePreview(); return; }
    if (inspectorSid) { closeInspector(); return; }
    return;
  }
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {
    event.preventDefault();
    if (mobileViewport.matches) setMobileMode('sessions');
    else if (sidebarHidden()) setSidebarHidden(false);
    $('session-search').focus();
    $('session-search').select();
  }
});

/* ------------------------------------------------------------------ */
/* Init                                                                */
/* ------------------------------------------------------------------ */
rebuildGroups();
applyFilters();
setSidebarHidden(sidebarHidden());
renderNeedsAction();
renderThemeOptions();
renderWorkspaceTabs();
refreshOrphanCount();
setInterval(refreshOrphanCount, 15000);
setInterval(loadBareTmux, 15000);
setInterval(reconcileRows, 15000);

const pageParams = new URLSearchParams(location.search);
const requestedSid = pageParams.get('session');
const restoredSid = requestedSid || localStorage.getItem(ACTIVE_TAB_KEY);
if (restoredSid && restoredSid.startsWith(RAW)) {
  openRawTmux(restoredSid.slice(RAW.length));
} else {
  const candidates = allRows().filter(c => c.dataset.tmux && c.dataset.status !== 'stopped').sort((a, b) => rankOf(a) - rankOf(b));
  const initialCard = cardFor(restoredSid) || candidates[0];
  if (initialCard && (!mobileViewport.matches || requestedSid)) previewSession(initialCard);
  else setMobileMode('sessions');
  loadBareTmux();
}
