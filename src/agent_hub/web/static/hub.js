/* Agent Hub — shared browser helpers (theme, WebSocket, toasts, dialogs). */
window.AgentHub = (() => {
  const THEMES = [
    {id:'graphite', name:'Graphite', tone:'dark', swatches:['#0b0f14','#18212b','#5cc8ff']},
    {id:'midnight', name:'Midnight', tone:'dark', swatches:['#080b18','#18213a','#7aa2f7']},
    {id:'nord', name:'Nord', tone:'dark', swatches:['#242933','#3b4252','#88c0d0']},
    {id:'tokyo', name:'Tokyo Night', tone:'dark', swatches:['#101017','#24283b','#7aa2f7']},
    {id:'mocha', name:'Mocha', tone:'dark', swatches:['#11111b','#1e1e2e','#89b4fa']},
    {id:'paper', name:'Paper', tone:'light', swatches:['#f4f6f8','#ffffff','#1677b8']},
    {id:'solarized', name:'Solarized', tone:'light', swatches:['#fdf6e3','#eee8d5','#268bd2']},
    {id:'one-light', name:'One Light', tone:'light', swatches:['#f4f4f5','#ffffff','#4078f2']},
    {id:'rose-dawn', name:'Rosé Dawn', tone:'light', swatches:['#faf4ed','#f2e9e1','#286983']},
    {id:'latte', name:'Latte', tone:'light', swatches:['#eff1f5','#e6e9ef','#1e66f5']},
  ];

  function currentTheme() { return document.documentElement.dataset.theme || 'graphite'; }
  function setTheme(id, persist = true) {
    if (!THEMES.some(t => t.id === id)) return;
    document.documentElement.dataset.theme = id;
    if (persist) localStorage.setItem('agentHubTheme', id);
    window.dispatchEvent(new CustomEvent('agenthub:theme', {detail: {theme: id}}));
  }
  function themeName(id) { return (THEMES.find(t => t.id === id) || THEMES[0]).name; }

  function escapeHtml(s) {
    return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  }
  function icon(name, cls = 'icon') {
    return `<svg class="${cls}" aria-hidden="true"><use href="#i-${name}"/></svg>`;
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

  function copyText(text) {
    if (navigator.clipboard?.writeText) return navigator.clipboard.writeText(text);
    const ta = document.createElement('textarea');
    ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta); ta.select();
    try { document.execCommand('copy'); } catch (_) {}
    ta.remove();
    return Promise.resolve();
  }

  /* Hub WebSocket: reconnects with a capped backoff, mirrors state into
     #ws-dot / #ws-label, forwards messages to window.onWsMessage. */
  let ws = null, retryDelay = 1500;
  function setConn(state) {
    const dot = document.getElementById('ws-dot');
    const label = document.getElementById('ws-label');
    if (dot) dot.className = 'dot ' + (state === 'live' ? 'dot-active' : state === 'connecting' ? 'dot-idle' : 'dot-danger');
    if (label) label.textContent = state === 'live' ? 'Live' : state === 'connecting' ? 'Connecting' : 'Reconnecting';
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

  return {THEMES, currentTheme, setTheme, themeName, escapeHtml, icon, toast, api, confirmDialog, promptDialog, copyText};
})();
