/* Perseus dashboard client. No framework, no build step.
   Every piece of model or web text reaches the page as a text node (never innerHTML). */
'use strict';

(() => {
  const $ = (id) => document.getElementById(id);
  const app = $('app');

  // ------------------------------------------------------------------ small helpers
  const store = {
    get(k, d) { try { const v = localStorage.getItem('perseus.' + k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } },
    set(k, v) { try { localStorage.setItem('perseus.' + k, JSON.stringify(v)); } catch (e) { /* private mode */ } },
  };
  const iconTpl = $('icons').content;
  const icon = (name) => iconTpl.querySelector(`[data-i="${name}"]`).cloneNode(true);

  function h(tag, attrs, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === 'class') el.className = v;
      else if (k === 'text') el.textContent = v;
      else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? '' : String(v));
    }
    for (const kid of kids.flat()) {
      if (kid === null || kid === undefined || kid === false) continue;
      el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
    }
    return el;
  }

  const clock = (t) => new Date(t * 1000).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  const kb = (n) => n >= 1024 * 1024 ? (n / 1048576).toFixed(1) + ' MB' : Math.max(1, Math.round(n / 1024)) + ' KB';
  const secs = (ms) => (ms / 1000).toFixed(1) + ' s';
  const tokensLabel = (n) => n >= 1000 ? (n / 1000).toFixed(n >= 10000 ? 0 : 1) + 'K' : String(n);
  const ctxLabel = (n) => n >= 1024 ? Math.round(n / 1024) + 'K' : String(n);
  const cap = (s) => s.charAt(0).toUpperCase() + s.slice(1);

  async function api(method, path, body) {
    const r = await fetch(path, {
      method, credentials: 'same-origin',
      headers: { 'X-Perseus': '1', ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}) },
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
    if (r.status === 401) { location.reload(); throw new Error('signed out'); }
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || r.statusText);
    return data;
  }

  // ------------------------------------------------------------------ state
  const ui = {
    theme: store.get('theme', matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'),
    showThinking: store.get('thought', true),
    feedOpen: store.get('feed', true),
    view: 'chat',
    server: null,            // /api/state
    list: [],                // chat summaries, newest first
    chats: {},               // id -> full chat
    activeId: null,
    streams: {},             // chat id -> { controller }
    closing: {},             // chat id -> true while the model writes its closed-out answer
    feedOpenTurns: {},       // chat id -> Set of earlier question numbers opened in the feed
    feedCleared: store.get('feedCleared', {}),   // chat id -> last event id hidden by Clear (display only)
    thinkOpen: {},           // message id -> bool
    fileOpen: {},            // chat id + file id -> preview open
    fileText: {},            // chat id + file id -> fetched content (or { error })
    thinkFollow: {},         // message id -> false once the reader scrolls up in a streaming thinking box
    menuOpen: false,
    attachments: [],
    pending: null,           // { context, creativity, length } chosen in settings, not yet applied
    freshFeed: new Set(),
  };

  const active = () => (ui.activeId && ui.chats[ui.activeId]) || null;
  const settings = () => (ui.server ? ui.server.settings : {});
  const info = () => (ui.server ? ui.server.info : {});

  // ------------------------------------------------------------------ markdown (safe: DOM nodes only)
  function inline(text) {
    const out = [];
    const re = /(`[^`\n]+`)|(\*\*[^*\n]+\*\*|__[^_\n]+__)|(\*[^*\s][^*\n]*\*|_[^_\s][^_\n]*_)|(\[[^\]\n]+\]\((https?:\/\/[^\s)]+)\))|(https?:\/\/[^\s<>()]+[^\s<>().,;:!?'"])/g;
    let last = 0, m;
    while ((m = re.exec(text))) {
      if (m.index > last) out.push(text.slice(last, m.index));
      if (m[1]) out.push(h('code', { text: m[1].slice(1, -1) }));
      else if (m[2]) out.push(h('strong', {}, inline(m[2].slice(2, -2))));
      else if (m[3]) out.push(h('em', {}, inline(m[3].slice(1, -1))));
      else if (m[4]) out.push(link(m[4].slice(1, m[4].indexOf('](')), m[5]));
      else if (m[6]) out.push(link(m[6], m[6]));
      last = re.lastIndex;
    }
    if (last < text.length) out.push(text.slice(last));
    return out;
  }
  const link = (label, href) => h('a', { href, target: '_blank', rel: 'noopener noreferrer nofollow' }, label);

  // A fenced code block with a Copy button in its top-right corner.
  function codeBlock(text, lang) {
    const label = h('span', { class: 'copy-label', text: 'Copy' });
    const btn = h('button', { type: 'button', class: 'code-copy', 'aria-label': 'Copy code' }, icon('copy'), label);
    btn.addEventListener('click', async () => {
      const ok = await copyText(text);
      btn.replaceChildren(icon(ok ? 'check' : 'x'), h('span', { class: 'copy-label', text: ok ? 'Copied' : 'Copy failed' }));
      btn.classList.toggle('done', ok);
      clearTimeout(btn._t);
      btn._t = setTimeout(() => { btn.replaceChildren(icon('copy'), h('span', { class: 'copy-label', text: 'Copy' })); btn.classList.remove('done'); }, 1600);
    });
    return h('div', { class: 'code-wrap' },
      h('pre', { class: 'code', 'aria-label': lang ? lang + ' code' : 'code' }, text), btn);
  }
  async function copyText(text) {
    try {
      if (navigator.clipboard && window.isSecureContext) { await navigator.clipboard.writeText(text); return true; }
    } catch (e) { /* fall through to the older way */ }
    const ta = h('textarea', { 'aria-hidden': 'true', readonly: true });
    ta.value = text;
    ta.style.cssText = 'position:fixed;top:0;left:0;width:1px;height:1px;opacity:0';
    document.body.append(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
    ta.remove();
    return ok;
  }

  function markdown(src, streaming) {
    const blocks = [];
    const lines = (src || '').replace(/\r/g, '').split('\n');
    let para = [];
    const flush = () => { if (para.length) { blocks.push(h('p', {}, inline(para.join(' ')))); para = []; } };
    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];
      const fence = line.match(/^\s*(```|~~~)\s*([\w+-]*)/);
      if (fence) {
        flush();
        const body = [];
        i++;
        while (i < lines.length && !lines[i].trim().startsWith(fence[1])) body.push(lines[i++]);
        blocks.push(codeBlock(body.join('\n'), fence[2]));
        continue;
      }
      if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}/.test(lines[i + 1])) {
        flush();
        const rows = [];
        while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) rows.push(lines[i++]);
        i--;
        const cells = (r) => r.trim().replace(/^\||\|$/g, '').split('|').map((c) => c.trim());
        const head = cells(rows[0]);
        const body = rows.slice(2).map(cells);
        blocks.push(h('div', { class: 'md-table' }, h('table', {},
          h('thead', {}, h('tr', {}, head.map((c) => h('th', {}, inline(c))))),
          h('tbody', {}, body.map((r) => h('tr', {}, r.map((c) => h('td', {}, inline(c)))))))));
        continue;
      }
      const hd = line.match(/^\s*#{1,6}\s+(.*)/);
      if (hd) { flush(); blocks.push(h('h3', {}, inline(hd[1]))); continue; }
      const ul = line.match(/^\s*[-*+]\s+(.*)/);
      if (ul) { flush(); blocks.push(h('div', { class: 'li' }, h('span', { class: 'li-mark', 'aria-hidden': 'true' }, '•'), h('span', {}, inline(ul[1])))); continue; }
      const ol = line.match(/^\s*(\d+)[.)]\s+(.*)/);
      if (ol) { flush(); blocks.push(h('div', { class: 'li' }, h('span', { class: 'li-mark num' }, ol[1] + '.'), h('span', {}, inline(ol[2])))); continue; }
      const bq = line.match(/^\s*>\s?(.*)/);
      if (bq) { flush(); blocks.push(h('blockquote', {}, inline(bq[1]))); continue; }
      if (/^\s*([-*_])\s*\1\s*\1[\s\1]*$/.test(line)) { flush(); continue; }
      if (!line.trim()) { flush(); continue; }
      para.push(line.trim());
    }
    flush();
    if (streaming) {
      const cursor = h('span', { class: 'cursor', 'aria-hidden': 'true' });
      const lastBlock = blocks[blocks.length - 1];
      if (lastBlock && lastBlock.tagName !== 'PRE' && lastBlock.tagName !== 'DIV') lastBlock.append(cursor);
      else blocks.push(h('p', {}, cursor));
    }
    return blocks;
  }

  // ------------------------------------------------------------------ theme and layout
  function applyTheme() {
    app.classList.toggle('pz-dark', ui.theme === 'dark');
    app.classList.toggle('pz-light', ui.theme !== 'dark');
    document.body.classList.toggle('pz-dark-body', ui.theme === 'dark');
    $('raw').className = 'raw ' + (ui.theme === 'dark' ? 'pz-dark' : 'pz-light');
    $('theme-light').setAttribute('aria-pressed', String(ui.theme !== 'dark'));
    $('theme-dark').setAttribute('aria-pressed', String(ui.theme === 'dark'));
  }

  function relayout() {
    const feedVisible = ui.feedOpen && ui.view === 'chat';
    const need = 300 + 560 + (feedVisible ? 340 : 0);
    app.classList.toggle('fixed', window.innerWidth >= need);
  }

  // ------------------------------------------------------------------ sidebar
  function toggleDefs() {
    const s = settings();
    return [
      { id: 'internet', label: 'Internet Access', icon: 'globe', on: !!s.internet },
      { id: 'thought', label: 'Show Thinking', icon: 'eye', on: ui.showThinking },
      { id: 'sandbox', label: 'Sandbox Mode', icon: 'flask', on: false, disabled: true, sub: 'Coming in a later release' },
    ];
  }

  async function flipToggle(id) {
    if (id === 'thought') {
      ui.showThinking = !ui.showThinking;
      store.set('thought', ui.showThinking);
      renderToggles();
      renderMessages();
      return;
    }
    if (id === 'internet') await patchSettings({ internet: !settings().internet });
  }

  function renderToggles() {
    const make = (cls) => toggleDefs().map((d) => h('button', {
      type: 'button', role: 'switch', class: cls, 'aria-checked': String(d.on), disabled: d.disabled,
      title: d.sub || null, onclick: () => flipToggle(d.id),
    },
    cls === 'toggle' ? h('span', { class: 'toggle-icon' }, icon(d.icon)) : null,
    h('span', { class: 'toggle-label' }, d.label, cls === 'toggle' && d.sub ? h('span', { class: 'toggle-sub' }, d.sub) : null),
    h('span', { class: 'switch', 'aria-hidden': 'true' })));
    $('side-toggles').replaceChildren(...make('toggle'));
    $('settings-toggles').replaceChildren(...make('toggle settings-toggle'));
  }

  function renderChatList() {
    const rows = ui.list.map((c) => {
      const on = c.id === ui.activeId;
      return h('div', { class: 'chat-row' + (on ? ' active' : '') },
        h('button', { type: 'button', class: 'chat-pick', 'aria-current': on ? 'true' : null, title: c.title,
          onclick: () => openChat(c.id) }, c.title),
        c.busy ? h('span', { class: 'chat-busy', title: 'Answering…' }) : null,
        on ? h('button', { type: 'button', class: 'chat-del', 'aria-label': 'Delete chat', onclick: () => deleteChat(c.id) }, icon('x')) : null);
    });
    $('chat-list').replaceChildren(...(rows.length ? rows : [h('p', { class: 'empty-chats', text: 'No chats yet.' })]));
  }

  function statusOf() {
    const st = ui.server && ui.server.status;
    if (!ui.server) return { cls: '', short: 'Connecting…', long: 'Connecting to Perseus…' };
    if (ui.server.restarting) return { cls: 'restarting', short: 'Restarting…', long: 'Model server restarting' };
    const v = st && (st.vLLM || st.vllm);
    const state = v ? v.state : 'up';
    if (state === 'up') return { cls: 'up', short: 'Running', long: 'Model server running' };
    if (state === 'restarting' || state === 'starting') return { cls: 'restarting', short: 'Restarting…', long: 'Model server restarting' };
    return { cls: 'down', short: 'Offline', long: 'Model server offline' + (v && v.reason ? ': ' + v.reason : '') };
  }

  function renderStatus() {
    const s = statusOf();
    $('status-dot').className = 'status-dot ' + s.cls;
    $('status-long').textContent = s.long;
    $('ctx-size').textContent = ui.server ? ctxLabel(info().context) : '';
    $('server-dot').className = 'pill-dot ' + s.cls;
    $('server-label').textContent = s.short;
  }

  // ------------------------------------------------------------------ header
  function renderHeader() {
    const inSettings = ui.view === 'settings';
    const inChat = ui.view === 'chat';
    const c = active();
    $('header-title').textContent = inSettings ? 'Settings' : ui.view === 'pipelines' ? 'Development Pipelines'
      : ui.view === 'tools' ? 'Tools' : (c ? c.title : 'New chat');
    $('chat-controls').hidden = !inChat;
    const gear = $('gear');
    gear.setAttribute('aria-pressed', String(inSettings));
    gear.setAttribute('aria-label', inSettings ? 'Close settings' : 'Open settings');
    $('show-feed').hidden = ui.feedOpen || !inChat;

    const s = settings();
    const pill = $('internet-pill');
    pill.setAttribute('aria-pressed', String(!!s.internet));
    pill.querySelector('.pill-label').textContent = s.internet ? 'Internet On' : 'Internet Off';

    const thinks = !!info().thinks;
    $('think-wrap').hidden = !thinks;
    $('think-label').textContent = 'Thinking: ' + cap(s.thinking || 'medium');
    $('think-btn').setAttribute('aria-expanded', String(ui.menuOpen));
    const menu = $('think-menu');
    menu.hidden = !ui.menuOpen;
    if (ui.menuOpen) {
      menu.replaceChildren(...(ui.server.thinking || []).map((lvl) => {
        const on = s.thinking === lvl;
        return h('button', { type: 'button', role: 'menuitemradio', class: 'menu-item', 'aria-checked': String(on),
          onclick: () => { ui.menuOpen = false; patchSettings({ thinking: lvl }); } }, cap(lvl), on ? icon('check') : null);
      }));
    }
    renderRing();
  }

  function renderRing() {
    const c = active();
    const total = info().context || 1;
    const used = (c && c.ctxUsed) || 0;
    const pct = Math.min(1, used / total);
    const label = Math.round(pct * 100) + '%';
    $('ring-fill').style.strokeDashoffset = (113.1 * (1 - pct)).toFixed(2);
    $('ring-fill').classList.toggle('hot', pct >= 0.8);
    $('ctx-pct').textContent = label;
    const aria = `Context used: ${label} of ${ctxLabel(total)}` + (used ? ` (${used.toLocaleString()} tokens)` : '');
    $('ctx-ring').setAttribute('aria-label', aria);
    $('ctx-ring').title = aria;
  }

  // ------------------------------------------------------------------ messages
  function renderMessages() {
    const c = active();
    const box = $('messages');
    if (!c || !c.messages.length) {
      box.replaceChildren(h('div', { class: 'hero' },
        h('img', { src: '/assets/logo-full.svg', alt: 'Perseus — Weapon of Clarity' }),
        ui.server ? h('p', { text: `${info().model} · ${info().hardware}` }) : null));
      return;
    }
    box.replaceChildren(...c.messages.map(messageNode));
    for (const m of c.messages) {
      if (!m.streaming) continue;
      const el = box.querySelector(`[data-id="${CSS.escape(m.id)}"]`);
      if (el) placeThinkBox(el, m, null);
    }
  }

  function messageNode(m) {
    if (m.role === 'user') return h('div', { class: 'msg-user', 'data-id': m.id }, h('div', { class: 'bubble' }, m.text));
    const hasReasoning = !!(m.reasoning && m.reasoning.length);
    // phase comes from the server: thinking | answering | tools | closing (every step, not only the first)
    const phase = m.streaming ? (m.phase || (m.content ? 'answering' : 'thinking')) : null;
    const stillThinking = phase === 'thinking';
    const wrapping = phase === 'closing';
    const showThink = ui.showThinking && hasReasoning;
    const open = !!ui.thinkOpen[m.id];
    const kids = [];
    if (showThink) {
      const used = m.think_used || 0, budget = m.think_budget || 0;
      const count = budget ? `${tokensLabel(used)} / ${tokensLabel(budget)}` : (used ? `${tokensLabel(used)} tokens` : '');
      const status = wrapping ? (m.content ? 'Writing final answer…' : 'Wrapping up…')
        : stillThinking ? 'Thinking…' + (count ? ' ' + count : '')
        : (m.think_secs != null ? `Thought for ${Math.max(1, Math.round(m.think_secs))}s` : '') + (used ? ` · ${tokensLabel(used)} tokens` : '');
      const frac = budget ? Math.min(1, used / budget) : 0;
      const meter = m.streaming && budget && phase !== 'answering'
        ? h('div', { class: 'think-meter' + (frac >= 0.8 ? ' near' : ''), role: 'progressbar', 'aria-label': 'Thinking budget used',
            'aria-valuemin': '0', 'aria-valuemax': String(budget), 'aria-valuenow': String(Math.min(used, budget)) },
            (() => { const f = h('div', { class: 'think-meter-fill' }); f.style.width = (frac * 100).toFixed(1) + '%'; return f; })())
        : null;   // width set from script: the CSP blocks style="" attributes
      const brain = icon('brain'); brain.classList.add('brain');
      const chev = icon('chevron'); chev.classList.add('chev');
      kids.push(h('div', { class: 'think' },
        h('button', { type: 'button', class: 'think-toggle', 'aria-expanded': String(open),
          onclick: () => { ui.thinkOpen[m.id] = !open; replaceMessage(m); } },
        brain, h('span', { class: 'think-title', text: 'Thinking Process' }), h('span', { class: 'think-status', text: status }), chev),
        meter,
        open ? thinkBox(m) : null));
    }
    if (!showThink && !m.content && (stillThinking || wrapping)) kids.push(h('p', { class: 'waiting', text: wrapping ? 'Wrapping up…' : 'Thinking…' }));
    if (phase === 'tools' && !m.content) kids.push(h('p', { class: 'waiting', text: 'Using tools…' }));
    kids.push(h('div', { class: 'answer' }, markdown(m.content, m.streaming && !!m.content)));
    if (m.files && m.files.length) kids.push(fileList(m));
    // later steps (after a web search) think again below text that is already shown
    if (m.content && (phase === 'tools' || (!showThink && (stillThinking || wrapping))))
      kids.push(h('p', { class: 'waiting', text: wrapping ? 'Wrapping up…' : phase === 'tools' ? 'Using tools…' : 'Thinking…' }));
    if (!m.streaming && !m.content && !m.error && !m.stopped && hasReasoning)
      kids.push(h('p', { class: 'note-warn', text: 'No answer came back; the thinking is above. Ask again, or raise Length or Thinking.' }));
    if (!m.streaming) kids.push(footer(m));
    return h('div', { class: 'msg-ai', 'data-id': m.id },
      h('span', { class: 'avatar' }, h('img', { src: '/assets/mark.png', alt: '' })),
      h('div', { class: 'ai-body' }, kids));
  }

  // The thinking box follows new text while it streams, unless the reader has scrolled up in it;
  // scrolling back to the bottom (or "Jump to latest") follows again.
  function thinkBox(m) {
    const box = h('div', { class: 'think-text', 'data-think': m.id }, m.reasoning);
    const live = m.streaming && m.phase !== 'answering';
    if (!live) return box;
    const jump = h('button', { type: 'button', class: 'think-jump', hidden: ui.thinkFollow[m.id] !== false,
      onclick: () => { ui.thinkFollow[m.id] = true; box.scrollTop = box.scrollHeight; jump.hidden = true; } }, 'Jump to latest ↓');
    box.addEventListener('scroll', () => {
      if (box.dataset.restoring) return;          // our own scroll, not the reader's
      const atEnd = box.scrollHeight - box.scrollTop - box.clientHeight < 24;
      ui.thinkFollow[m.id] = atEnd;
      jump.hidden = atEnd;
    });
    return h('div', { class: 'think-live' }, box, jump);
  }
  function placeThinkBox(el, m, prevTop) {
    const box = el.querySelector('.think-text');
    if (!box) return;
    box.dataset.restoring = '1';
    if (m.streaming && ui.thinkFollow[m.id] !== false) box.scrollTop = box.scrollHeight;
    else if (prevTop != null) box.scrollTop = prevTop;
    requestAnimationFrame(() => { delete box.dataset.restoring; });
  }

  // Files the model handed over with output_file: a card each, under the answer.
  // The content is fetched on demand and only ever shown as text (Preview) or downloaded.
  const fileUrl = (cid, f) => `/api/chats/${encodeURIComponent(cid)}/files/${encodeURIComponent(f.id)}`;
  async function fileContent(cid, f) {
    const key = cid + ':' + f.id;
    if (typeof ui.fileText[key] === 'string') return ui.fileText[key];
    const r = await fetch(fileUrl(cid, f), { credentials: 'same-origin' });
    if (!r.ok) throw new Error(r.status === 404 ? 'file not found' : r.statusText);
    return (ui.fileText[key] = await r.text());
  }
  function fileList(m) {
    const c = active();
    if (!c) return null;
    return h('div', { class: 'files' }, m.files.map((f) => fileCard(c.id, m, f)));
  }
  function fileCard(cid, m, f) {
    const key = cid + ':' + f.id;
    const open = !!ui.fileOpen[key];
    const meta = [f.language && f.language !== 'text' ? f.language : null,
      f.lines != null ? `${f.lines} line${f.lines === 1 ? '' : 's'}` : null,
      f.bytes != null ? (f.bytes < 1024 ? f.bytes + ' B' : kb(f.bytes)) : null].filter(Boolean).join(' · ');
    const copyBtn = h('button', { type: 'button', class: 'file-btn', 'aria-label': `Copy ${f.name}` }, icon('copy'), h('span', { text: 'Copy' }));
    copyBtn.addEventListener('click', async () => {
      let ok = false;
      try { ok = await copyText(await fileContent(cid, f)); } catch (e) { banner(e.message); }
      copyBtn.replaceChildren(icon(ok ? 'check' : 'x'), h('span', { text: ok ? 'Copied' : 'Copy failed' }));
      clearTimeout(copyBtn._t);
      copyBtn._t = setTimeout(() => copyBtn.replaceChildren(icon('copy'), h('span', { text: 'Copy' })), 1600);
    });
    const previewBtn = h('button', { type: 'button', class: 'file-btn', 'aria-expanded': String(open),
      'aria-label': `${open ? 'Hide' : 'Preview'} ${f.name}`,
      onclick: async () => {
        ui.fileOpen[key] = !open;
        if (!open && typeof ui.fileText[key] !== 'string') {
          try { await fileContent(cid, f); } catch (e) { ui.fileText[key] = { error: e.message }; }
        }
        replaceMessage(m);
      } }, icon('eye'), h('span', { text: open ? 'Hide' : 'Preview' }));
    const download = h('a', { class: 'file-btn', href: fileUrl(cid, f), download: f.name, 'aria-label': `Download ${f.name}` },
      icon('download'), h('span', { text: 'Download' }));
    const text = ui.fileText[key];
    const preview = !open ? null
      : typeof text === 'string' ? h('div', { class: 'file-preview' }, codeBlock(text, f.language))
      : text && text.error ? h('p', { class: 'note-warn', text: `Couldn't load the file: ${text.error}` })
      : h('p', { class: 'waiting', text: 'Loading…' });
    const fileIcon = icon('file'); fileIcon.classList.add('file-icon');
    return h('div', { class: 'file-card' },
      h('div', { class: 'file-head' },
        fileIcon,
        h('div', { class: 'file-info' },
          h('span', { class: 'file-name', text: f.name }),
          meta ? h('span', { class: 'file-meta', text: meta }) : null),
        h('div', { class: 'file-actions' }, previewBtn, copyBtn, download)),
      f.description ? h('p', { class: 'file-desc', text: f.description }) : null,
      preview);
  }

  function footer(m) {
    const bits = [];
    if (m.error) bits.push(h('span', { class: 'err', text: m.error }));
    if (m.stopped) bits.push(h('span', { text: 'Stopped' }));
    const closed = { user: 'Closed out', budget: 'Thinking budget reached · wrapped up',
      length: 'Ran out of room while thinking · wrapped up' }[m.closed];
    if (closed) bits.push(h('span', { class: 'warn', text: closed }));
    if (m.truncated) bits.push(h('span', { class: 'warn', text: 'Cut off at the length limit · try Length: Long' }));
    if (m.lookups) bits.push(h('span', { text: `${m.lookups} of ${m.limit} lookups` }));
    if (m.duration != null) bits.push(h('span', { text: `${m.duration.toFixed(1)} s` }));
    const u = m.usage || {};
    if (u.completion_tokens) bits.push(h('span', { text: `${tokensLabel(u.completion_tokens)} tokens out` }));
    bits.push(h('button', { type: 'button', class: 'link-btn', onclick: () => openRaw(m) }, 'View raw'));
    return h('div', { class: 'msg-foot' }, bits);
  }

  let rafPending = new Set();
  function replaceMessage(m) {
    rafPending.add(m.id);
    if (rafPending.size > 1) return;
    requestAnimationFrame(() => {
      const nearBottom = isNearBottom();
      const c = active();
      for (const id of rafPending) {
        const msg = c && c.messages.find((x) => x.id === id);
        const old = $('messages').querySelector(`[data-id="${CSS.escape(id)}"]`);
        if (msg && old) {
          const oldBox = old.querySelector('.think-text');
          const prevTop = oldBox ? oldBox.scrollTop : null;
          const el = messageNode(msg);
          old.replaceWith(el);
          placeThinkBox(el, msg, prevTop);
        }
      }
      rafPending = new Set();
      if (nearBottom) scrollToBottom();
    });
  }
  function isNearBottom() {
    const sc = app.classList.contains('fixed') ? $('scroller') : document.scrollingElement;
    return sc.scrollHeight - sc.scrollTop - sc.clientHeight < 120;
  }
  function scrollToBottom() {
    const sc = app.classList.contains('fixed') ? $('scroller') : document.scrollingElement;
    sc.scrollTop = sc.scrollHeight;
  }

  // ------------------------------------------------------------------ tool feed
  const STATUS = {
    ok: ['OK', ''], cached: ['Cached', ''], running: ['Running', 'run'], blocked: ['Blocked', 'bad'],
    refused: ['Refused', 'bad'], error: ['Error', 'warn'], stopped: ['Stopped', 'run'],
  };

  function feedDetail(ev) {
    if (ev.status === 'running') return 'running…';
    const parts = [];
    if (ev.lookup) parts.push('lookup ' + ev.lookup);
    if (ev.feed && ev.feed.detail) parts.push(ev.feed.detail);
    if (ev.http_status) parts.push(String(ev.http_status));
    if (ev.bytes) parts.push(kb(ev.bytes));
    if (ev.duration_ms != null) parts.push(secs(ev.duration_ms));
    return parts.join(' · ');
  }

  // Details: a tool's own readable sections (run_code: code, stdin, stdout, stderr), then the raw text
  // sent to the model; without sections, the raw arguments and result as before.
  function feedDetails(ev, argsText) {
    const f = ev.feed || {};
    if (f.sections && f.sections.length) {
      return h('details', { class: 'f-more' },
        h('summary', {}, 'Details'),
        ...f.sections.map((s) => [h('h4', { text: s.title }), h('pre', { class: 'f-code' }, s.text)]),
        ev.result ? h('details', { class: 'f-raw' },
          h('summary', {}, 'Raw text returned to the model'), h('pre', {}, ev.result)) : null);
    }
    return h('details', { class: 'f-more' },
      h('summary', {}, 'Details'),
      h('h4', { text: 'Arguments the model sent' }), h('pre', {}, argsText || '{}'),
      ev.result ? [h('h4', { text: 'Returned to the model' }), h('pre', {}, ev.result)] : null);
  }

  function feedCard(ev) {
    const f = ev.feed || {};
    // the tool's own badge ("Program failed") when the call itself went through
    let [label, tone] = STATUS[ev.status] || [ev.status, ''];
    if (ev.status === 'ok' && f.pill) [label, tone] = [f.pill, f.tone || ''];
    const bad = ev.status === 'blocked' || ev.status === 'refused';
    const ic = ev.tool === 'web_search' ? 'search' : ev.tool === 'fetch_url' ? 'fetch' : 'tool';
    const argsText = typeof ev.args === 'string' ? ev.args : JSON.stringify(ev.args, null, 2);
    return h('div', { class: 'fcard' + (bad ? ' bad' : '') + (ui.freshFeed.has(ev.id) ? ' fresh' : ''), 'data-ev': ev.id },
      h('div', { class: 'f-top' },
        h('span', { class: 'f-icon' }, icon(ic)),
        h('span', { class: 'f-tool', text: ev.tool }),
        h('span', { class: 'f-time', text: clock(ev.started) })),
      h('div', { class: 'f-arg', text: f.summary || ev.summary || '' }),
      f.headline ? h('div', { class: 'f-headline' + (tone ? ' ' + tone : ''), text: f.headline }) : null,
      ev.reason && ev.status !== 'ok' && ev.status !== 'cached' ? h('div', { class: 'f-reason', text: ev.reason }) : null,
      ev.warning ? h('div', { class: 'f-warning' }, icon('flag'), h('span', { text: ev.warning })) : null,
      h('div', { class: 'f-foot' },
        h('span', { class: 'chip' + (tone ? ' ' + tone : ''), text: label }),
        h('span', { class: 'f-detail', text: feedDetail(ev) })),
      ev.status !== 'running' ? feedDetails(ev, argsText) : null);
  }

  function renderFeed() {
    const visible = ui.feedOpen && ui.view === 'chat';
    $('feed').hidden = !visible;
    relayout();
    if (!visible) return;
    const c = active();
    const all = (c && c.feed) || [];
    const cleared = (c && ui.feedCleared[c.id]) || 0;     // Clear only hides; the chat and export keep everything
    const events = all.filter((e) => e.id > cleared);
    $('feed-count').textContent = String(events.length);
    // lookups in the latest answer
    const lastTurn = events.reduce((n, e) => Math.max(n, e.turn), 0);
    const used = events.filter((e) => e.turn === lastTurn && e.lookup).length;
    const lastMsg = c && [...c.messages].reverse().find((m) => m.role === 'assistant');
    const limit = (lastMsg && lastMsg.limit) || (ui.server ? ui.server.depths[settings().depth] : 20);
    $('lookups-label').textContent = events.length ? `${used} of ${limit} lookups in the last answer` : 'No lookups yet';
    $('lookups-fill').style.width = Math.min(100, Math.round((used / limit) * 100)) + '%';
    const bar = $('lookups-bar');
    bar.setAttribute('aria-valuemax', String(limit));
    bar.setAttribute('aria-valuenow', String(used));
    const list = $('feed-list');
    const openIds = new Set([...list.querySelectorAll('details.f-more[open]')].map((d) => d.closest('[data-ev]').dataset.ev));
    const hidden = all.length - events.length;
    $('clear-feed').hidden = !events.length;
    if (!events.length) {
      list.replaceChildren(h('p', { class: 'feed-empty' },
        hidden ? `Cleared ${hidden} tool call${hidden === 1 ? '' : 's'}. ` : 'No tool calls yet.',
        hidden ? h('button', { type: 'button', class: 'link-btn', onclick: () => setCleared(c, 0) }, 'Show them') : null));
      return;
    }
    // One group per question. The current question stays open; earlier ones fold up as soon as a new
    // question is sent (its number is on the answer the server created), and open again on a click.
    const current = Math.max(lastTurn, currentTurn(c));
    const turns = [...new Set(events.map((e) => e.turn))].sort((a, b) => b - a);
    const opened = ui.feedOpenTurns[c.id] || (ui.feedOpenTurns[c.id] = new Set());
    const card = (ev) => {
      const el = feedCard(ev);
      if (openIds.has(String(ev.id))) { const d = el.querySelector('details'); if (d) d.open = true; }
      return el;
    };
    list.replaceChildren(...turns.map((t) => {
      const evs = events.filter((e) => e.turn === t).reverse();
      if (t === current) return h('div', { class: 'fgroup-now' }, ...evs.map(card));
      const open = opened.has(t);
      const bad = evs.filter((e) => e.status === 'blocked' || e.status === 'refused' || e.status === 'error').length;
      const q = questionFor(c, t);
      return h('div', { class: 'fgroup' + (open ? ' open' : '') },
        h('button', { type: 'button', class: 'fgroup-head', 'aria-expanded': String(open),
          onclick: () => { open ? opened.delete(t) : opened.add(t); renderFeed(); } },
          h('span', { class: 'fgroup-q', text: q || 'Earlier question' }),
          h('span', { class: 'fgroup-n', text: `${evs.length} call${evs.length === 1 ? '' : 's'}` + (bad ? ` · ${bad} not OK` : '') }),
          (() => { const ch = icon('chevron'); ch.classList.add('chev'); return ch; })()),
        open ? h('div', { class: 'fgroup-body' }, ...evs.map(card)) : null);
    }));
  }

  // The question number of the answer running (or last run) in this chat: tool calls of earlier
  // questions fold up the moment a new one is sent, before its first tool call arrives.
  function currentTurn(c) {
    const m = c && [...c.messages].reverse().find((x) => x.role === 'assistant' && x.turn);
    return m ? m.turn : 0;
  }
  function questionFor(c, turn) {
    const i = c.messages.findIndex((x) => x.role === 'assistant' && x.turn === turn);
    const u = i > 0 && c.messages[i - 1].role === 'user' ? c.messages[i - 1].text : '';
    return u.replace(/\s+/g, ' ').trim().slice(0, 80);
  }
  function setCleared(c, upTo) {
    if (upTo) ui.feedCleared[c.id] = upTo; else delete ui.feedCleared[c.id];
    store.set('feedCleared', ui.feedCleared);
    renderFeed();
  }
  function clearFeed() {
    const c = active();
    if (!c || !c.feed || !c.feed.length) return;
    setCleared(c, c.feed.reduce((n, e) => Math.max(n, e.id), 0));
  }

  function upsertEvent(chat, ev) {
    const i = chat.feed.findIndex((e) => e.id === ev.id);
    if (i >= 0) chat.feed[i] = ev; else { chat.feed.push(ev); ui.freshFeed.add(ev.id); setTimeout(() => ui.freshFeed.delete(ev.id), 800); }
  }

  // ------------------------------------------------------------------ composer
  const composer = $('composer');
  function autosize() {
    composer.style.height = 'auto';
    composer.style.height = Math.min(240, composer.scrollHeight) + 'px';
  }
  function renderComposer() {
    const c = active();
    const streaming = !!(c && ui.streams[c.id]);
    const busy = !!(c && c.busy) || streaming;
    const send = $('send');
    send.classList.toggle('stop', busy);
    send.classList.toggle('ready', !busy && !!(composer.value.trim() || ui.attachments.length));
    send.setAttribute('aria-label', busy ? 'Stop answering' : 'Send message');
    const close = $('closeout');
    close.hidden = !busy;
    if (busy && c && ui.closing[c.id]) {
      close.disabled = true;
      close.replaceChildren(h('span', { class: 'co-long' }, 'Writing final answer'), '…');
    } else {
      close.disabled = false;
      close.textContent = 'Close out';
    }
    const atts = $('attachments');
    atts.hidden = !ui.attachments.length;
    atts.replaceChildren(...ui.attachments.map((a, i) => h('span', { class: 'att' }, icon('file'), a.name,
      h('button', { type: 'button', 'aria-label': 'Remove ' + a.name, onclick: () => { ui.attachments.splice(i, 1); renderComposer(); } }, icon('x')))));
  }

  function banner(text) {
    const b = $('banner');
    b.textContent = text || '';
    b.hidden = !text;
    if (text) setTimeout(() => { if (b.textContent === text) b.hidden = true; }, 8000);
  }

  async function onSend() {
    const c = active();
    if (c && (ui.streams[c.id] || c.busy)) {
      try { await api('POST', `/api/chats/${c.id}/stop`); } catch (e) { banner(e.message); }
      return;
    }
    let text = composer.value.trim();
    if (!text && !ui.attachments.length) return;
    for (const a of ui.attachments) text += `\n\nAttached file: ${a.name}\n\`\`\`\n${a.text}\n\`\`\``;
    text = text.trim();
    let chat = c;
    if (!chat) {
      try { chat = await createChat(); } catch (e) { banner(e.message); return; }
    }
    composer.value = '';
    ui.attachments = [];
    autosize();
    await streamTurn(chat, text);
  }

  async function onCloseOut() {
    const c = active();
    if (!c || !(ui.streams[c.id] || c.busy) || ui.closing[c.id]) return;
    try {
      await api('POST', `/api/chats/${c.id}/closeout`);
      ui.closing[c.id] = true;
      renderComposer();
    } catch (e) { banner(e.message); }
  }

  async function streamTurn(chat, text) {
    const controller = new AbortController();
    ui.streams[chat.id] = { controller };
    chat.busy = true;
    renderComposer();
    let current = null;
    let finished = false;
    try {
      const r = await fetch(`/api/chats/${chat.id}/messages`, {
        method: 'POST', credentials: 'same-origin', signal: controller.signal,
        headers: { 'X-Perseus': '1', 'Content-Type': 'application/json' }, body: JSON.stringify({ text }),
      });
      if (!r.ok) {
        const data = await r.json().catch(() => ({}));
        throw new Error(data.error || r.statusText);
      }
      const reader = r.body.getReader();
      const dec = new TextDecoder();
      let buf = '';
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let cut;
        while ((cut = buf.indexOf('\n\n')) >= 0) {
          const chunk = buf.slice(0, cut);
          buf = buf.slice(cut + 2);
          const line = chunk.split('\n').find((l) => l.startsWith('data:'));
          if (!line) continue;
          let ev;
          try { ev = JSON.parse(line.slice(5)); } catch (e) { continue; }
          if (ev.type === 'start') {
            current = ev.message;
            chat.messages.push(ev.user, current);
            chat.title = ev.chat.title;
            const row = ui.list.find((x) => x.id === chat.id);
            if (row) { row.title = chat.title; row.busy = true; }
            delete ui.feedOpenTurns[chat.id];       // a new question folds up every earlier one
            if (chat.id === ui.activeId) { renderMessages(); renderHeader(); renderFeed(); scrollToBottom(); }
            renderChatList();
          } else if (ev.type === 'text' && current) {
            if (current.phase !== 'closing') current.phase = ev.thought ? 'thinking' : 'answering';
            if (ev.thought) { current.reasoning += ev.text; if (ev.think_used != null) current.think_used = ev.think_used; }
            else {
              if (!current.content && current.reasoning && current.think_secs == null) current.think_secs = (Date.now() / 1000) - current.started;
              current.content += ev.text;
            }
            if (chat.id === ui.activeId) replaceMessage(current);
          } else if (ev.type === 'retract' && current) {
            // text shown as answer that turned out to be thinking: move it into the thinking box
            if (current.content.endsWith(ev.text)) current.content = current.content.slice(0, current.content.length - ev.text.length);
            if (!current.content) current.think_secs = null;
            current.reasoning += ev.text;
            if (ev.think_used != null) current.think_used = ev.think_used;
            if (current.phase !== 'closing') current.phase = 'thinking';
            if (chat.id === ui.activeId) replaceMessage(current);
          } else if (ev.type === 'phase' && current) {
            current.phase = ev.phase;
            if (chat.id === ui.activeId) replaceMessage(current);
          } else if (ev.type === 'file' && current) {
            current.files = current.files || [];
            if (!current.files.some((f) => f.id === ev.file.id)) current.files.push(ev.file);
            if (chat.id === ui.activeId) replaceMessage(current);
          } else if (ev.type === 'tool') {
            upsertEvent(chat, ev.event);
            if (chat.id === ui.activeId) renderFeed();
          } else if (ev.type === 'done') {
            finished = true;
            const i = chat.messages.findIndex((m) => current && m.id === current.id);
            if (i >= 0) chat.messages[i] = ev.message;
            if (ev.context_used) chat.ctxUsed = ev.context_used;
            current = ev.message;
            if (chat.id === ui.activeId) { replaceMessage(current); renderRing(); renderFeed(); }
          }
        }
      }
    } catch (e) {
      if (e.name !== 'AbortError') banner(e.message);
    } finally {
      delete ui.streams[chat.id];
      delete ui.closing[chat.id];
      chat.busy = false;
      const row = ui.list.find((x) => x.id === chat.id);
      if (row) row.busy = false;
      renderComposer();
      renderChatList();
      if (!finished && current) setTimeout(() => reloadChat(chat.id), 800); // stream cut: fetch what the server kept
    }
  }

  async function reloadChat(id) {
    try {
      const full = await api('GET', `/api/chats/${id}`);
      const keep = ui.chats[id];
      full.ctxUsed = keep ? keep.ctxUsed : 0;
      const lastAi = [...full.messages].reverse().find((m) => m.role === 'assistant' && m.usage && m.usage.prompt_tokens);
      if (lastAi) full.ctxUsed = (lastAi.usage.prompt_tokens || 0) + (lastAi.usage.completion_tokens || 0);
      ui.chats[id] = full;
      if (id === ui.activeId) renderAll();
      if (full.busy && !ui.streams[id]) setTimeout(() => reloadChat(id), 1500);
    } catch (e) { /* chat deleted meanwhile */ }
  }

  // ------------------------------------------------------------------ attachments
  const MAX_ATTACH = 200 * 1024;
  async function addFiles(files) {
    for (const f of files) {
      if (ui.attachments.length >= 5) { banner('Up to 5 files per message.'); break; }
      if (f.size > MAX_ATTACH) { banner(`${f.name} is larger than 200 KB; attach a smaller text file.`); continue; }
      const text = await f.text();
      if (text.includes('\u0000')) { banner(`${f.name} isn't a text file; only text files can be attached.`); continue; }
      ui.attachments.push({ name: f.name, text });
    }
    renderComposer();
  }

  // ------------------------------------------------------------------ chats
  async function createChat() {
    const full = await api('POST', '/api/chats');
    full.ctxUsed = 0;
    ui.chats[full.id] = full;
    ui.list.unshift({ id: full.id, title: full.title, busy: false, messages: 0 });
    ui.activeId = full.id;
    renderChatList();
    return full;
  }

  async function newChat() {
    ui.view = 'chat';
    const c = active();
    if (!c || c.messages.length) await createChat().catch((e) => banner(e.message));
    renderAll();
    composer.focus();
  }

  async function openChat(id) {
    ui.view = 'chat';
    ui.activeId = id;
    store.set('active', id);
    if (!ui.chats[id]) await reloadChat(id);
    renderAll();
    scrollToBottom();
  }

  async function deleteChat(id) {
    try { await api('DELETE', `/api/chats/${id}`); } catch (e) { banner(e.message); return; }
    delete ui.chats[id];
    ui.list = ui.list.filter((c) => c.id !== id);
    if (ui.activeId === id) ui.activeId = ui.list[0] ? ui.list[0].id : null;
    if (ui.activeId && !ui.chats[ui.activeId]) await reloadChat(ui.activeId);
    renderAll();
  }

  let deleteArmed = null;
  async function deleteAll() {
    const btn = $('delete-all');
    if (!deleteArmed) {
      btn.classList.add('confirm');
      $('delete-all-label').textContent = 'Click again to delete all';
      deleteArmed = setTimeout(() => { deleteArmed = null; btn.classList.remove('confirm'); $('delete-all-label').textContent = 'Delete all chats'; }, 4000);
      return;
    }
    clearTimeout(deleteArmed); deleteArmed = null;
    btn.classList.remove('confirm');
    $('delete-all-label').textContent = 'Delete all chats';
    try { await api('DELETE', '/api/chats'); } catch (e) { banner(e.message); return; }
    ui.list = []; ui.chats = {}; ui.activeId = null;
    renderAll();
  }

  // ------------------------------------------------------------------ settings
  async function patchSettings(patch) {
    try {
      const r = await api('PATCH', '/api/settings', patch);
      ui.server.settings = r.settings;
    } catch (e) { banner(e.message); }
    renderToggles(); renderHeader(); renderSettings(); renderFeed();
  }

  function optBtn(label, on, pick, extra) {
    return h('button', { type: 'button', class: 'opt' + (extra && extra.cls ? ' ' + extra.cls : ''), 'aria-pressed': String(on),
      disabled: extra && extra.disabled, title: extra && extra.title, onclick: pick }, ...(extra && extra.kids ? extra.kids : [label]));
  }

  function currentModel() {
    const s = settings();
    const cur = (ui.server.context_options.find((o) => o.current) || {}).id;
    return { context: cur, creativity: s.creativity, length: s.length };
  }

  function renderSettings() {
    if (!ui.server || ui.view !== 'settings') return;
    const s = settings();
    const inf = info();
    $('fact-model').textContent = inf.model;
    $('fact-gpus').textContent = inf.hardware;
    $('fact-precision').textContent = inf.precision;

    const personaIcon = { engineer: 'code', planner: 'plan', researcher: 'research', custom: 'pen' };
    $('personas').replaceChildren(...ui.server.personas.map((p) => optBtn(cap(p), s.persona === p, () => patchSettings({ persona: p }),
      { cls: 'persona', kids: [icon(personaIcon[p] || 'pen'), h('span', { text: cap(p) })] })));
    $('custom-wrap').hidden = s.persona !== 'custom';
    const cp = $('custom-prompt');
    if (document.activeElement !== cp) cp.value = s.custom_prompt || '';

    const applied = currentModel();
    const pend = ui.pending || applied;
    $('context-opts').replaceChildren(...ui.server.context_options.map((o) => optBtn(o.label, pend.context === o.id,
      () => setPending({ context: o.id }),
      { disabled: !o.available, title: o.available ? `${o.tokens.toLocaleString()} tokens` : `Doesn't fit on this box (up to ${info().capacity.toLocaleString()} tokens)` })));
    $('creativity-opts').replaceChildren(...ui.server.creativity.map((c) => optBtn(cap(c), pend.creativity === c, () => setPending({ creativity: c }))));
    $('length-opts').replaceChildren(...ui.server.length.map((l) => optBtn(cap(l), pend.length === l, () => setPending({ length: l }))));

    $('depths').replaceChildren(...Object.entries(ui.server.depths).map(([id, n]) => optBtn(cap(id), s.depth === id,
      () => patchSettings({ depth: id }), { cls: 'depth', kids: [h('span', { class: 'd-label', text: cap(id) }), h('span', { class: 'd-sub', text: `${n} lookups` })] })));

    const ENGINE_NAMES = { duckduckgo: 'DuckDuckGo', wikipedia: 'Wikipedia', github: 'GitHub', arxiv: 'arXiv', stackoverflow: 'Stack Overflow', mojeek: 'Mojeek', google: 'Google', bing: 'Bing', brave: 'Brave', startpage: 'Startpage', qwant: 'Qwant' };
    $('engines').replaceChildren(...Object.entries(s.engines || {}).map(([id, on]) => h('button', {
      type: 'button', role: 'checkbox', class: 'check', 'aria-checked': String(on),
      onclick: () => patchSettings({ engines: { [id]: !on } }),
    }, h('span', { class: 'check-box', 'aria-hidden': 'true' }, icon('check')), ENGINE_NAMES[id] || id)));

    // pending changes + apply
    const names = (list, id) => { const o = list.find((x) => x.id === id); return o ? o.label : cap(String(id)); };
    const changes = [];
    if (pend.context !== applied.context) changes.push(`Memory ${names(ui.server.context_options, applied.context)} → ${names(ui.server.context_options, pend.context)}`);
    if (pend.creativity !== applied.creativity) changes.push(`Creativity ${cap(applied.creativity)} → ${cap(pend.creativity)}`);
    if (pend.length !== applied.length) changes.push(`Answer length ${cap(applied.length)} → ${cap(pend.length)}`);
    const restarting = !!ui.server.restarting;
    $('pending-label').textContent = changes.length === 0 ? 'No pending changes'
      : changes.length === 1 ? '1 change waiting to be applied' : `${changes.length} changes waiting to be applied`;
    $('pending-summary').hidden = !changes.length;
    $('pending-summary').textContent = changes.join(' · ');
    $('discard').hidden = !changes.length;
    $('discard').disabled = restarting;
    const needsRestart = pend.context !== applied.context || !changes.length;
    $('apply-label').textContent = restarting ? 'Restarting…' : (changes.length ? (needsRestart ? 'Apply & restart' : 'Apply') : 'Restart model');
    $('apply').disabled = restarting;
    if (inf.last_restart_error && !changes.length) {
      $('pending-summary').hidden = false;
      $('pending-summary').textContent = 'Last restart failed: ' + inf.last_restart_error;
    }
  }

  function setPending(patch) {
    ui.pending = { ...(ui.pending || currentModel()), ...patch };
    const a = currentModel();
    if (ui.pending.context === a.context && ui.pending.creativity === a.creativity && ui.pending.length === a.length) ui.pending = null;
    renderSettings();
  }

  async function applyModel() {
    const a = currentModel();
    const p = ui.pending || a;
    const patch = {};
    if (p.creativity !== a.creativity) patch.creativity = p.creativity;
    if (p.length !== a.length) patch.length = p.length;
    try {
      if (Object.keys(patch).length) await patchSettings(patch);
      const restart = p.context !== a.context || !ui.pending;
      if (restart) {
        const tokens = (ui.server.context_options.find((o) => o.id === p.context) || {}).tokens || info().context;
        await api('POST', '/api/model/apply', { context: tokens });
        ui.server.restarting = true;
      }
      ui.pending = null;
    } catch (e) { banner(e.message); }
    renderSettings(); renderStatus();
    pollSoon();
  }

  let customTimer = null;
  $('custom-prompt').addEventListener('input', (e) => {
    clearTimeout(customTimer);
    $('custom-saved').textContent = '';
    customTimer = setTimeout(async () => {
      await patchSettings({ custom_prompt: e.target.value });
      $('custom-saved').textContent = 'Saved';
    }, 600);
  });

  // ------------------------------------------------------------------ raw view
  async function openRaw(m) {
    const c = active();
    if (!c) return;
    const dlg = $('raw');
    $('raw-prompt').textContent = 'Loading…';
    $('raw-output').textContent = '';
    $('raw-tokens').textContent = '';
    $('raw-note').hidden = true;
    dlg.showModal();
    try {
      const r = await api('GET', `/api/chats/${c.id}/raw/${encodeURIComponent(m.id)}`);
      $('raw-prompt').textContent = r.prompt;
      $('raw-output').textContent = r.outputs.map((o) => {
        let s = `── step ${o.step} · ${o.finish_reason || '?'}\n`;
        if (o.reasoning) s += `[reasoning]\n${o.reasoning}\n`;
        if (o.content) s += `[content]\n${o.content}\n`;
        if (o.tool_calls && o.tool_calls.length) s += `[tool calls]\n${JSON.stringify(o.tool_calls, null, 2)}\n`;
        return s;
      }).join('\n');
      const bits = [];
      if (r.prompt_tokens) bits.push(`${r.prompt_tokens.toLocaleString()} prompt tokens`);
      if (r.completion_tokens) bits.push(`${r.completion_tokens.toLocaleString()} output tokens`);
      bits.push(`${r.steps} step${r.steps === 1 ? '' : 's'}`);
      $('raw-tokens').textContent = bits.join(' · ');
      if (r.note) { $('raw-note').hidden = false; $('raw-note').textContent = r.note; }
    } catch (e) {
      $('raw-prompt').textContent = 'Not available: ' + e.message;
    }
  }
  $('raw-close').addEventListener('click', () => $('raw').close());
  $('raw').addEventListener('click', (e) => { if (e.target === $('raw')) $('raw').close(); });

  // ------------------------------------------------------------------ development pipelines + tools (WIP)
  // Read-only for now: pipelines are shown, not run. Data comes from /api/pipelines and /api/tools.
  const dev = { pipelines: null, errors: [], tools: null, folder: 'perseus/pipelines', loading: false, error: null,
                pid: store.get('pipeline', null), stage: {}, filter: '' };
  const isDev = () => ui.view === 'pipelines' || ui.view === 'tools';
  const TOOL_ICONS = { web_search: 'search', fetch_url: 'fetch', calculator: 'calc', current_time: 'clock' };
  const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;

  async function loadDev() {
    if (dev.loading) return;
    dev.loading = true; dev.error = null;
    if (!dev.pipelines) renderDev();
    try {
      const [p, t] = await Promise.all([api('GET', '/api/pipelines'), api('GET', '/api/tools')]);
      dev.pipelines = p.pipelines; dev.errors = p.errors || []; dev.folder = p.folder || dev.folder; dev.tools = t.tools;
    } catch (e) { dev.error = e.message; }
    dev.loading = false;
    renderDev(); renderNav();
  }

  function openDev(view) {
    hideTip();
    ui.view = view; ui.menuOpen = false; ui.pending = null;
    renderAll();
    $('dev-view').scrollTop = 0;
    loadDev();           // every visit: picks up pipeline and tool files added since
  }

  function renderNav() {
    for (const id of ['nav-pipelines', 'nav-tools']) {
      const b = $(id);
      const on = ui.view === b.dataset.view;
      b.classList.toggle('active', on);
      if (on) b.setAttribute('aria-current', 'page'); else b.removeAttribute('aria-current');
    }
    const n = dev.tools ? dev.tools.length : (ui.server && ui.server.tools ? ui.server.tools.length : null);
    $('nav-tools-count').textContent = n == null ? '' : String(n);
    $('nav-tools-count').setAttribute('aria-label', n == null ? '' : plural(n, 'tool'));
  }

  // ---- tooltip: one shared element, on hover (short delay) and on keyboard focus; Escape closes it
  const tip = h('div', { id: 'dev-tip', class: 'tip', role: 'tooltip', hidden: true });
  app.append(tip);
  let tipOwner = null, tipTimer = null;
  function showTip(el) {
    clearTimeout(tipTimer);
    if (!el.isConnected) return;
    tipOwner = el;
    tip.replaceChildren(...[].concat(el._tip()));
    tip.hidden = false;
    el.setAttribute('aria-describedby', 'dev-tip');
    const r = el.getBoundingClientRect(), w = tip.offsetWidth, th = tip.offsetHeight;
    const below = r.top - th - 10 < 8;
    const left = Math.max(8, Math.min(r.left + r.width / 2 - w / 2, window.innerWidth - w - 8));
    tip.style.left = left + 'px';
    tip.style.top = (below ? r.bottom + 10 : r.top - th - 10) + 'px';
    tip.style.setProperty('--arrow-x', Math.max(14, Math.min(w - 14, r.left + r.width / 2 - left)) + 'px');
    tip.classList.toggle('below', below);
  }
  function hideTip() {
    clearTimeout(tipTimer);
    if (tipOwner) tipOwner.removeAttribute('aria-describedby');
    tipOwner = null; tip.hidden = true;
  }
  function withTip(el, build) {
    el._tip = build;
    el.addEventListener('mouseenter', () => { clearTimeout(tipTimer); tipTimer = setTimeout(() => showTip(el), 150); });
    el.addEventListener('mouseleave', hideTip);
    el.addEventListener('focus', () => showTip(el));
    el.addEventListener('blur', hideTip);
    return el;
  }
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && tipOwner) hideTip(); });
  let tipFrame = 0;   // follow the owner when the page scrolls (focusing an off-screen item scrolls it into view)
  document.addEventListener('scroll', () => {
    if (!tipOwner || tipFrame) return;
    tipFrame = requestAnimationFrame(() => { tipFrame = 0; if (tipOwner) showTip(tipOwner); });
  }, true);

  function toolTip(name) {
    const t = (dev.tools || []).find((x) => x.name === name);
    if (!t) {
      return [h('div', { class: 'tip-head' }, h('span', { class: 'tip-name mono', text: name }), h('span', { class: 'chip warn', text: 'Not installed' })),
        h('p', { class: 'tip-desc', text: 'No tool with this name is loaded, so this stage would run without it. Add it to perseus/tools/ or remove it from the pipeline.' })];
    }
    const out = [h('div', { class: 'tip-head' }, h('span', { class: 'tip-name mono', text: t.name }),
      h('span', { class: 'chip' + (t.web ? '' : ' run'), text: t.web ? 'Web' : 'Local' })),
      h('p', { class: 'tip-desc', text: t.description })];
    if (t.params.length) {
      out.push(h('div', { class: 'tip-sub', text: 'Arguments' }),
        h('ul', { class: 'tip-params' }, t.params.map((p) => h('li', {},
          h('span', { class: 'mono tip-param', text: p.name }), h('span', { class: 'tip-type', text: p.type + (p.required ? '' : ' · optional') }),
          p.description ? h('span', { class: 'tip-pdesc', text: p.description }) : null))));
    } else out.push(h('div', { class: 'tip-sub', text: 'Takes no arguments' }));
    const meta = [`Times out after ${t.timeout_s} s`];
    if (t.used_by.length) meta.push('Used by ' + [...new Set(t.used_by.map((u) => u.role))].join(', '));
    out.push(h('div', { class: 'tip-meta', text: meta.join(' · ') }));
    return out;
  }

  function toolChip(name, missing) {
    return withTip(h('button', { type: 'button', class: 'tool-chip mono' + (missing ? ' missing' : '') },
      missing ? icon('alert') : null, name), () => toolTip(name));
  }

  // ---- shared states
  function devState() {
    if (dev.error && !dev.pipelines) {
      return h('div', { class: 'dev-empty' }, h('p', { text: "Couldn't load development data: " + dev.error }),
        h('button', { type: 'button', class: 'btn-secondary', onclick: loadDev }, 'Try again'));
    }
    return h('div', { class: 'dev-empty', 'aria-busy': 'true' }, h('div', { class: 'skeleton' }), h('div', { class: 'skeleton short' }));
  }

  function renderDev() {
    if (!isDev()) return;
    hideTip();
    const body = $('dev-body');
    if (!dev.pipelines) { body.replaceChildren(devState()); return; }
    body.replaceChildren(...(ui.view === 'tools' ? toolsPage() : pipelinesPage()));
  }

  // ---- pipelines page
  function pipelinesPage() {
    const out = [h('div', { class: 'callout' }, icon('info'), h('div', {},
      h('strong', { text: 'Preview. ' }),
      'Pipelines don’t run yet. Each stage is a role with its own prompt and tools; when it finishes, its work is handed to the next stage in a ',
      h('strong', { text: 'new context window' }),
      ', so review stages judge the work without seeing how it was made.'))];
    for (const e of dev.errors) {
      out.push(h('div', { class: 'callout warn', role: 'alert' }, icon('alert'),
        h('div', {}, h('strong', { class: 'mono', text: e.file }), ' wasn’t loaded: ' + e.error)));
    }
    if (!dev.pipelines.length) {
      out.push(h('div', { class: 'card dev-empty' }, h('h2', { text: 'No pipelines yet' }),
        h('p', { class: 'hint', text: `Add a .toml file to ${dev.folder}/. The format is described at the top of ${dev.folder}/__init__.py.` })));
      return out;
    }
    const pl = dev.pipelines.find((p) => p.id === dev.pid) || dev.pipelines[0];
    const sel = pl.stages.find((s) => s.id === dev.stage[pl.id]) || pl.stages[0];

    if (dev.pipelines.length > 1) {
      const tabs = h('div', { class: 'dev-tabs', role: 'tablist', 'aria-label': 'Pipelines' },
        dev.pipelines.map((p) => h('button', { type: 'button', role: 'tab', class: 'dev-tab', id: 'pl-tab-' + p.id,
          'aria-selected': String(p.id === pl.id), 'aria-controls': 'pl-panel', tabindex: p.id === pl.id ? '0' : '-1',
          onclick: () => { dev.pid = p.id; store.set('pipeline', p.id); renderDev(); } },
        h('span', { class: 'dev-tab-name', text: p.name }), h('span', { class: 'dev-tab-count', text: plural(p.stages.length, 'stage') }))));
      tabs.addEventListener('keydown', (e) => {   // arrow keys move between tabs (WAI-ARIA tabs pattern)
        const i = dev.pipelines.findIndex((p) => p.id === pl.id);
        const j = e.key === 'ArrowRight' ? i + 1 : e.key === 'ArrowLeft' ? i - 1 : e.key === 'Home' ? 0 : e.key === 'End' ? dev.pipelines.length - 1 : null;
        if (j === null) return;
        e.preventDefault();
        const next = dev.pipelines[(j + dev.pipelines.length) % dev.pipelines.length];
        dev.pid = next.id; store.set('pipeline', next.id); renderDev();
        $('pl-tab-' + next.id).focus();
      });
      out.push(tabs);
    }

    out.push(h('section', { class: 'card pl-card', id: 'pl-panel', role: dev.pipelines.length > 1 ? 'tabpanel' : null,
      'aria-labelledby': dev.pipelines.length > 1 ? 'pl-tab-' + pl.id : null },
      h('div', { class: 'pl-head' },
        h('div', { class: 'pl-title' }, h('h2', { text: pl.name }), h('span', { class: 'chip ' + (pl.status === 'ready' ? '' : 'warn'), text: cap(pl.status) })),
        h('span', { class: 'pl-source mono', title: 'Defined in', text: `${dev.folder}/${pl.source}` })),
      h('p', { class: 'pl-summary', text: pl.summary }),
      flow(pl, sel.id),
      h('div', { class: 'legend' },
        h('span', { class: 'legend-item' }, h('span', { class: 'conn-badge' }, icon('fresh')), 'New context window at every handoff'),
        h('span', { class: 'legend-item' }, h('span', { class: 'legend-gate' }, icon('shield')), 'Review gate: pass moves on, revise sends the work back'))));
    out.push(stageDetail(pl, sel));
    out.push(h('p', { class: 'hint dev-foot' }, `Add or edit a pipeline in ${dev.folder}/ (one .toml file each). Changes show up the next time you open this page.`));
    return out;
  }

  function flow(pl, selId) {
    const grid = h('div', { class: 'flow', role: 'group', 'aria-label': `${pl.name}: ${plural(pl.stages.length, 'stage')}` });
    grid.style.gridTemplateColumns = pl.stages.map(() => 'minmax(150px, 1fr)').join(' 56px ');
    pl.stages.forEach((st, i) => {
      if (i) grid.append(h('div', { class: 'conn', 'aria-hidden': 'true' }, h('span', { class: 'conn-line' }),
        h('span', { class: 'conn-badge', title: 'New context window: only the listed artifacts are handed on' }, icon('fresh')),
        pl.stages[i - 1].kind === 'review' ? h('span', { class: 'conn-label', text: 'pass' }) : null));
      grid.append(stageCard(pl, st, i, st.id === selId));
    });
    let row = 2;
    pl.stages.forEach((st, g) => {
      if (st.kind !== 'review') return;
      const t = pl.stages.findIndex((s) => s.id === st.on_revise);
      const lane = h('div', { class: 'lane', 'aria-hidden': 'true' },
        h('span', { class: 'lane-label' }, icon('revise'), `Revise · up to ${plural(st.max_rounds, 'round')}`));
      lane.style.gridColumn = `${2 * t + 1} / ${2 * g + 2}`;
      lane.style.gridRow = String(row++);
      grid.append(lane);
    });
    return h('div', { class: 'flow-scroll' }, grid);
  }

  function stageCard(pl, st, i, on) {
    const review = st.kind === 'review';
    const target = review ? pl.stages.find((s) => s.id === st.on_revise) : null;
    const missing = st.missing_tools.length;
    return h('button', { type: 'button', class: 'stage' + (review ? ' gate' : '') + (on ? ' on' : ''),
      'aria-pressed': String(on), 'aria-controls': 'stage-detail',
      onclick: () => { dev.stage[pl.id] = st.id; renderDev(); const d = $('stage-detail'); if (d) d.scrollIntoView({ block: 'nearest', behavior: 'smooth' }); } },
    h('span', { class: 'stage-top' }, h('span', { class: 'stage-num', text: String(i + 1) }),
      h('span', { class: 'stage-kind' }, review ? icon('shield') : null, review ? 'Review gate' : 'Work')),
    h('span', { class: 'stage-role', text: st.role }),
    h('span', { class: 'stage-goal', text: st.goal }),
    h('span', { class: 'stage-foot' },
      h('span', { class: 'stage-meta' + (missing ? ' warn' : ''), title: missing ? 'Not installed: ' + st.missing_tools.join(', ') : null },
        missing ? icon('alert') : icon('tool'), st.tools.length ? plural(st.tools.length, 'tool') : 'No tools'),
      review ? h('span', { class: 'stage-meta' }, icon('revise'), 'Back to ' + (target ? target.role : st.on_revise))
        : h('span', { class: 'stage-meta mono' }, icon('arrow'), st.outputs.join(', '))));
  }

  function stageDetail(pl, st) {
    const i = pl.stages.indexOf(st);
    const review = st.kind === 'review';
    const roleOf = (id) => { const s = pl.stages.find((x) => x.id === id); return s ? s.role : id; };
    const next = pl.stages[i + 1];
    const section = (title, ...kids) => h('div', { class: 'sd-sec' }, h('h4', { text: title }), ...kids);

    const receives = h('ul', { class: 'sd-list' }, st.inputs.map((a) => h('li', {},
      h('span', { class: 'mono sd-art', text: a.name }),
      h('span', { class: 'sd-from', text: a.name === 'request' ? 'what you asked for' : 'from ' + roleOf(a.from || '') }))));
    const handsOn = review
      ? h('ul', { class: 'sd-list' },
        h('li', {}, h('span', { class: 'chip', text: 'Pass' }), h('span', { class: 'sd-from', text: next ? 'moves on to ' + next.role : 'pipeline complete' })),
        h('li', {}, h('span', { class: 'chip warn', text: 'Revise' }),
          h('span', { class: 'sd-from', text: `feedback goes back to ${roleOf(st.on_revise)} (up to ${plural(st.max_rounds, 'round')}, then a person decides)` })))
      : h('ul', { class: 'sd-list' }, st.outputs.map((a) => h('li', {}, h('span', { class: 'mono sd-art', text: a }),
        h('span', { class: 'sd-from', text: next ? 'to ' + next.role + (pl.stages.slice(i + 2).some((s) => s.inputs.some((x) => x.name === a)) ? ' and later stages' : '') : 'final output' }))));

    const tools = st.tools.length
      ? h('div', { class: 'tool-chips' }, st.tools.map((t) => toolChip(t, st.missing_tools.includes(t))))
      : h('p', { class: 'hint', text: 'No tools: this stage works only from what it’s handed.' });

    return h('section', { class: 'card stage-detail', id: 'stage-detail', 'aria-labelledby': 'sd-title' },
      h('div', { class: 'sd-head' },
        h('div', {}, h('div', { class: 'sd-eyebrow', text: `Stage ${i + 1} of ${pl.stages.length}` }),
          h('h3', { id: 'sd-title', text: st.role })),
        h('div', { class: 'sd-chips' }, h('span', { class: 'chip' + (review ? ' warn' : ' run'), text: review ? 'Review gate' : 'Work' }),
          st.thinking ? h('span', { class: 'chip run', text: 'Thinking: ' + cap(st.thinking) }) : null)),
      h('p', { class: 'sd-goal', text: st.goal }),
      h('div', { class: 'sd-fresh' }, icon('fresh'),
        'Starts in a fresh context window. It sees only the items under “Receives”, never earlier reasoning, tool results or chat.'),
      h('div', { class: 'sd-grid' },
        h('div', { class: 'sd-col' },
          section('Role instructions', h('p', { class: 'sd-prompt', text: st.prompt })),
          section(review ? 'Passes only if' : 'Done when', h('ul', { class: 'sd-checks' },
            st.done_when.length ? st.done_when.map((c) => h('li', {}, icon('check'), h('span', { text: c })))
              : h('li', { class: 'hint', text: 'No criteria set.' })))),
        h('div', { class: 'sd-col' },
          section('Receives', receives),
          section(review ? 'Decides' : 'Hands on', handsOn),
          section('Tools', tools))),
      h('details', { class: 'sd-more' }, h('summary', {}, icon('chevron'), 'System prompt this stage gets'), codeBlock(st.system_prompt, 'text')),
      h('details', { class: 'sd-more' }, h('summary', {}, icon('chevron'), 'Opening message it receives'), codeBlock(st.packet, 'text')));
  }

  // ---- tools page
  function toolsPage() {
    const tools = dev.tools || [];
    const q = dev.filter.trim().toLowerCase();
    const shown = tools.filter((t) => !q || t.name.toLowerCase().includes(q) || t.description.toLowerCase().includes(q));
    const input = h('input', { type: 'search', id: 'tool-filter', class: 'tool-filter', placeholder: 'Filter tools', 'aria-label': 'Filter tools', autocomplete: 'off' });
    input.value = dev.filter;
    input.addEventListener('input', () => {
      dev.filter = input.value;
      const at = input.selectionStart;
      renderDev();
      const again = $('tool-filter'); again.focus(); again.setSelectionRange(at, at);
    });
    const grid = h('div', { class: 'tool-grid', role: 'list', 'aria-label': 'Available tools' }, shown.map((t) => h('div', { role: 'listitem' },
      withTip(h('button', { type: 'button', class: 'tool-tile' },
        h('span', { class: 'tool-ico' }, icon(TOOL_ICONS[t.name] || 'tool')),
        h('span', { class: 'tool-body' }, h('span', { class: 'tool-name mono', text: t.name }),
          h('span', { class: 'tool-meta' },
            h('span', { class: 'chip' + (t.web ? '' : ' run'), text: t.web ? 'Web' : 'Local' }),
            h('span', { text: t.used_by.length ? 'Used in ' + plural(t.used_by.length, 'stage') : 'Not used in a pipeline' })))),
      () => toolTip(t.name)))));
    return [
      h('div', { class: 'tools-head' },
        h('p', { class: 'tools-lede' }, `${plural(tools.length, 'tool')} the model can call. Hover over or focus a tool to see what it does and what it takes.`),
        input),
      shown.length ? grid : h('div', { class: 'card dev-empty' }, h('p', { class: 'hint', text: tools.length ? `No tools match “${dev.filter}”.` : 'No tools are loaded.' })),
      h('p', { class: 'hint dev-foot', text: 'Tools are Python files in perseus/tools/. New and changed files load before the next answer.' }),
    ];
  }

  $('nav-pipelines').addEventListener('click', () => openDev('pipelines'));
  $('nav-tools').addEventListener('click', () => openDev('tools'));
  window.addEventListener('resize', () => { if (tipOwner) hideTip(); });

  // ------------------------------------------------------------------ render all
  function renderAll() {
    applyTheme();
    const inChat = ui.view === 'chat';
    $('chat-view').hidden = !inChat;
    $('settings-view').hidden = ui.view !== 'settings';
    $('dev-view').hidden = !isDev();
    if (!isDev()) hideTip();
    renderToggles(); renderChatList(); renderNav(); renderStatus(); renderHeader();
    if (inChat) { renderMessages(); renderComposer(); }
    renderFeed(); renderSettings(); renderDev();
    relayout();
  }

  // ------------------------------------------------------------------ polling
  let pollTimer = null;
  async function poll() {
    clearTimeout(pollTimer);
    try {
      const st = await api('GET', '/api/state');
      const wasRestarting = ui.server && ui.server.restarting;
      ui.server = st;
      // merge the list, keeping local busy flags for chats this tab is streaming
      ui.list = st.chats.map((c) => ({ ...c, busy: c.busy || !!ui.streams[c.id] }));
      if (ui.activeId && !st.chats.some((c) => c.id === ui.activeId)) ui.activeId = st.chats[0] ? st.chats[0].id : null;
      renderStatus(); renderChatList(); renderNav();
      if (ui.view === 'settings') renderSettings();
      if (wasRestarting && !st.restarting) { renderHeader(); renderRing(); }
    } catch (e) {
      ui.server && (ui.server.status = { vLLM: { state: 'failed', reason: 'dashboard unreachable' } });
      renderStatus();
    }
    pollTimer = setTimeout(poll, ui.server && ui.server.restarting ? 1500 : 4000);
  }
  const pollSoon = () => { clearTimeout(pollTimer); pollTimer = setTimeout(poll, 400); };

  // ------------------------------------------------------------------ events
  $('new-chat').addEventListener('click', newChat);
  $('gear').addEventListener('click', () => { ui.view = ui.view === 'settings' ? 'chat' : 'settings'; ui.menuOpen = false; ui.pending = null; renderAll(); });
  $('internet-pill').addEventListener('click', () => patchSettings({ internet: !settings().internet }));
  $('think-btn').addEventListener('click', (e) => { e.stopPropagation(); ui.menuOpen = !ui.menuOpen; renderHeader(); });
  document.addEventListener('click', (e) => { if (ui.menuOpen && !$('think-wrap').contains(e.target)) { ui.menuOpen = false; renderHeader(); } });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && ui.menuOpen) { ui.menuOpen = false; renderHeader(); $('think-btn').focus(); } });
  $('hide-feed').addEventListener('click', () => { ui.feedOpen = false; store.set('feed', false); renderHeader(); renderFeed(); });
  $('show-feed').addEventListener('click', () => { ui.feedOpen = true; store.set('feed', true); renderHeader(); renderFeed(); });
  $('theme-light').addEventListener('click', () => { ui.theme = 'light'; store.set('theme', 'light'); applyTheme(); });
  $('theme-dark').addEventListener('click', () => { ui.theme = 'dark'; store.set('theme', 'dark'); applyTheme(); });
  $('delete-all').addEventListener('click', deleteAll);
  $('discard').addEventListener('click', () => { ui.pending = null; renderSettings(); });
  $('apply').addEventListener('click', applyModel);
  $('send').addEventListener('click', onSend);
  $('closeout').addEventListener('click', onCloseOut);
  $('clear-feed').addEventListener('click', clearFeed);
  $('attach').addEventListener('click', () => $('file-input').click());
  $('file-input').addEventListener('change', (e) => { addFiles([...e.target.files]); e.target.value = ''; });
  composer.addEventListener('input', () => { autosize(); renderComposer(); });
  composer.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); onSend(); }
  });
  composer.addEventListener('paste', (e) => {
    const files = [...(e.clipboardData && e.clipboardData.files) || []];
    if (files.length) { e.preventDefault(); addFiles(files); }
  });
  window.addEventListener('resize', relayout);

  // ------------------------------------------------------------------ boot
  (async () => {
    applyTheme();
    await poll();
    if (!ui.server) return;
    const remembered = store.get('active', null);
    ui.activeId = ui.list.some((c) => c.id === remembered) ? remembered : (ui.list[0] ? ui.list[0].id : null);
    if (ui.activeId) await reloadChat(ui.activeId);
    renderAll();
    scrollToBottom();
    composer.focus();
  })();
})();
