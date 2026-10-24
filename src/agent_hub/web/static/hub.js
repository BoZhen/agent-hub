/* Agent Hub — shared browser helpers (theme, WebSocket, toasts, dialogs). */
window.AgentHub = (() => {
  const THEMES = [
    {id:'graphite', name:'Graphite', tone:'dark', swatches:['#0b0f14','#18212b','#5cc8ff']},
    {id:'midnight', name:'Midnight', tone:'dark', swatches:['#080b18','#18213a','#7aa2f7']},
    {id:'nord', name:'Nord', tone:'dark', swatches:['#242933','#3b4252','#88c0d0']},
    {id:'tokyo', name:'Tokyo Night', tone:'dark', swatches:['#101017','#24283b','#7aa2f7']},
    {id:'mocha', name:'Mocha', tone:'dark', swatches:['#11111b','#1e1e2e','#89b4fa']},
    {id:'dracula', name:'Dracula', tone:'dark', swatches:['#21222c','#282a36','#bd93f9']},
    {id:'paper', name:'Paper', tone:'light', swatches:['#f4f6f8','#ffffff','#1677b8']},
    {id:'solarized', name:'Solarized', tone:'light', swatches:['#fdf6e3','#eee8d5','#268bd2']},
    {id:'one-light', name:'One Light', tone:'light', swatches:['#f4f4f5','#ffffff','#4078f2']},
    {id:'rose-dawn', name:'Rosé Dawn', tone:'light', swatches:['#faf4ed','#f2e9e1','#286983']},
    {id:'latte', name:'Latte', tone:'light', swatches:['#eff1f5','#e6e9ef','#1e66f5']},
    {id:'gruvbox-light', name:'Gruvbox Light', tone:'light', swatches:['#f2e5bc','#fbf1c7','#076678']},
  ];

  function currentTheme() { return document.documentElement.dataset.theme || 'graphite'; }
  function setTheme(id, persist = true) {
    if (!THEMES.some(t => t.id === id)) return;
    document.documentElement.dataset.theme = id;
    if (persist) localStorage.setItem('agentHubTheme', id);
    window.dispatchEvent(new CustomEvent('agenthub:theme', {detail: {theme: id}}));
  }
  function themeName(id) { return (THEMES.find(t => t.id === id) || THEMES[0]).name; }
  /* opencode and pi do not follow a theme change by themselves inside
     tmux: ask the Hub to tell them to look at the terminal's colours again
     (POST /api/theme), once the terminals have reported the new colours to
     tmux. Without ids, every running opencode and pi session. */
  function noticeTheme(sessionIds) {
    const tone = (THEMES.find(t => t.id === currentTheme()) || THEMES[0]).tone;
    setTimeout(() => api('POST', '/api/theme', {tone, session_ids: sessionIds || null}).catch(() => {}), 500);
  }

  function escapeHtml(s) {
    return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  }
  function icon(name, cls = 'icon') {
    return `<svg class="${cls}" aria-hidden="true"><use href="#i-${name}"/></svg>`;
  }

  /* A small Markdown subset for agent replies (Monitor chat, phone page):
     fenced code, tables, headings, lists, quotes, paragraphs, inline code,
     bold and italics. Line based, and everything is escaped first, so
     nothing inside a code block is reformatted and no HTML gets through. */
  const LIST_RE = /^\s*([-*+]|\d+[.)])\s+/;
  const TABLE_RE = /^\s*\|.*\|\s*$/;
  function renderMarkdown(text) {
    if (!text) return '';
    const lines = String(text).replace(/\u0000/g, '').replace(/\r\n?/g, '\n').split('\n');
    const inline = s => {
      const codes = [];
      let h = escapeHtml(s).replace(/`([^`]+)`/g, (_, c) => `\u0000${codes.push(c) - 1}\u0000`);
      h = h.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
           .replace(/(^|[^*\w])\*([^*\s][^*]*)\*/g, '$1<em>$2</em>');
      return h.replace(/\u0000(\d+)\u0000/g, (_, n) => `<code>${codes[+n]}</code>`);
    };
    const startsBlock = line => /^\s*```/.test(line) || TABLE_RE.test(line) || /^#{1,6}\s/.test(line) || LIST_RE.test(line) || /^\s*>/.test(line);
    const table = rows => {
      const cells = row => row.replace(/^\s*\||\|\s*$/g, '').split('|').map(c => c.trim());
      if (rows.length < 2 || !/^[\s|:-]+$/.test(rows[1])) return `<pre><code>${escapeHtml(rows.join('\n'))}</code></pre>`;
      const head = cells(rows[0]).map(c => `<th>${inline(c)}</th>`).join('');
      const body = rows.slice(2).map(r => `<tr>${cells(r).map(c => `<td>${inline(c)}</td>`).join('')}</tr>`).join('');
      return `<div class="md-table"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
    };
    const out = [];
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      if (/^\s*```/.test(line)) {
        const body = [];
        for (i++; i < lines.length && !/^\s*```/.test(lines[i]); i++) body.push(lines[i]);
        i++;
        out.push(`<pre><code>${escapeHtml(body.join('\n'))}</code></pre>`);
      } else if (TABLE_RE.test(line)) {
        const rows = [];
        for (; i < lines.length && TABLE_RE.test(lines[i]); i++) rows.push(lines[i]);
        out.push(table(rows));
      } else if (/^#{1,6}\s/.test(line)) {
        out.push(`<p class="md-heading">${inline(line.replace(/^#{1,6}\s+/, ''))}</p>`);
        i++;
      } else if (LIST_RE.test(line)) {
        const ordered = /^\s*\d/.test(line);
        const items = [];
        for (; i < lines.length && LIST_RE.test(lines[i]); i++) items.push(`<li>${inline(lines[i].replace(LIST_RE, ''))}</li>`);
        out.push(ordered ? `<ol>${items.join('')}</ol>` : `<ul>${items.join('')}</ul>`);
      } else if (/^\s*>/.test(line)) {
        const quote = [];
        for (; i < lines.length && /^\s*>/.test(lines[i]); i++) quote.push(inline(lines[i].replace(/^\s*>\s?/, '')));
        out.push(`<blockquote>${quote.join('<br>')}</blockquote>`);
      } else if (!line.trim()) {
        i++;
      } else {
        const para = [];
        for (; i < lines.length && lines[i].trim() && !startsBlock(lines[i]); i++) para.push(inline(lines[i]));
        out.push(`<p>${para.join('<br>')}</p>`);
      }
    }
    return out.join('');
  }

  function toast(message, kind = '', ms = 2800) {
    const host = document.getElementById('toasts');
    if (!host) return;
    const el = document.createElement('div');
    el.className = 'toast' + (kind ? ' ' + kind : '');
    el.textContent = message;
    host.appendChild(el);
    setTimeout(() => el.remove(), ms);
  }

  async function api(method, url, body) {
    const opts = {method, headers: {}};
    if (body !== undefined) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    const res = await fetch(url, opts);
    let data = null;
    try { data = await res.json(); } catch (_) { data = null; }
    if (!res.ok) {
      const detail = (data && (data.detail || data.error)) || res.statusText || 'Request failed';
      throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
    }
    return data;
  }

  /* Small confirm dialog that resolves true/false. Replaces window.confirm. */
  function confirmDialog({title, body = '', confirmLabel = 'Confirm', danger = false}) {
    return new Promise(resolve => {
      const backdrop = document.createElement('div');
      backdrop.className = 'modal-backdrop';
      backdrop.innerHTML = `
        <div class="modal confirm" role="dialog" aria-modal="true">
          <h2>${escapeHtml(title)}</h2>
          ${body ? `<p>${escapeHtml(body)}</p>` : ''}
          <div class="modal-foot">
            <button type="button" class="btn" data-cancel>Cancel</button>
            <button type="button" class="btn ${danger ? 'btn-danger' : 'btn-primary'}" data-ok>${escapeHtml(confirmLabel)}</button>
          </div>
        </div>`;
      const done = value => { backdrop.remove(); document.removeEventListener('keydown', onKey); resolve(value); };
      const onKey = e => { if (e.key === 'Escape') done(false); };
      backdrop.querySelector('[data-cancel]').addEventListener('click', () => done(false));
      backdrop.querySelector('[data-ok]').addEventListener('click', () => done(true));
      backdrop.addEventListener('click', e => { if (e.target === backdrop) done(false); });
      document.addEventListener('keydown', onKey);
      document.body.appendChild(backdrop);
      backdrop.querySelector('[data-ok]').focus();
    });
  }

  /* Single-field prompt. Resolves the trimmed string, or null on cancel. */
  function promptDialog({title, label = '', value = '', placeholder = '', confirmLabel = 'Save'}) {
    return new Promise(resolve => {
      const backdrop = document.createElement('div');
      backdrop.className = 'modal-backdrop';
      backdrop.innerHTML = `
        <div class="modal confirm" role="dialog" aria-modal="true">
          <h2>${escapeHtml(title)}</h2>
          <div class="field">${label ? `<label for="prompt-input">${escapeHtml(label)}</label>` : ''}<input class="input" id="prompt-input" type="text" value="${escapeHtml(value)}" placeholder="${escapeHtml(placeholder)}" autocomplete="off"></div>
          <div class="modal-foot">
            <button type="button" class="btn" data-cancel>Cancel</button>
            <button type="button" class="btn btn-primary" data-ok>${escapeHtml(confirmLabel)}</button>
          </div>
        </div>`;
      const input = backdrop.querySelector('#prompt-input');
      const done = result => { backdrop.remove(); document.removeEventListener('keydown', onKey); resolve(result); };
      const onKey = e => { if (e.key === 'Escape') done(null); };
      backdrop.querySelector('[data-cancel]').addEventListener('click', () => done(null));
      backdrop.querySelector('[data-ok]').addEventListener('click', () => done(input.value.trim()));
      input.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); done(input.value.trim()); } });
      backdrop.addEventListener('click', e => { if (e.target === backdrop) done(null); });
      document.addEventListener('keydown', onKey);
      document.body.appendChild(backdrop);
      input.focus();
      input.select();
    });
  }

  /* Restart a running session's CLI so it runs a newly installed version,
     after the user confirms. The Hub respawns the agent's pane with the
     resume command, so the conversation goes on in the same tmux and an
     open terminal stays on it. A session in the middle of a turn asks once
     more. Resolves true when restarted, false when the user cancelled. */
  async function restartSession(sid, name) {
    const ok = await confirmDialog({title: `Restart ${name}?`, body: 'The CLI exits and resumes this conversation in the same tmux, so it runs the version installed now. Text typed but not sent is lost.', confirmLabel: 'Restart'});
    if (!ok) return false;
    const url = `/api/sessions/${encodeURIComponent(sid)}/restart`;
    try {
      await api('POST', url);
    } catch (e) {
      if (!/middle of a turn/.test(e.message)) throw e;
      const force = await confirmDialog({title: `${name} is working`, body: 'Restarting now stops the current turn. The conversation so far is kept.', confirmLabel: 'Restart anyway', danger: true});
      if (!force) return false;
      await api('POST', `${url}?force=true`);
    }
    return true;
  }

  function copyText(text) {
    if (navigator.clipboard?.writeText) return navigator.clipboard.writeText(text);
    const ta = document.createElement('textarea');
    ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta); ta.select();
    try { document.execCommand('copy'); } catch (_) {}
    ta.remove();
    return Promise.resolve();
  }

  /* Hub WebSocket: reconnects with a capped backoff, shows #ws-conn
     ("Reconnecting") only while the connection is down, forwards messages
     to window.onWsMessage. */
  let ws = null, retryDelay = 1500;
  function setConn(state) {
    const conn = document.getElementById('ws-conn');
    if (conn) conn.hidden = state !== 'down';
  }
  function connectWS() {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    setConn('connecting');
    ws = new WebSocket(`${proto}//${location.host}/ws`);
    ws.onopen = () => { retryDelay = 1500; setConn('live'); window.dispatchEvent(new CustomEvent('agenthub:ws-open')); };
    ws.onclose = () => {
      setConn('down');
      setTimeout(connectWS, retryDelay);
      retryDelay = Math.min(retryDelay * 2, 15000);
    };
    ws.onmessage = e => {
      let data;
      try { data = JSON.parse(e.data); } catch (_) { return; }
      if (typeof window.onWsMessage === 'function') window.onWsMessage(data);
    };
  }
  if (document.getElementById('ws-dot') || document.body.dataset.ws !== 'off') connectWS();

  return {THEMES, currentTheme, setTheme, themeName, noticeTheme, escapeHtml, icon, renderMarkdown, toast, api, confirmDialog, promptDialog, restartSession, copyText};
})();
