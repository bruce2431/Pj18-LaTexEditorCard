/* 编译日志卡（多卡片架构，2026-10-18）——自包含：
   不引用 PDF 卡的任何文件，自己实现 $ / esc / api（含反代门票 401 自愈）/ 渲染。
   数据源 = server.py 落盘账本 GET /api/history?book=<书稿名>（结构 {ok, records:[{ts,ok,elapsed,pages,by,error,log}]}，新→旧）。
   进入方式：
     · 宿主顶栏点「编译日志」tab —— 无 payload，显示账本最近一次（展开）；
     · PDF 卡编译失败时发 floria-wk-card-switch{card:'log', payload:{book,path,error,ts}}，
       宿主换帧后发 floria-wk-card-init{card:'log', payload} —— 按 payload.ts 定位并自动展开那次编译。
   卡间切换一律由宿主顶栏完成：本卡只收 init、不向宿主发 card-switch。 */
(function () {
  'use strict';

  const $ = id => document.getElementById(id);
  const EMBEDDED = window.parent !== window;

  /* ---------------- 基础设施：转义 / 时间 ---------------- */
  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }
  const two = n => (n < 10 ? '0' : '') + n;
  function fmtStamp(ts) {
    const d = ts ? new Date(ts * 1000) : new Date();
    return two(d.getMonth() + 1) + '-' + two(d.getDate()) + ' ' +
      two(d.getHours()) + ':' + two(d.getMinutes()) + ':' + two(d.getSeconds());
  }

  /* ---------------- API：卡片在 <base>/<cardId>/<entry>，API 在 <base>/api/ ---------------- */
  function apiUrl(url) {
    const s = String(url == null ? '' : url);
    if (!/^api\//.test(s)) return s;
    const p = (window.location && window.location.pathname) || '';
    const cut = p.lastIndexOf('/');
    const dir = cut >= 0 ? p.slice(0, cut) : p;        // <base>/<cardId>
    const cut2 = dir.lastIndexOf('/');
    const base = cut2 >= 0 ? dir.slice(0, cut2) : '';  // <base>
    return base + '/' + s;
  }
  // 反代门票（floria_bp）自愈：401 unauthorized = 网关进程换过、内存门票丢了，重签一次再试。
  let ticketP = null;
  function bpLabel() {
    const p = (window.location && window.location.pathname) || '';
    const m = /^\/(?:backend|bp)\/([^/]+)(?:\/|$)/.exec(p);
    if (!m) return '';
    try { return decodeURIComponent(m[1]); } catch (e) { return m[1]; }
  }
  function renewTicket() {
    const label = bpLabel();
    if (!label) return Promise.resolve(false);
    if (!ticketP) {
      ticketP = fetch('/gateway/backend?label=' + encodeURIComponent(label))
        .then(r => r.ok).catch(() => false)
        .then(ok => { ticketP = null; return ok; });
    }
    return ticketP;
  }
  async function api(url) {
    const u = apiUrl(url);
    const doFetch = async () => {
      try { return await fetch(u); }
      catch (e) { throw new Error('连不上本地服务器'); }
    };
    let r = await doFetch();
    if (r.status === 401) {
      const b = await r.clone().json().catch(() => ({}));
      if (b && b.error === 'unauthorized' && await renewTicket()) r = await doFetch();
    }
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.error || ('HTTP ' + r.status));
    return d;
  }

  /* ---------------- 状态 ---------------- */
  const S = {
    book: '',            // 当前书稿名（项目根相对 POSIX 路径）
    recs: [],            // 新 → 旧
    payload: null,       // floria-wk-card-init 带来的上下文 {book,path,error,ts}
    loaded: false,
  };
  const BY_TAG = { page: '页', chapter: '快速', ai: 'AI', disk: '回溯' };
  const BY_TITLE = {
    page: '在本页点「编译」产生的记录',
    chapter: '在本页点「编译」走按章并行快速路径产生的记录',
    ai: '会话里用 compile_book.py 编译产生的记录',
    disk: '由磁盘产物回溯：这次编译没有经过应用的编译账本（手工 latexmk / 旧版本 / 其他脚本），只能拿到时间、成败与页数，耗时不详',
  };
  let seq = 0;           // 并发装载的序号：后发起的覆盖先发起的（card-init 可能晚于首次装载）

  function bookFromPath(p) {
    p = String(p || '').replace(/\\/g, '/').replace(/^\/+/, '');
    return p.indexOf('/') < 0 ? '' : p.slice(0, p.lastIndexOf('/'));
  }

  /* ---------------- 书稿解析：payload → localStorage 上次打开的那篇 → 任一有 PDF 的书 ---------------- */
  async function resolveBook(payload) {
    let name = String((payload && payload.book) || '') || bookFromPath(payload && payload.path);
    if (!name) {
      try { name = localStorage.getItem('florialatex.lastbook') || ''; } catch (e) { /* 隐私模式 */ }
    }
    let books = null;
    try { books = await api('api/books'); } catch (e) { return name; }   // 服务器不可达：仍回已知名
    if (!Array.isArray(books)) books = [];
    const hit = books.find(b => b.name === name);
    if (!hit) {
      const pick = books.find(b => b.pdf && b.pdf.exists) || books[0];
      name = pick ? pick.name : '';
    }
    return name;
  }

  /* ---------------- 渲染 ---------------- */
  function renderEmpty(title, hint) {
    $('lg-list').innerHTML = '<div class="lg-empty"><p class="le-title">' + esc(title) + '</p>' +
      (hint ? '<p class="le-hint">' + esc(hint) + '</p>' : '') + '</div>';
  }
  function renderBanner() {
    const el = $('lg-banner');
    const p = S.payload;
    if (p && p.error) {
      el.textContent = '本次编译失败：' + p.error;
      el.classList.remove('hidden');
    } else {
      el.classList.add('hidden');
    }
  }
  function render() {
    const el = $('lg-list');
    const sec = '<div class="pane-sec"><span class="pane-sec-title">编译记录</span>' +
      '<span class="pane-sec-note">' + S.recs.length + '</span></div>';
    if (!S.recs.length) {
      el.innerHTML = sec + '<div class="lg-empty"><p class="le-title">还没有编译记录</p>' +
        '<p class="le-hint">在 PDF 卡点「编译」后，每次编译的结果与报错都会记在这里。</p></div>';
      return;
    }
    el.innerHTML = sec + S.recs.map((e, i) => `
      <div class="log-row${e.ok ? '' : ' bad'}" data-idx="${i}">
        <div class="log-head">
          <span class="log-mark">${e.ok ? '✓' : '✕'}</span>
          <span class="log-time">${esc(fmtStamp(e.ts))}</span>
          <span class="log-el"${e.elapsed == null ? ' title="这次编译的耗时没有记录（多为由磁盘回溯的旧编译）"' : ''}>${e.elapsed != null ? esc(e.elapsed + 's') : '耗时未知'}</span>
          ${e.pages != null ? '<span class="log-pages">' + esc(e.pages) + ' 页</span>' : ''}
          ${BY_TAG[e.by] ? '<span class="log-by" title="' + esc(BY_TITLE[e.by] || '') + '">' + BY_TAG[e.by] + '</span>' : ''}
          <span class="log-brief">${esc(e.error || '编译通过')}</span>
        </div>
        <pre class="log-body hidden">${esc(e.log || '(无输出)')}</pre>
      </div>`).join('');
    el.querySelectorAll('.log-row').forEach(row => {
      row.addEventListener('click', () => toggleRow(row));
    });
  }
  function toggleRow(row, forceOpen) {
    const body = row.querySelector('.log-body');
    if (!body) return;
    const open = forceOpen === undefined ? body.classList.contains('hidden') : !!forceOpen;
    body.classList.toggle('hidden', !open);
    row.classList.toggle('open', open);
  }
  // 定位要展开的那条：payload.ts 最近（±30s，编译响应紧跟账本落盘）→ 否则最新一条
  function targetIndex() {
    const p = S.payload;
    if (p && p.ts) {
      let best = -1, bd = Infinity;
      S.recs.forEach((r, i) => {
        const d = Math.abs((r.ts || 0) - p.ts);
        if (d < bd) { bd = d; best = i; }
      });
      if (best >= 0 && bd <= 30) return best;
    }
    return S.recs.length ? 0 : -1;
  }
  function autoExpand() {
    const rows = $('lg-list').querySelectorAll('.log-row');
    const i = targetIndex();
    if (i < 0 || !rows[i]) return;
    toggleRow(rows[i], true);
    try { rows[i].scrollIntoView({ block: 'center' }); } catch (e) { rows[i].scrollIntoView(); }
  }

  /* ---------------- 装载 ---------------- */
  async function load(payload) {
    const my = ++seq;
    if (payload) S.payload = payload;
    const name = await resolveBook(S.payload);
    if (my !== seq) return;                         // 期间又发起了新的装载
    S.book = name;
    $('lg-book').textContent = name || '未识别书稿';
    renderBanner();
    if (!name) {
      S.recs = [];
      renderEmpty('没有可显示的书稿', '还没有编译记录：先在 PDF 卡里打开一篇论文并点「编译」。');
      return;
    }
    let records = [];
    try {
      const r = await api('api/history?book=' + encodeURIComponent(name));
      records = (r && r.records) || [];
    } catch (e) {
      if (my !== seq) return;
      renderEmpty('读取编译记录失败', e.message || String(e));
      return;
    }
    if (my !== seq) return;
    S.recs = records.slice().sort((a, b) => (b.ts || 0) - (a.ts || 0));
    S.loaded = true;
    render();
    autoExpand();
  }

  function init() {
    // 宿主在 iframe load 后下发的一次性上下文（从 PDF 卡切过来时带 {book,path,error,ts}）
    window.addEventListener('message', e => {
      if (!EMBEDDED || e.source !== window.parent) return;
      const d = e.data;
      if (!d || typeof d !== 'object' || d.type !== 'floria-wk-card-init') return;
      load(d.payload || null);
    });

    $('lg-refresh').addEventListener('click', () => load(null));

    load(null);   // 打开即拉历史（无 payload → 解析上次打开的书稿）
  }

  init();
})();
