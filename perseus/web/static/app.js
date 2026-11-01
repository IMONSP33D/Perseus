/* Perseus dashboard client, the Aegis design. No framework, no build step.
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
  const withClass = (el, cls) => { el.classList.add(cls); return el; };

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
  const fileSize = (n) => (n < 1024 ? `${n} B` : kb(n));   // 512 B, 234 KB, 1.2 MB
  const kb = (n) => n >= 1024 * 1024 ? (n / 1048576).toFixed(1) + ' MB' : Math.max(1, Math.round(n / 1024)) + ' KB';
  const secs = (ms) => (ms / 1000).toFixed(1) + ' s';
  const tokensLabel = (n) => n >= 1000 ? (n / 1000).toFixed(n >= 10000 ? 0 : 1) + 'K' : String(n);
  const ctxLabel = (n) => n >= 1024 ? Math.round(n / 1024) + 'K' : String(n);
  const cap = (s) => s.charAt(0).toUpperCase() + s.slice(1);
  const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;

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
    view: 'chat',            // chat | settings | tools
    history: [],             // earlier views, for Back
    sideTab: 'chats',        // chats | feed
    server: null,            // /api/state
    list: [],                // chat summaries, newest first
    chats: {},               // id -> full chat
    activeId: null,
    streams: {},             // chat id -> { controller }
    closing: {},             // chat id -> true while the model writes its closed-out answer
    feedOpenTurns: {},       // chat id -> Set of earlier question numbers opened in the feed
    feedShutTurns: {},       // chat id -> Set of current question numbers folded by the reader
    callOpen: new Set(),     // tool event ids with their details open
    thinkShrunk: {},         // message id -> true when its thinking is folded to one line
    fileOpen: {},            // chat id + file id -> preview open
    fileText: {},            // chat id + file id -> fetched content (or { error })
    thinkTouched: {},        // message id -> time of the reader's last input in its thinking box
    thinkFollow: {},         // message id -> false once the reader scrolls up in a streaming thinking box
    menu: null,              // which message-box menu is open: persona | think
    attachments: [],
    pending: null,           // { context } chosen in settings, waiting for a restart
  };

  const active = () => (ui.activeId && ui.chats[ui.activeId]) || null;
  const settings = () => (ui.server ? ui.server.settings : {});
  const info = () => (ui.server ? ui.server.info : {});

  const THINK = { low: ['4K', 4096], medium: ['16K', 16384], high: ['32K', 32768], max: ['MAX', null] };
  const PERSONA_ORDER = ['researcher', 'engineer', 'planner', 'custom'];
  const PERSONA_ICON = { engineer: 'code', planner: 'plan', researcher: 'research', custom: 'pen' };
  const PERSONA_SHORT = {
    researcher: 'Finds sources and cites them.', engineer: 'Precise, working answers with code.',
    planner: 'Concrete, ordered steps.', custom: 'Your own instructions.',
  };
  const ENGINE_NAMES = { duckduckgo: 'DuckDuckGo', wikipedia: 'Wikipedia', github: 'GitHub', arxiv: 'arXiv', stackoverflow: 'Stack Overflow',
    mojeek: 'Mojeek', google: 'Google', bing: 'Bing', brave: 'Brave', startpage: 'Startpage', qwant: 'Qwant' };
  const CUSTOM_MAX = 8000;

  // ------------------------------------------------------------------ syntax colours
  // A small tokenizer per language; each match becomes a span with a t-* class. Unknown languages stay plain.
  const KW_PY = 'def|return|for|while|in|is|try|except|finally|raise|import|from|as|if|else|elif|continue|break|pass|class|with|not|and|or|lambda|yield|async|await|global|nonlocal|None|True|False';
  const KW_JS = 'const|let|var|function|return|if|else|for|while|do|switch|case|break|continue|new|class|extends|import|from|export|default|try|catch|finally|throw|async|await|typeof|instanceof|of|in|this|null|undefined|true|false|interface|type';
  const KW_SH = 'if|then|else|elif|fi|for|in|do|done|while|case|esac|function|return|export|local';
  const RULES = {
    py: [new RegExp(`(#[^\\n]*)|("""[\\s\\S]*?"""|'''[\\s\\S]*?'''|[rbf]?"(?:\\\\.|[^"\\\\\\n])*"|[rbf]?'(?:\\\\.|[^'\\\\\\n])*')|\\b(${KW_PY})\\b|\\b(\\d+(?:\\.\\d+)?)\\b|\\b(str|int|float|dict|list|set|tuple|len|range|print|open|self|Exception|ValueError|TypeError|KeyError)\\b|\\b([A-Za-z_]\\w*)(?=\\()`, 'g'),
      ['com', 'str', 'kw', 'num', 'bi', 'fn']],
    js: [new RegExp(`(\\/\\/[^\\n]*|\\/\\*[\\s\\S]*?\\*\\/)|("(?:\\\\.|[^"\\\\\\n])*"|'(?:\\\\.|[^'\\\\\\n])*'|\`(?:\\\\.|[^\`\\\\])*\`)|\\b(${KW_JS})\\b|\\b(\\d+(?:\\.\\d+)?)\\b|\\b(console|document|window|Math|JSON|Promise|Array|Object|String|Number)\\b|\\b([A-Za-z_$][\\w$]*)(?=\\()`, 'g'),
      ['com', 'str', 'kw', 'num', 'bi', 'fn']],
    json: [/("(?:\\.|[^"\\])*")(?=\s*:)|("(?:\\.|[^"\\])*")|\b(true|false|null)\b|(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)|([{}[\],:])/g,
      ['key', 'str', 'num', 'num', 'punct']],
    sh: [new RegExp(`(#[^\\n]*)|("(?:\\\\.|[^"\\\\])*"|'[^']*')|\\b(${KW_SH})\\b|(\\s-{1,2}[\\w-]+)|(\\$\\{?\\w+\\}?)`, 'g'),
      ['com', 'str', 'kw', 'key', 'bi']],
    res: [/(<<<UNTRUSTED CONTENT[^\n]*|<<<END UNTRUSTED CONTENT>>>)|^(Blocked:|Error:)|^([A-Za-z][\w ]{0,20}:)(?= )/gm,
      ['flag', 'tag', 'key']],
    tmpl: [/(<\|[^|>]+\|>|<\/?(?:system|user|assistant|tool_call|tool_response|think)>)|(?<=<\|im_start\|>)(system|user|assistant|tool)/g,
      ['tag', 'kw']],
  };
  const LANG = { python: 'py', py: 'py', javascript: 'js', js: 'js', ts: 'js', typescript: 'js', jsx: 'js', tsx: 'js', json: 'json',
    bash: 'sh', sh: 'sh', shell: 'sh', zsh: 'sh', console: 'sh', res: 'res', tmpl: 'tmpl' };
  function highlight(code, lang) {
    const rule = RULES[LANG[(lang || '').toLowerCase()]];
    if (!rule) return [code];
    const [re, names] = rule;
    const out = [];
    let last = 0, m;
    re.lastIndex = 0;
    while ((m = re.exec(code))) {
      if (m[0] === '') { re.lastIndex++; continue; }
      if (m.index > last) out.push(code.slice(last, m.index));
      let g = 1;
      while (g < m.length && m[g] === undefined) g++;
      out.push(h('span', { class: 't-' + (names[g - 1] || 'punct') }, m[0]));
      last = m.index + m[0].length;
    }
    if (last < code.length) out.push(code.slice(last));
    return out;
  }

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

  function copyButton(source, label, cls) {
    const getText = source;   // a function giving the text (or a promise of it)
    const idle = () => [icon('copy'), h('span', { text: 'Copy' })];
    const btn = h('button', { type: 'button', class: cls, 'aria-label': label }, idle());
    btn.addEventListener('click', async () => {
      let ok = false;
      try { ok = await copyText(await getText()); } catch (e) { banner(e.message); }
      btn.replaceChildren(icon(ok ? 'check' : 'x'), h('span', { text: ok ? 'Copied' : 'Copy failed' }));
      btn.classList.toggle('done', ok);
      clearTimeout(btn._t);
      btn._t = setTimeout(() => { btn.replaceChildren(...idle()); btn.classList.remove('done'); }, 1600);
    });
    return btn;
  }

  // A fenced code block: a header with the language and Copy, then the coloured code (line numbers on request).
  function codeBlock(text, lang, numbered) {
    const pre = h('pre', { class: 'code', 'aria-label': lang ? lang + ' code' : 'code' }, highlight(text, lang));
    const body = numbered
      ? h('div', { class: 'code-lines' },
        h('pre', { class: 'ln', 'aria-hidden': 'true' }, text.split('\n').map((_, i) => String(i + 1)).join('\n')), pre)
      : pre;
    return h('div', { class: 'code-block' },
      h('div', { class: 'code-head' },
        h('span', { class: 'code-lang mono', text: lang || 'text' }),
        copyButton(() => text, 'Copy code', 'code-copy')),
      body);
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

  // ------------------------------------------------------------------ theme, views
  function applyTheme() {
    const dark = ui.theme === 'dark';
    for (const el of [app, $('raw'), $('chat-settings')]) {
      el.classList.toggle('pz-dark', dark);
      el.classList.toggle('pz-light', !dark);
    }
    document.body.classList.toggle('pz-dark-body', dark);
  }
  function setTheme(t) { ui.theme = t; store.set('theme', t); applyTheme(); renderSettings(); }

  function go(view) {
    if (ui.view === view) return;
    ui.history.push(ui.view);
    if (ui.history.length > 20) ui.history.shift();
    showView(view);
  }
  function back() { showView(ui.history.pop() || 'chat'); }
  function showView(view) {
    ui.view = view; ui.menu = null; ui.pending = null;
    renderAll();
    window.scrollTo(0, 0);
    if (view === 'tools') loadTools();
    if (view === 'chat') scrollToBottom();
  }

  // ------------------------------------------------------------------ left bar
  function renderNav() {
    for (const id of ['nav-tools', 'nav-settings']) {
      const b = $(id);
      const on = ui.view === b.dataset.view;
      b.classList.toggle('active', on);
      if (on) b.setAttribute('aria-current', 'page'); else b.removeAttribute('aria-current');
    }
    const n = tools.list ? tools.list.length : (ui.server && ui.server.tools ? ui.server.tools.length : null);
    const badge = $('nav-tools-count');
    badge.textContent = n == null ? '' : String(n);
    if (n == null) badge.removeAttribute('aria-label'); else badge.setAttribute('aria-label', plural(n, 'tool'));
  }

  function renderSideTabs() {
    const feed = ui.sideTab === 'feed';
    $('tab-chats').setAttribute('aria-selected', String(!feed));
    $('tab-feed').setAttribute('aria-selected', String(feed));
    $('tab-chats').tabIndex = feed ? -1 : 0;
    $('tab-feed').tabIndex = feed ? 0 : -1;
    $('panel-chats').hidden = feed;
    $('panel-feed').hidden = !feed;
  }
  function setSideTab(tab) { ui.sideTab = tab; store.set('sideTab', tab); renderSideTabs(); renderFeed(); }

  function renderChatList() {
    const rows = ui.list.map((c) => {
      const on = c.id === ui.activeId && ui.view === 'chat';
      return h('li', { class: 'chat-row' + (on ? ' active' : '') },
        h('button', { type: 'button', class: 'chat-pick', 'aria-current': on ? 'true' : null, title: c.title,
          onclick: () => openChat(c.id) }, h('span', { class: 'chat-title', text: c.title })),
        c.busy ? h('span', { class: 'chat-busy', title: 'Answering…' }) : null,
        h('button', { type: 'button', class: 'chat-del', 'aria-label': 'Delete ' + c.title, title: 'Delete chat',
          onclick: () => deleteChat(c.id) }, icon('x')));
    });
    $('chat-list').replaceChildren(...(rows.length ? rows : [h('li', { class: 'empty-chats', text: 'No chats yet.' })]));
  }

  // ------------------------------------------------------------------ top bar
  function statusOf() {
    const st = ui.server && ui.server.status;
    if (!ui.server) return { cls: '', short: 'Connecting…', long: 'Connecting…' };
    if (ui.server.restarting) return { cls: 'restarting', short: 'Restarting…', long: 'Model restarting' };
    const v = st && (st.vLLM || st.vllm);
    const state = v ? v.state : 'up';
    if (state === 'up') return { cls: 'up', short: 'Model running', long: 'Model running' };
    if (state === 'restarting' || state === 'starting') return { cls: 'restarting', short: 'Restarting…', long: 'Model restarting' };
    return { cls: 'down', short: 'Model offline', long: 'Model offline' + (v && v.reason ? ': ' + v.reason : '') };
  }

  function renderStatus() {
    const s = statusOf();
    $('status-dot').className = 'status-dot ' + s.cls;
    $('status-long').textContent = s.long;
    $('status-model').textContent = ui.server ? info().model || '' : '';
    $('server-dot').className = 'status-dot ' + s.cls;
    $('server-label').textContent = s.short;
  }

  function renderHeader() {
    const c = active();
    $('header-title').textContent = ui.view === 'settings' ? 'Settings' : ui.view === 'tools' ? 'Agent Tools'
      : (c && c.messages.length ? c.title : 'New chat');
    $('back').hidden = !ui.history.length;
    renderContext();
  }

  // Context used by the chat so far: the pill's bar runs green to red as it fills.
  function renderContext() {
    const pill = $('ctx-pill');
    const c = active();
    pill.hidden = ui.view !== 'chat' || !ui.server;
    if (pill.hidden) return;
    const total = info().context || 1;
    const used = (c && c.ctxUsed) || 0;
    const frac = Math.min(1, used / total);
    const pct = (frac * 100).toFixed(2).replace(/\.00$/, '') + '%';
    $('ctx-pct').textContent = pct;
    $('ctx-of').textContent = `of ${ctxLabel(total)} context`;
    const fill = $('ctx-fill');
    fill.style.width = (frac * 100).toFixed(2) + '%';
    fill.style.backgroundColor = `hsl(${Math.round(135 * (1 - frac))}, 68%, 48%)`;
    pill.setAttribute('aria-valuenow', (frac * 100).toFixed(2));
    const tip = `${used.toLocaleString()} of ${total.toLocaleString()} tokens of context used`;
    pill.setAttribute('aria-label', tip);
    pill.title = tip;
  }

  // ------------------------------------------------------------------ messages
  function renderMessages() {
    const c = active();
    const box = $('messages');
    if (!c || !c.messages.length) {
      box.replaceChildren(h('div', { class: 'hero' },
        h('img', { src: '/assets/logo-header.svg', alt: 'Perseus' }),
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
    const kids = [];
    if (hasReasoning) kids.push(thinkNode(m, phase));
    if (!hasReasoning && !m.content && (stillThinking || wrapping)) kids.push(h('p', { class: 'waiting', text: wrapping ? 'Wrapping up…' : 'Thinking…' }));
    if (phase === 'tools' && !m.content) kids.push(h('p', { class: 'waiting', text: 'Using tools…' }));
    kids.push(h('div', { class: 'answer' }, markdown(m.content, m.streaming && !!m.content)));
    if (m.files && m.files.length) kids.push(fileList(m));
    // later steps (after a web search) think again below text that is already shown
    if (m.content && (phase === 'tools' || (!hasReasoning && (stillThinking || wrapping))))
      kids.push(h('p', { class: 'waiting', text: wrapping ? 'Wrapping up…' : phase === 'tools' ? 'Using tools…' : 'Thinking…' }));
    if (!m.streaming && !m.content && !m.error && !m.stopped && hasReasoning)
      kids.push(h('p', { class: 'note-warn', text: 'No answer came back; the thinking is above. Ask again, or raise Answer Output Length or Thinking Complexity.' }));
    if (!m.streaming) kids.push(footer(m));
    return h('div', { class: 'msg-ai', 'data-id': m.id }, kids);
  }

  // Thinking is always shown; Shrink folds it to one line, Expand opens it again.
  function thinkNode(m, phase) {
    const stillThinking = phase === 'thinking', wrapping = phase === 'closing';
    const open = !ui.thinkShrunk[m.id];
    const used = m.think_used || 0, budget = m.think_budget || 0;
    const count = budget ? `${tokensLabel(used)} / ${tokensLabel(budget)}` : (used ? `${tokensLabel(used)} tokens` : '');
    const status = wrapping ? (m.content ? 'Writing final answer…' : 'Wrapping up…')
      : stillThinking ? 'Thinking…' + (count ? ' ' + count : '')
      : (m.think_secs != null ? `Thought for ${Math.max(1, Math.round(m.think_secs))}s` : 'Thought') + (used ? ` · ${tokensLabel(used)} tokens` : '');
    const frac = budget ? Math.min(1, used / budget) : 0;
    let meter = null;
    if (m.streaming && budget && phase !== 'answering') {
      const fill = h('div', { class: 'think-meter-fill' });
      fill.style.width = (frac * 100).toFixed(1) + '%';    // set from script: the CSP blocks style="" attributes
      meter = h('div', { class: 'think-meter' + (frac >= 0.8 ? ' near' : ''), role: 'progressbar', 'aria-label': 'Thinking budget used',
        'aria-valuemin': '0', 'aria-valuemax': String(budget), 'aria-valuenow': String(Math.min(used, budget)) }, fill);
    }
    const lastLine = (m.reasoning || '').trim().split('\n').filter((l) => l.trim()).pop() || '';
    return h('div', { class: 'think' + (open ? ' open' : '') },
      h('button', { type: 'button', class: 'think-toggle', 'aria-expanded': String(open),
        onclick: () => { ui.thinkShrunk[m.id] = open; replaceMessage(m); } },
      h('span', { class: 'think-title', text: 'Thinking' }), h('span', { class: 'think-status', text: status }),
      h('span', { class: 'think-verb' }, open ? 'Shrink' : 'Expand', withClass(icon('chevron'), 'chev'))),
      meter,
      open ? thinkBox(m) : h('p', { class: 'think-line', text: lastLine }));
  }

  // The thinking box follows new text while it streams, unless the reader has scrolled up in it;
  // scrolling back to the bottom (or "Jump to latest") follows again. Only the reader's own input
  // (wheel, touch, keys, dragging the scrollbar) changes that: our auto-scroll fires scroll events
  // too, at a moment that differs between browsers, so a scroll event alone proves nothing.
  const readerScrolling = (id) => performance.now() - (ui.thinkTouched[id] || -1e9) < 1000;
  const thinkAtEnd = (box) => box.scrollHeight - box.scrollTop - box.clientHeight < 24;
  function thinkBox(m) {
    const box = h('div', { class: 'think-text', 'data-think': m.id }, m.reasoning);
    const live = m.streaming && m.phase !== 'answering';
    if (!live) return box;
    const jump = h('button', { type: 'button', class: 'think-jump', hidden: true,
      onclick: () => { ui.thinkFollow[m.id] = true; box.scrollTop = box.scrollHeight; jump.hidden = true; } }, 'Jump to latest ↓');
    const reader = () => { ui.thinkTouched[m.id] = performance.now(); };
    for (const type of ['wheel', 'touchmove', 'keydown', 'pointerdown']) box.addEventListener(type, reader, { passive: true });
    box.addEventListener('scroll', () => {
      if (!readerScrolling(m.id)) return;    // not the reader: our own scroll, or layout
      const atEnd = thinkAtEnd(box);
      ui.thinkFollow[m.id] = atEnd;
      jump.hidden = atEnd;
    });
    return h('div', { class: 'think-live' }, box, jump);
  }
  function placeThinkBox(el, m, prevTop) {
    const box = el.querySelector('.think-text');
    if (!box) return;
    const follow = ui.thinkFollow[m.id] !== false;
    if (m.streaming && follow) box.scrollTop = box.scrollHeight;
    else if (prevTop != null) box.scrollTop = prevTop;
    const jump = el.querySelector('.think-jump');
    if (jump) jump.hidden = follow || thinkAtEnd(box);    // nothing below to jump to: never shown
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
      f.lines != null ? plural(f.lines, 'line') : null,
      f.bytes != null ? fileSize(f.bytes) : null].filter(Boolean).join(' · ');
    const previewBtn = h('button', { type: 'button', class: 'btn', 'aria-expanded': String(open),
      'aria-label': `${open ? 'Hide' : 'Preview'} ${f.name}`,
      onclick: async () => {
        ui.fileOpen[key] = !open;
        if (!open && typeof ui.fileText[key] !== 'string') {
          try { await fileContent(cid, f); } catch (e) { ui.fileText[key] = { error: e.message }; }
        }
        replaceMessage(m);
      } }, icon('eye'), h('span', { text: open ? 'Hide' : 'Preview' }));
    const download = h('a', { class: 'btn', href: fileUrl(cid, f), download: f.name, 'aria-label': `Download ${f.name}` },
      icon('download'), h('span', { text: 'Download' }));
    const text = ui.fileText[key];
    const preview = !open ? null
      : typeof text === 'string' ? h('div', { class: 'file-preview' }, codeBlock(text, f.language, true))
      : text && text.error ? h('p', { class: 'file-desc', text: `Couldn't load the file: ${text.error}` })
      : h('p', { class: 'file-desc', text: 'Loading…' });
    return h('div', { class: 'file-card' },
      h('div', { class: 'file-head' },
        withClass(icon('file'), 'file-icon'),
        h('div', { class: 'file-info' },
          h('span', { class: 'file-name', text: f.name }),
          meta ? h('span', { class: 'file-meta', text: meta }) : null),
        h('div', { class: 'file-actions' }, previewBtn, copyButton(() => fileContent(cid, f), `Copy ${f.name}`, 'btn'), download)),
      f.description ? h('p', { class: 'file-desc', text: f.description }) : null,
      preview);
  }

  function footer(m) {
    const bits = [];
    if (m.error) bits.push(h('span', { class: 'err', text: m.error }));
    if (m.stopped) bits.push(h('span', { text: 'Stopped' }));
    const closed = { user: 'Closed out', budget: 'Thinking budget reached · wrapped up',
      length: 'Ran out of room while thinking · wrapped up',
      steps: 'Tool step limit reached · wrapped up' }[m.closed];
    if (closed) bits.push(h('span', { class: 'warn', text: closed }));
    if (m.truncated) bits.push(h('span', { class: 'warn', text: 'Cut off at the length limit · try a longer Answer Output Length' }));
    if (m.duration != null) bits.push(h('span', { text: `${m.duration.toFixed(1)} s` }));
    const u = m.usage || {};
    if (u.completion_tokens) bits.push(h('span', { text: `${tokensLabel(u.completion_tokens)} tokens` }));
    const c = active();
    const calls = c && m.turn ? (c.feed || []).filter((e) => e.turn === m.turn).length : 0;
    if (calls) bits.push(h('button', { type: 'button', class: 'link-btn', onclick: () => showCalls(m.turn) }, plural(calls, 'tool call')));
    bits.push(h('button', { type: 'button', class: 'link-btn', onclick: () => openRaw(m) }, 'View raw'));
    return h('div', { class: 'msg-foot' }, bits);
  }

  // Replace keep's siblings with fresh's siblings, in order, leaving keep where it is.
  function swapAround(keep, fresh) {
    const parent = keep.parentNode, fparent = fresh.parentNode;
    for (const c of [...parent.childNodes]) if (c !== keep) c.remove();
    let before = true;
    for (const c of [...fparent.childNodes]) {
      if (c === fresh) { before = false; continue; }
      if (before) parent.insertBefore(c, keep); else parent.append(c);
    }
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
          // A live thinking box is kept, not rebuilt: a new element would drop the reader's
          // scroll mid-gesture, so they could never scroll back down to the latest text.
          const oldLive = old.querySelector('.think-live'), newLive = el.querySelector('.think-live');
          if (oldLive && newLive) {
            // swap everything around it, so the box itself never leaves the page (moving it resets its scroll)
            const box = oldLive.querySelector('.think-text');
            const shown = box.textContent;
            if (msg.reasoning.startsWith(shown)) { if (msg.reasoning.length > shown.length) box.append(msg.reasoning.slice(shown.length)); }
            else box.textContent = msg.reasoning;     // a retract rewrote it
            swapAround(oldLive, newLive);
            swapAround(oldLive.closest('.think'), newLive.closest('.think'));
            placeThinkBox(old, msg, null);
            continue;
          }
          old.replaceWith(el);
          placeThinkBox(el, msg, prevTop);
        }
      }
      rafPending = new Set();
      if (nearBottom) scrollToBottom();
    });
  }
  const page = () => document.scrollingElement || document.documentElement;
  function isNearBottom() { const sc = page(); return sc.scrollHeight - sc.scrollTop - sc.clientHeight < 160; }
  function scrollToBottom() { const sc = page(); sc.scrollTop = sc.scrollHeight; }

  // ------------------------------------------------------------------ tool feed (sidebar tab)
  const STATUS = { ok: 'OK', cached: 'Cached', running: 'Running', blocked: 'Blocked', refused: 'Refused', error: 'Error', stopped: 'Stopped' };
  const isWebTool = (name) => {
    const t = (tools.list || []).find((x) => x.name === name);
    return t ? !!t.web : name === 'web_search' || name === 'fetch_url';
  };
  // in: stayed on this box · out: went to the public web · flag: went through with a warning · bad: blocked or failed
  function callKind(ev) {
    if (ev.status === 'running' || ev.status === 'stopped') return 'run';
    if (ev.status === 'blocked' || ev.status === 'refused' || ev.status === 'error') return 'bad';
    const f = ev.feed || {};
    if (ev.warning || f.tone === 'warn' || f.tone === 'bad') return 'flag';
    return ev.lookup || isWebTool(ev.tool) ? 'out' : 'in';
  }

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

  // Details: a tool's own readable sections (run_code: code, stdin, stdout, stderr), else the arguments
  // the model sent; then the text returned to the model, with the untrusted-content markers coloured.
  function callDetails(ev, kind) {
    const f = ev.feed || {};
    const out = [];
    if (ev.reason && kind === 'bad') out.push(h('p', { class: 'call-note bad', text: ev.reason }));
    if (ev.warning) out.push(h('p', { class: 'call-note flag', text: ev.warning }));
    if (f.headline) out.push(h('p', { class: 'call-headline', text: f.headline }));
    if (f.sections && f.sections.length) {
      for (const s of f.sections) {
        const lang = s.language || (/^Code/.test(s.title) && ev.args && ev.args.language) || (ev.tool === 'output_file' && /\.(\w+)$/.test(s.title) ? s.title.split('.').pop() : '');
        out.push(h('span', { class: 'call-h', text: s.title }), h('pre', { class: 'call-pre' }, highlight(s.text, lang)));
      }
    } else {
      const args = typeof ev.args === 'string' ? ev.args : JSON.stringify(ev.args || {}, null, 2);
      out.push(h('span', { class: 'call-h', text: 'Arguments the model sent' }), h('pre', { class: 'call-pre' }, highlight(args, 'json')));
    }
    if (ev.result) out.push(h('span', { class: 'call-h', text: 'Returned to the model' }), h('pre', { class: 'call-pre' }, highlight(ev.result, 'res')));
    return h('div', { class: 'call-more' }, out);
  }

  function callRow(ev) {
    const f = ev.feed || {};
    const kind = callKind(ev);
    const label = ev.status === 'ok' && f.pill ? f.pill : STATUS[ev.status] || ev.status;
    const open = ui.callOpen.has(ev.id);
    const markName = { in: 'mark-in', out: 'mark-out', flag: 'mark-flag', bad: 'mark-bad', run: 'mark-run' }[kind];
    return h('li', { class: 'call', 'data-ev': ev.id },
      h('button', { type: 'button', class: 'call-btn', 'aria-expanded': String(open),
        onclick: () => { open ? ui.callOpen.delete(ev.id) : ui.callOpen.add(ev.id); renderFeed(); } },
      h('span', { class: 'call-mark ' + kind, title: { in: 'Stayed on this box', out: 'Went to the public web', flag: 'Flagged', bad: 'Blocked or failed', run: 'Running' }[kind] }, icon(markName)),
      h('span', { class: 'call-body' },
        h('span', { class: 'call-top' }, h('span', { class: 'call-tool mono', text: ev.tool }), h('span', { class: 'call-time', text: ev.started ? clock(ev.started) : '' })),
        h('span', { class: 'call-sum', text: f.summary || ev.summary || '' }),
        h('span', { class: 'call-foot' },
          h('span', { class: 'chip ' + (kind === 'run' ? '' : kind), text: label }),
          h('span', { class: 'call-dest', text: feedDetail(ev) })))),
      open && ev.status !== 'running' ? callDetails(ev, kind) : null);
  }

  function groupPills(evs) {
    const n = { ok: 0, flag: 0, bad: 0, run: 0 };
    for (const e of evs) { const k = callKind(e); n[k === 'in' || k === 'out' ? 'ok' : k]++; }
    const pills = [h('span', { class: 'tpill calls', text: plural(evs.length, 'call') })];
    const add = (k, word) => { if (n[k]) pills.push(h('span', { class: 'tpill ' + k, title: `${n[k]} ${word}`, 'aria-label': `${n[k]} ${word}`, text: String(n[k]) })); };
    add('ok', 'succeeded'); add('flag', 'flagged'); add('bad', 'failed'); add('run', 'running');
    return pills;
  }

  function renderFeed() {
    if (ui.sideTab !== 'feed') return;
    const c = active();
    const events = (c && c.feed) || [];
    const lastTurn = events.reduce((n, e) => Math.max(n, e.turn), 0);
    const used = events.filter((e) => e.turn === lastTurn && e.lookup).length;
    const lastMsg = c && [...c.messages].reverse().find((m) => m.role === 'assistant');
    const limit = (lastMsg && lastMsg.limit) || (ui.server ? ui.server.depths[settings().depth] : 20) || 20;
    $('lookups-label').textContent = events.length ? `${used} of ${limit} lookups in the last answer` : 'No lookups yet';
    $('lookups-fill').style.width = Math.min(100, Math.round((used / limit) * 100)) + '%';
    const bar = $('lookups-bar');
    bar.setAttribute('aria-valuemax', String(limit));
    bar.setAttribute('aria-valuenow', String(used));
    const list = $('feed-list');
    if (!events.length) { list.replaceChildren(h('p', { class: 'feed-empty', text: 'No tool calls in this chat yet.' })); return; }
    // One group per question. The current question stays open; earlier ones fold up as soon as a new
    // question is sent (its number is on the answer the server created), and open again on a click.
    const current = Math.max(lastTurn, currentTurn(c));
    const turns = [...new Set(events.map((e) => e.turn))].sort((a, b) => b - a);
    const opened = ui.feedOpenTurns[c.id] || (ui.feedOpenTurns[c.id] = new Set());
    const shut = ui.feedShutTurns[c.id] || (ui.feedShutTurns[c.id] = new Set());
    list.replaceChildren(...turns.map((t) => {
      const evs = events.filter((e) => e.turn === t).reverse();
      const open = t === current ? !shut.has(t) : opened.has(t);
      const flip = () => {
        const set = t === current ? shut : opened;
        if ((t === current) === open) set.add(t); else set.delete(t);
        renderFeed();
      };
      return h('section', { class: 'fgroup' + (open ? ' open' : ''), 'data-turn': t },
        h('button', { type: 'button', class: 'fgroup-head', 'aria-expanded': String(open), onclick: flip },
          h('span', { class: 'fgroup-text' },
            h('span', { class: 'fgroup-q', text: questionFor(c, t) || 'Earlier question' }),
            h('span', { class: 'fgroup-pills' }, groupPills(evs))),
          withClass(icon('chevron'), 'chev')),
        open ? h('ul', { class: 'call-list' }, evs.map(callRow)) : null);
    }));
  }

  // "N tool calls" under an answer: open the feed tab at that question's group.
  function showCalls(turn) {
    const c = active();
    if (!c) return;
    (ui.feedOpenTurns[c.id] || (ui.feedOpenTurns[c.id] = new Set())).add(turn);
    if (ui.feedShutTurns[c.id]) ui.feedShutTurns[c.id].delete(turn);
    setSideTab('feed');
    const g = $('feed-list').querySelector(`[data-turn="${turn}"]`);
    if (g) g.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
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

  function upsertEvent(chat, ev) {
    const i = chat.feed.findIndex((e) => e.id === ev.id);
    if (i >= 0) chat.feed[i] = ev; else chat.feed.push(ev);
  }

  // ------------------------------------------------------------------ message box
  const composer = $('composer');
  function autosize() {
    composer.style.height = 'auto';
    composer.style.height = Math.max(118, Math.min(240, composer.scrollHeight + 2)) + 'px';
  }
  function renderComposer() {
    const c = active();
    const busy = !!(c && (c.busy || ui.streams[c.id]));
    const send = $('send');
    send.classList.toggle('stop', busy);
    send.setAttribute('aria-label', busy ? 'Stop answering' : 'Send message');
    send.title = busy ? 'Stop answering' : 'Send message';
    const close = $('closeout');
    close.hidden = !busy;
    const wrapping = busy && c && ui.closing[c.id];
    close.disabled = !!wrapping;
    close.textContent = wrapping ? 'Writing final answer…' : 'Close out';
    $('attachments').replaceChildren(...ui.attachments.map((a, i) => h('span', { class: 'att', title: a.name },
      h('span', { class: 'att-name', text: a.name }), h('span', { class: 'att-size', text: fileSize(a.size) }),
      h('button', { type: 'button', 'aria-label': 'Remove ' + a.name, onclick: () => { ui.attachments.splice(i, 1); renderComposer(); } }, icon('x')))));
    $('top-row').hidden = !busy && !ui.attachments.length;
    renderQbar();
  }

  // The bar under the message box: web on/off, system prompt, thinking, and the chat settings gear.
  function renderQbar() {
    const s = settings();
    const web = $('q-web');
    web.setAttribute('aria-pressed', String(!!s.internet));
    $('q-web-label').textContent = s.internet ? 'Web on' : 'Web off';
    web.title = s.internet ? 'Web access is on: Perseus may search and read public pages' : 'Web access is off';
    $('q-persona-label').textContent = cap(s.persona || 'researcher');
    $('q-think-wrap').hidden = !info().thinks;
    $('q-think-label').textContent = cap(THINK[s.thinking] ? s.thinking : 'medium') + ' thinking';
    for (const [name, wrap] of [['persona', 'q-persona'], ['think', 'q-think']]) {
      const open = ui.menu === name;
      $(wrap).setAttribute('aria-expanded', String(open));
      const menu = $(wrap + '-menu');
      menu.hidden = !open;
      if (!open) continue;
      if (name === 'persona') {
        const order = PERSONA_ORDER.filter((p) => (ui.server.personas || []).includes(p));
        menu.replaceChildren(...order.map((p) => menuItem(cap(p), PERSONA_SHORT[p], s.persona === p, () => patchSettings({ persona: p }))));
      } else {
        menu.replaceChildren(...Object.keys(THINK).filter((l) => (ui.server.thinking || []).includes(l)).map((l) =>
          menuItem(cap(l), THINK[l][1] ? `Up to ${THINK[l][0]} reasoning tokens.` : 'No reasoning limit.', s.thinking === l, () => patchSettings({ thinking: l }))));
      }
    }
  }
  function menuItem(label, sub, on, onPick) {
    const pick = onPick;
    return h('button', { type: 'button', role: 'menuitemradio', class: 'menu-item', 'aria-checked': String(on),
      onclick: () => { ui.menu = null; pick(); } }, h('b', { text: label }), sub ? h('span', { text: sub }) : null);
  }
  function toggleMenu(name, e) {
    e.stopPropagation();
    ui.menu = ui.menu === name ? null : name;
    renderQbar();
    if (ui.menu) { const first = $('q-' + name + '-menu').querySelector('[aria-checked="true"]') || $('q-' + name + '-menu').firstChild; if (first) first.focus(); }
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
    const here = () => chat.id === ui.activeId && ui.view === 'chat';
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
            if (here()) { renderMessages(); renderHeader(); renderFeed(); scrollToBottom(); }
            renderChatList();
          } else if (ev.type === 'text' && current) {
            if (current.phase !== 'closing') current.phase = ev.thought ? 'thinking' : 'answering';
            if (ev.thought) { current.reasoning += ev.text; if (ev.think_used != null) current.think_used = ev.think_used; }
            else {
              if (!current.content && current.reasoning && current.think_secs == null) current.think_secs = (Date.now() / 1000) - current.started;
              current.content += ev.text;
            }
            if (here()) replaceMessage(current);
          } else if (ev.type === 'retract' && current) {
            // text shown as answer that turned out to be thinking: move it into the thinking box
            if (current.content.endsWith(ev.text)) current.content = current.content.slice(0, current.content.length - ev.text.length);
            if (!current.content) current.think_secs = null;
            current.reasoning += ev.text;
            if (ev.think_used != null) current.think_used = ev.think_used;
            if (current.phase !== 'closing') current.phase = 'thinking';
            if (here()) replaceMessage(current);
          } else if (ev.type === 'phase' && current) {
            current.phase = ev.phase;
            if (here()) replaceMessage(current);
          } else if (ev.type === 'file' && current) {
            current.files = current.files || [];
            if (!current.files.some((f) => f.id === ev.file.id)) current.files.push(ev.file);
            if (here()) replaceMessage(current);
          } else if (ev.type === 'tool') {
            upsertEvent(chat, ev.event);
            if (chat.id === ui.activeId) renderFeed();
          } else if (ev.type === 'done') {
            finished = true;
            const i = chat.messages.findIndex((m) => current && m.id === current.id);
            if (i >= 0) chat.messages[i] = ev.message;
            if (ev.context_used) chat.ctxUsed = ev.context_used;
            current = ev.message;
            if (here()) { replaceMessage(current); renderContext(); }
            if (chat.id === ui.activeId) renderFeed();
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
      full.feed = full.feed || [];
      ui.chats[id] = full;
      if (id === ui.activeId) renderAll();
      if (full.busy && !ui.streams[id]) setTimeout(() => reloadChat(id), 1500);
    } catch (e) { /* chat deleted meanwhile */ }
  }

  // ------------------------------------------------------------------ attachments
  const MAX_UPLOAD = 60 * 1024 * 1024;     // all attachments of one message together
  async function addFiles(files) {
    for (const f of files) {
      if (ui.attachments.length >= 5) { banner('Up to 5 files per message.'); break; }
      const used = ui.attachments.reduce((n, a) => n + a.size, 0);
      if (used + f.size > MAX_UPLOAD) { banner(`${f.name} (${fileSize(f.size)}) would go over the 60 MB upload limit.`); continue; }
      const text = await f.text();
      if (text.includes('\u0000')) { banner(`${f.name} isn't a text file; only text files can be attached.`); continue; }
      ui.attachments.push({ name: f.name, text, size: f.size });
    }
    renderComposer();
  }

  // ------------------------------------------------------------------ chats
  async function createChat() {
    const full = await api('POST', '/api/chats');
    full.ctxUsed = 0;
    full.feed = full.feed || [];
    ui.chats[full.id] = full;
    ui.list.unshift({ id: full.id, title: full.title, busy: false, messages: 0 });
    ui.activeId = full.id;
    store.set('active', full.id);
    renderChatList();
    return full;
  }

  async function newChat() {
    const c = active();
    if (!c || c.messages.length) await createChat().catch((e) => banner(e.message));
    if (ui.view !== 'chat') go('chat'); else renderAll();
    composer.focus();
  }

  async function openChat(id) {
    ui.activeId = id;
    store.set('active', id);
    if (!ui.chats[id]) await reloadChat(id);
    if (ui.view !== 'chat') go('chat'); else { renderAll(); scrollToBottom(); }
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
  async function deleteAll(btn) {
    if (!deleteArmed) {
      btn.classList.add('confirm');
      btn.textContent = 'Press again to delete all';
      deleteArmed = setTimeout(() => { deleteArmed = null; btn.classList.remove('confirm'); btn.textContent = 'Delete all'; }, 4000);
      return;
    }
    clearTimeout(deleteArmed); deleteArmed = null;
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
    renderQbar(); renderSettings(); renderChatSettings(); renderFeed();
  }

  function seg(label, options) {
    return h('div', { class: 'seg', role: 'group', 'aria-label': label }, options.map((o) =>
      h('button', { type: 'button', 'aria-pressed': String(!!o.on), disabled: o.disabled, title: o.title, onclick: o.pick },
        o.icon ? icon(o.icon) : null, o.label)));
  }

  const appliedContext = () => (ui.server.context_options.find((o) => o.current) || {}).id;

  // One box per setting: bold title, a short light description beside it, then the control.
  // The Settings page and the Chat settings dialog build theirs from the same list.
  const BOXES = {
    appearance: ['Dashboard Appearance', 'Light or dark theme.', () => seg('Theme', [
      { label: 'Light', icon: 'sun', on: ui.theme !== 'dark', pick: () => setTheme('light') },
      { label: 'Dark', icon: 'moon', on: ui.theme === 'dark', pick: () => setTheme('dark') }])],
    context: ['Chat Context Window', 'Alter the context window.', () => {
      const pend = (ui.pending && ui.pending.context) || appliedContext();
      return seg('Context window', ui.server.context_options.map((o) => ({ label: o.label, on: pend === o.id, disabled: !o.available,
        title: o.available ? `${o.tokens.toLocaleString()} tokens · takes effect after a restart` : `Doesn't fit on this box (up to ${info().capacity.toLocaleString()} tokens)`,
        pick: () => setPending(o.id) })));
    }],
    length: ['Answer Output Length', 'Change the verbosity of the output.', () => seg('Answer output length',
      ui.server.length.map((l) => ({ label: cap(l), on: settings().length === l, pick: () => patchSettings({ length: l }) })))],
    thinking: ['Thinking Complexity', 'Change the token maximum of reasoning.', () => seg('Thinking complexity',
      Object.keys(THINK).filter((l) => ui.server.thinking.includes(l)).map((l) => ({ label: THINK[l][0], on: settings().thinking === l,
        title: cap(l), disabled: !info().thinks, pick: () => patchSettings({ thinking: l }) })))],
    creativity: ['Creativity', 'Alter temperature of thinking freedom.', () => seg('Creativity',
      ui.server.creativity.map((c) => ({ label: cap(c), on: settings().creativity === c, pick: () => patchSettings({ creativity: c }) })))],
    web: ['Web Access', 'Allow the agent access to the internet.', () => seg('Web access', [
      { label: 'On', on: !!settings().internet, pick: () => patchSettings({ internet: true }) },
      { label: 'Off', on: !settings().internet, pick: () => patchSettings({ internet: false }) }])],
    depth: ['Web Research Depth', 'Change maximum web lookups per answer.', () => seg('Web research depth',
      Object.entries(ui.server.depths).map(([id, n]) => ({ label: `${cap(id)} · ${n}`, on: settings().depth === id, pick: () => patchSettings({ depth: id }) })))],
    engines: ['Search engines', 'Search interfaces Perseus attempts to use.', () => h('div', { class: 'engines' },
      Object.entries(settings().engines || {}).map(([id, on]) => h('button', { type: 'button', role: 'checkbox', class: 'check', 'aria-checked': String(on),
        onclick: () => patchSettings({ engines: { [id]: !on } }) }, h('span', { class: 'check-box', 'aria-hidden': 'true' }, icon('check')), ENGINE_NAMES[id] || id))), true],
    persona: ['System Prompt', "Perseus's model will assume this role to better tailor answers.", (where) => {
      const s = settings();
      const order = PERSONA_ORDER.filter((p) => (ui.server.personas || []).includes(p));
      return [h('div', { class: 'persona-grid', role: 'group', 'aria-label': 'System prompt' }, order.map((p) =>
        h('button', { type: 'button', class: 'persona', 'aria-pressed': String(s.persona === p), onclick: () => patchSettings({ persona: p }) },
          icon(PERSONA_ICON[p] || 'pen'), cap(p)))),
      s.persona === 'custom' ? customEditor(where) : null];
    }, true],
    history: ['Chat History', 'Export to another session or clear history.', () => [
      h('a', { class: 'btn', href: '/api/export', download: 'perseus-chats.json' }, icon('download'), 'Export'),
      h('button', { type: 'button', class: 'btn btn-danger', onclick: (e) => deleteAll(e.currentTarget) }, 'Delete all')]],
  };
  const PAGE_BOXES = ['appearance', 'context', 'length', 'thinking', 'creativity', 'web', 'depth', 'engines', 'persona', 'history'];
  const CHAT_BOXES = ['web', 'thinking', 'persona', 'depth', 'engines', 'creativity', 'length'];

  function settingBox(key, where) {
    const [title, desc, make, wide] = BOXES[key];
    const build = make;
    const id = `${where}-${key}`;
    return h('section', { class: 'setting', 'aria-labelledby': id },
      h('div', { class: 'set-head' }, h(where === 'page' ? 'h2' : 'h3', { id, text: title }), h('p', { text: desc })),
      h('div', { class: 'set-ctl' + (wide ? ' wide' : '') }, build(where)));
  }

  // The custom prompt keeps its own element while you type: settings re-render around it, never through it.
  const editors = {};
  let customTimer = null;
  function customEditor(where) {
    let ed = editors[where];
    if (!ed) {
      const ta = h('textarea', { maxlength: String(CUSTOM_MAX), 'aria-label': 'Custom system prompt', placeholder: 'Perseus is your follower, tell him how to think' });
      const left = h('span', { class: 'char-pill', 'aria-live': 'polite' });
      ta.addEventListener('input', () => {
        left.textContent = `${(CUSTOM_MAX - ta.value.length).toLocaleString()} characters left`;
        clearTimeout(customTimer);
        customTimer = setTimeout(async () => {
          try { const r = await api('PATCH', '/api/settings', { custom_prompt: ta.value }); ui.server.settings = r.settings; } catch (e) { banner(e.message); }
        }, 600);
      });
      ed = editors[where] = { wrap: h('div', { class: 'custom-wrap' }, ta, left), ta, left };
    }
    if (document.activeElement !== ed.ta) ed.ta.value = settings().custom_prompt || '';
    ed.left.textContent = `${(CUSTOM_MAX - ed.ta.value.length).toLocaleString()} characters left`;
    return ed.wrap;
  }
  const typingIn = (el) => el.contains(document.activeElement) && document.activeElement.tagName === 'TEXTAREA';

  function renderSettings() {
    if (!ui.server || ui.view !== 'settings') return;
    if (!typingIn($('settings-list'))) $('settings-list').replaceChildren(...PAGE_BOXES.map((k) => settingBox(k, 'page')));
    const inf = info();
    $('model-pills').replaceChildren(...[['Model', inf.model], ['GPUs', inf.hardware], ['Precision', inf.precision]].map(([k, v]) =>
      h('span', { class: 'model-pill', title: `${k}: ${v}` }, h('span', { class: 'k', text: k }), h('span', { class: 'v', text: v || '—' }))));
    renderServerBar();
  }

  function renderServerBar() {
    const restarting = !!ui.server.restarting;
    const label = (id) => { const o = ui.server.context_options.find((x) => x.id === id); return o ? o.label : String(id); };
    const p = ui.pending;
    $('pending-label').textContent = restarting ? 'Restarting the model…'
      : p ? `Context window ${label(appliedContext())} → ${label(p.context)} takes effect after a restart.`
      : info().last_restart_error ? 'Last restart failed: ' + info().last_restart_error
      : 'No changes waiting for a restart.';
    $('discard').hidden = !p;
    $('discard').disabled = restarting;
    $('apply-label').textContent = restarting ? 'Restarting…' : 'Restart model';
    $('apply').disabled = restarting;
  }

  function setPending(context) {
    ui.pending = context === appliedContext() ? null : { context };
    renderSettings();
  }

  async function applyModel() {
    const target = (ui.pending && ui.pending.context) || appliedContext();
    const tokens = (ui.server.context_options.find((o) => o.id === target) || {}).tokens || info().context;
    try {
      await api('POST', '/api/model/apply', { context: tokens });
      ui.server.restarting = true;
      ui.pending = null;
    } catch (e) { banner(e.message); }
    renderSettings(); renderStatus();
    pollSoon();
  }

  // ---- chat settings dialog (the gear under the message box)
  function renderChatSettings() {
    const dlg = $('chat-settings');
    if (!dlg.open || !ui.server || typingIn($('cs-body'))) return;
    $('cs-body').replaceChildren(...CHAT_BOXES.map((k) => settingBox(k, 'dlg')));
  }
  function openChatSettings() {
    ui.menu = null; renderQbar();
    $('chat-settings').showModal();
    renderChatSettings();
  }

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
      $('raw-prompt').replaceChildren(...highlight(r.prompt, 'tmpl'));
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
      bits.push(plural(r.steps, 'step'));
      $('raw-tokens').textContent = bits.join(' · ');
      if (r.note) { $('raw-note').hidden = false; $('raw-note').textContent = r.note; }
    } catch (e) {
      $('raw-prompt').textContent = 'Not available: ' + e.message;
    }
  }

  // ------------------------------------------------------------------ Agent Tools
  // Read-only: what the model can call, grouped by where a call goes. Data from /api/tools.
  const tools = { list: null, errors: [], loading: false, error: null, sel: store.get('tool', null), filter: '' };
  const LANES = [
    { id: 'box', title: 'On this box', sub: '', dot: 'in', test: (t) => !t.web && t.requires !== 'sandbox' },
    { id: 'web', title: 'Public web', sub: 'read-only, through the gateway', dot: '', test: (t) => t.web && t.requires !== 'sandbox' },
    { id: 'sandbox', title: 'Throwaway sandbox', sub: '', dot: 'in', test: (t) => t.requires === 'sandbox' },
  ];

  async function loadTools() {
    if (tools.loading) return;
    tools.loading = true; tools.error = null;
    if (!tools.list) renderTools();
    try {
      const t = await api('GET', '/api/tools');
      tools.list = t.tools; tools.errors = t.errors || [];
    } catch (e) { tools.error = e.message; }
    tools.loading = false;
    renderTools(); renderNav();
  }

  function renderTools() {
    if (ui.view !== 'tools') return;
    const body = $('dev-body');
    if (!tools.list) {
      body.replaceChildren(tools.error
        ? h('div', { class: 'dev-empty' }, h('p', { text: "Couldn't load the tools: " + tools.error }),
          h('button', { type: 'button', class: 'btn', onclick: loadTools }, 'Try again'))
        : h('div', { class: 'skeleton', 'aria-busy': 'true' }));
      return;
    }
    const all = tools.list;
    const q = tools.filter.trim().toLowerCase();
    const shown = all.filter((t) => !q || t.name.toLowerCase().includes(q) || (t.description || '').toLowerCase().includes(q));
    const sel = all.find((t) => t.name === tools.sel) || shown[0] || all[0];
    const input = h('input', { type: 'search', id: 'tool-filter', class: 'tool-filter', placeholder: 'Filter tools', 'aria-label': 'Filter tools', autocomplete: 'off' });
    input.value = tools.filter;
    input.addEventListener('input', () => {
      tools.filter = input.value;
      const at = input.selectionStart;
      renderTools();
      const again = $('tool-filter'); again.focus(); again.setSelectionRange(at, at);
    });
    const groups = LANES.map((lane) => {
      const list = shown.filter(lane.test);
      if (!list.length) return null;
      return h('section', { class: 'tool-group' },
        h('h2', {}, h('span', { class: 'lane-dot ' + lane.dot, 'aria-hidden': 'true' }), lane.title,
          lane.sub ? h('span', { class: 'sub', text: lane.sub }) : null),
        h('div', { class: 'tool-grid' }, list.map((t) => h('button', { type: 'button', class: 'tool-tile', 'aria-pressed': String(sel && sel.name === t.name),
          onclick: () => { tools.sel = t.name; store.set('tool', t.name); renderTools(); } },
          h('span', { class: 'name', text: t.name }), h('span', { class: 'short', text: (t.description || '').split(/(?<=\.)\s(?=[A-Z])/)[0] })))));
    }).filter(Boolean);
    body.replaceChildren(
      ...tools.errors.map((e) => h('div', { class: 'callout', role: 'alert' }, icon('alert'),
        h('div', {}, h('strong', { class: 'mono', text: e.file }), ' wasn’t loaded: ' + e.error))),
      h('div', { class: 'tools-head' }, h('p', { class: 'tools-lede', text: `${plural(all.length, 'tool')} the model can call. Pick one to see what it takes.` }), input),
      h('div', { class: 'tools-layout' },
        h('div', { class: 'tool-groups' }, groups.length ? groups
          : h('div', { class: 'dev-empty', text: all.length ? `Nothing matches “${tools.filter}”.` : 'No tools loaded.' })),
        sel ? toolSpec(sel) : null));
  }

  function toolSpec(t) {
    const lane = LANES.find((l) => l.test(t));
    return h('aside', { class: 'tool-spec', 'aria-label': t.name },
      h('div', { class: 'tool-spec-head' }, h('h2', { text: t.name }),
        h('span', { class: 'chip ' + (t.web && t.requires !== 'sandbox' ? 'out' : 'in'), text: lane ? lane.title : '' })),
      h('p', { text: t.description }),
      h('h3', { text: t.params.length ? 'Arguments' : 'Takes no arguments' }),
      t.params.length ? h('dl', { class: 'tool-args' }, t.params.flatMap((p) => [
        h('dt', {}, h('span', { class: 'mono', text: p.name }), h('span', { class: 't', text: p.type + (p.required ? '' : ' · optional') })),
        p.description ? h('dd', { text: p.description }) : null])) : null,
      h('div', { class: 'tool-limits', text: `Times out after ${t.timeout_s} s` + (t.source ? ` · ${t.source}` : '') }));
  }

  // ------------------------------------------------------------------ render all
  function renderAll() {
    applyTheme();
    const inChat = ui.view === 'chat';
    $('chat-view').hidden = !inChat;
    $('settings-view').hidden = ui.view !== 'settings';
    $('dev-view').hidden = ui.view !== 'tools';
    renderSideTabs(); renderChatList(); renderNav(); renderStatus(); renderHeader();
    if (inChat) { renderMessages(); renderComposer(); }
    renderFeed(); renderSettings(); renderTools();
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
      renderStatus(); renderChatList(); renderNav(); renderQbar();
      if (ui.view === 'settings') renderSettings();
      if (wasRestarting && !st.restarting) renderHeader();
    } catch (e) {
      if (ui.server) ui.server.status = { vLLM: { state: 'failed', reason: 'dashboard unreachable' } };
      renderStatus();
    }
    pollTimer = setTimeout(poll, ui.server && ui.server.restarting ? 1500 : 4000);
  }
  const pollSoon = () => { clearTimeout(pollTimer); pollTimer = setTimeout(poll, 400); };

  // ------------------------------------------------------------------ events
  $('logo-top').addEventListener('click', () => { if (ui.view !== 'chat') go('chat'); window.scrollTo({ top: 0, behavior: 'smooth' }); });
  $('new-chat').addEventListener('click', newChat);
  $('nav-tools').addEventListener('click', () => go('tools'));
  $('nav-settings').addEventListener('click', () => go('settings'));
  $('back').addEventListener('click', back);
  $('tab-chats').addEventListener('click', () => setSideTab('chats'));
  $('tab-feed').addEventListener('click', () => setSideTab('feed'));
  for (const id of ['tab-chats', 'tab-feed']) {
    $(id).addEventListener('keydown', (e) => {
      if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
      e.preventDefault();
      const next = ui.sideTab === 'feed' ? 'chats' : 'feed';
      setSideTab(next);
      $('tab-' + next).focus();
    });
  }
  $('q-web').addEventListener('click', () => patchSettings({ internet: !settings().internet }));
  $('q-persona').addEventListener('click', (e) => toggleMenu('persona', e));
  $('q-think').addEventListener('click', (e) => toggleMenu('think', e));
  $('q-gear').addEventListener('click', openChatSettings);
  document.addEventListener('click', (e) => {
    if (ui.menu && !$('q-' + ui.menu + '-wrap').contains(e.target)) { ui.menu = null; renderQbar(); }
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && ui.menu) { const was = ui.menu; ui.menu = null; renderQbar(); $('q-' + was).focus(); }
  });
  $('cs-close').addEventListener('click', () => $('chat-settings').close());
  $('cs-done').addEventListener('click', () => $('chat-settings').close());
  $('chat-settings').addEventListener('click', (e) => { if (e.target === $('chat-settings')) $('chat-settings').close(); });
  $('raw-close').addEventListener('click', () => $('raw').close());
  $('raw').addEventListener('click', (e) => { if (e.target === $('raw')) $('raw').close(); });
  $('discard').addEventListener('click', () => { ui.pending = null; renderSettings(); });
  $('apply').addEventListener('click', applyModel);
  $('send').addEventListener('click', onSend);
  $('closeout').addEventListener('click', onCloseOut);
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

  // ------------------------------------------------------------------ boot
  (async () => {
    ui.sideTab = store.get('sideTab', 'chats') === 'feed' ? 'feed' : 'chats';
    applyTheme();
    renderSideTabs();
    await poll();
    if (!ui.server) return;
    const remembered = store.get('active', null);
    ui.activeId = ui.list.some((c) => c.id === remembered) ? remembered : (ui.list[0] ? ui.list[0].id : null);
    if (ui.activeId) await reloadChat(ui.activeId);
    renderAll();
    autosize();
    scrollToBottom();
    composer.focus();
    loadTools();         // for the badge and for telling web calls from local ones in the feed
  })();
})();
