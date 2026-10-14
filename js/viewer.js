/* Floria LaTeX v2 — 编辑页：满屏 PDF（自绘查看器 = js/pdfview.js，原样复用）+ 编译 + 缩放 + 翻页
   + 工具面板。顶栏只有三枚按钮：返回上级 / 编译 / 界面调整（编译态不再单列胶囊，折在编译按钮的文案里）。
   工具 tab 不写死：本文件只往 js/toolbar.js 的注册表里登记两枚
   （'ver' 版本快照 / 'log' 原始日志，共用同一份编译历史 = GET /api/history 的落盘账本），tab 条由注册表渲染。 */
(function () {
  'use strict';
  const F = window.Floria;
  const $ = F.$, esc = F.esc;
  const T = F.toolbar;

  const S = {
    book: '',            // 当前书稿名（= /api/books 里的 name）
    pdfMtime: 0,
    busy: false,
    recs: [],            // 编译历史（日志/版本共用）：{ts, ok, elapsed, pages, by, error, log}，新→旧
    tool: '',            // 当前展开的工具 id
    state: 'none',       // 编译五态
  };

  /* ---------------- 编译五态 = 编译按钮自己的文案（不再另立胶囊） ---------------- */
  const ST = { none: '编译', busy: '编译中…', ok: '编译', stale: '重新编译', err: '重试编译' };
  function setState(st) {
    S.state = st;
    const b = $('v-compile');
    if (!b) return;
    b.textContent = ST[st] || ST.none;
    b.className = 'vt-btn primary st-' + st;
    b.disabled = st === 'busy';
  }

  /* ---------------- 工具面板卡（注册表消费方） ---------------- */
  function markTabs(id) {
    document.querySelectorAll('#tool-tabs [data-tool]').forEach(b => b.classList.toggle('on', b.dataset.tool === id));
  }
  // 工具面板开合的唯一开关。面板占满整块视图区（见 css #side-card），故只需显隐——
  // 不再有「卡压在 PDF 上、卡的区域还露着 PDF」这回事。
  function setCardOpen(on) {
    $('side-card').classList.toggle('hidden', !on);
  }
  // 唯一落点：切 pane 显隐 + tab active 态 + 开关面板卡。force=true 时不吃「点同一枚收起」。
  function applyToolTab(id, force) {
    const card = $('side-card');
    if (!id) { setCardOpen(false); S.tool = ''; markTabs(''); return; }   // 空 = 收起
    const nid = T.toolNormId(id);
    if (!force && nid && nid === S.tool && !card.classList.contains('hidden')) {
      setCardOpen(false); S.tool = ''; markTabs(''); return;
    }
    S.tool = nid;
    S.lastTool = nid;           // 收起后记住：再点「工具」仍开回这一枚
    if (!nid) { setCardOpen(false); markTabs(''); return; }
    T.mountTool(nid);
    // pane 显隐按**去重后的 pane id** 收敛：先全隐，再只显示当前工具的 pane。
    // 不能按 d.id !== nid 逐条 toggle —— 两枚工具共用同一 pane 时，后一条会把前一条刚显示的又隐回去。
    const panes = new Set();
    T.toolDefs().forEach(d => { if (d.pane) panes.add(d.pane); });
    panes.forEach(p => { const el = $(p); if (el) el.classList.add('hidden'); });
    const def = T.toolDef(nid);
    if (def && def.pane) { const el = $(def.pane); if (el) el.classList.remove('hidden'); }
    $('sc-title').textContent = def ? def.title : '';
    // 卡头 tab 条（注册表渲染）在场时不必再重复一枚文字标题；被宿主接管（tab 条空）时用它兜底
    syncScTitle();
    setCardOpen(true);
    markTabs(nid);
  }
  function syncScTitle() {
    const tabs = $('tool-tabs');
    const hasTabs = !!(tabs && tabs.children.length);
    const t = $('sc-title');
    if (t) t.classList.toggle('hidden', hasTabs);
  }

  /* ---------------- 编译历史（日志 / 版本两枚工具的统一数据源） ----------------
     服务端账本 = <书稿>/.build/compile-history.jsonl（GET /api/history），三个写入方同格式：
     本页点「编译」(by=page)、AI 会话跑 compile_book.py (by=ai)、功能上线前的磁盘回溯 (by=disk)
     ⇒ 天然合流、落盘可刷新。内存里另存的只有「PDF 载入失败」这类不成编译的本地提示。 */
  function fmtClock(ts) {
    const d = ts ? new Date(ts * 1000) : new Date();
    const two = n => (n < 10 ? '0' : '') + n;
    return two(d.getHours()) + ':' + two(d.getMinutes()) + ':' + two(d.getSeconds());
  }
  function fmtStamp(ts) {
    const d = ts ? new Date(ts * 1000) : new Date();
    const two = n => (n < 10 ? '0' : '') + n;
    return two(d.getMonth() + 1) + '-' + two(d.getDate()) + ' ' + fmtClock(ts);
  }
  const BY_TAG = { page: '页', ai: 'AI', disk: '盘' };
  function listEl() { return $('tool-log'); }
  async function loadHistory() {
    if (!S.book) { S.recs = []; renderPanes(); return; }
    let records = null;
    try {
      const r = await F.api('api/history?book=' + encodeURIComponent(S.book));
      records = (r && r.records) || [];
    } catch (e) { return; }              // 服务器不可达：保留现有记录
    S.recs = records.slice().sort((a, b) => (b.ts || 0) - (a.ts || 0));   // 新 → 旧
    renderPanes();
  }
  function renderPanes() {
    const l = listEl();
    if (l && l.dataset.mounted) renderLogPane();
    const v = $('tool-ver');
    if (v && v.dataset.mounted) renderVerPane();
  }
  function renderLogPane() {
    const el = listEl();
    if (!el) return;
    const sec = n => '<div class="pane-sec"><span class="pane-sec-title">原始日志</span>' +
      '<span class="pane-sec-note">' + n + '</span></div>';
    if (!S.recs.length) {
      el.innerHTML = sec(0) + '<div class="pane-empty"><p class="pe-title">还没有编译记录</p>' +
        '<p class="pe-hint">点上方「编译」后，每次编译的结果与报错都会记在这里。</p></div>';
      return;
    }
    el.innerHTML = sec(S.recs.length) + S.recs.map(e => `
      <div class="log-row${e.ok ? '' : ' bad'}">
        <div class="log-head">
          <span class="log-mark">${e.ok ? '✓' : '✕'}</span>
          <span class="log-time">${esc(fmtClock(e.ts))}</span>
          <span class="log-el">${e.elapsed != null ? e.elapsed + 's' : '—'}</span>
          ${BY_TAG[e.by] ? '<span class="log-by">' + BY_TAG[e.by] + '</span>' : ''}
          <span class="log-brief">${esc(e.error || '编译通过')}</span>
        </div>
        <pre class="log-body hidden">${esc(e.log || '(无输出)')}</pre>
      </div>`).join('');
    el.querySelectorAll('.log-row').forEach(r => r.addEventListener('click', () => {
      const b = r.querySelector('.log-body');
      if (b) b.classList.toggle('hidden');
    }));
  }
  function renderVerPane() {
    const el = $('tool-ver');
    if (!el) return;
    const sec = n => '<div class="pane-sec"><span class="pane-sec-title">版本快照</span>' +
      '<span class="pane-sec-note">' + n + '</span></div>';
    if (!S.recs.length) {
      el.innerHTML = sec(0) + '<div class="pane-empty"><p class="pe-title">还没有版本记录</p>' +
        '<p class="pe-hint">每次编译（本页点「编译」或 AI 会话里编译）都会在这里留一条：时间 / 页数 / 耗时 / 成败。</p></div>';
      return;
    }
    el.innerHTML = sec(S.recs.length) + S.recs.map(e => `
      <div class="log-row ver-row${e.ok ? '' : ' bad'}">
        <div class="log-head">
          <span class="log-mark">${e.ok ? '✓' : '✕'}</span>
          <span class="log-time">${esc(fmtStamp(e.ts))}</span>
          ${BY_TAG[e.by] ? '<span class="log-by">' + BY_TAG[e.by] + '</span>' : ''}
          <span class="log-brief">${esc(e.error || '编译通过')}</span>
        </div>
        <div class="ver-meta"><span>页数 ${e.pages != null ? e.pages : '—'}</span><span>耗时 ${e.elapsed != null ? e.elapsed + 's' : '—'}</span></div>
      </div>`).join('');
  }
  function pushLocal(e) {              // 本地-only 记录（不成一次真编译，如 PDF 载入失败）
    S.recs.unshift({ ts: Date.now() / 1000, ok: !!e.ok, elapsed: e.elapsed != null ? e.elapsed : null,
      pages: null, by: 'page', error: e.error || '', log: e.log || '' });
    renderPanes();
  }

  /* ---------------- 目录抽屉（PDF 自带书签 → 跳页） ----------------
     书签树由 pdf.js 读（PdfView.getOutline），本文件只管画与跳。 */
  const tocOpen_ = () => !$('toc-drawer').classList.contains('hidden');
  function tocAct(on) {
    $('toc-drawer').classList.toggle('hidden', !on);
    $('toc-toggle').classList.toggle('on', on);   // 入口在顶栏，常驻；开着时只是变「按下」态
    if (on) renderToc();
  }
  function renderToc() {
    const body = $('toc-body');
    if (!body) return;
    const empty = (t, h) => '<div class="pane-empty"><p class="pe-title">' + esc(t) + '</p>' +
      '<p class="pe-hint">' + esc(h) + '</p></div>';
    if (!window.PdfView || !S.book) {
      body.innerHTML = empty('还没有打开论文', '回目录页选一篇，再来这里按章节跳转。');
      return;
    }
    body.innerHTML = '<div class="pane-empty"><p class="pe-hint">读取书签…</p></div>';
    PdfView.getOutline().then(list => {
      if (!tocOpen_()) return;                        // 期间被关掉就别再画
      if (!list.length) {
        body.innerHTML = empty('这篇 PDF 没有书签',
          '编译时用了 \\usepackage{hyperref} 才有章节书签；也可以在 PDF 的目录页直接点链接跳转。');
        return;
      }
      body.innerHTML = '';
      const add = (items, depth) => items.forEach(it => {
        const b = document.createElement('button');
        b.className = 'toc-item lv' + Math.min(depth + 1, 4);
        b.innerHTML = '<span class="toc-t">' + esc(it.title) + '</span>' +
          (it.page ? '<span class="toc-pg">' + it.page + '</span>' : '');
        if (it.page) b.dataset.page = it.page; else { b.disabled = true; b.classList.add('off'); }
        body.appendChild(b);
        if (it.children && it.children.length) add(it.children, depth + 1);
      });
      add(list, 0);
      markToc();
    }).catch(() => {
      if (tocOpen_()) body.innerHTML = empty('书签读取失败', '可以直接在 PDF 的目录页点链接跳转。');
    });
  }
  // 高亮「当前章节」= 起始页不超过当前页的最后一枚（滚动时跟着走）
  function markToc() {
    const rows = document.querySelectorAll('#toc-body .toc-item');
    if (!rows.length) return;
    const cur = parseInt(($('pdf-page-ind').textContent || '').split('/')[0], 10);
    if (!Number.isFinite(cur)) return;
    let best = null;
    rows.forEach(r => {
      r.classList.remove('on');
      const p = +r.dataset.page;
      if (p && p <= cur && (!best || p >= +best.dataset.page)) best = r;
    });
    if (best) best.classList.add('on');
  }

  /* ---------------- 空态（还没有任何书稿被打开时铺在视图区） ----------------
     #view-empty 与 #pdf-pages 同层，DOM 在前 ⇒ PDF 页一渲出来就把它盖住，无需按态显隐。 */
  function showEmpty(on) {
    const el = $('view-empty');
    if (el) el.classList.toggle('hidden', !on);
  }
  function setHint(t) { const h = $('ve-hint'); if (h && t) h.textContent = t; }

  /* ---------------- 单开预载 ----------------
     未桥接（file:// / 直起 server）时没有宿主来「打开某篇」，启动即挑第一篇**有 PDF 的**书稿顶上，
     免得整块视图空着。桥接（iframe）时不抢跑，等宿主的 open 命令 / floria-pv-file-open 广播。 */
  async function preload() {
    if (F.bridge.embedded()) return;
    try {
      const books = await F.api('api/books');
      const b = (books || []).find(x => x.pdf && x.pdf.exists) || (books || [])[0];
      if (b && b.name) await open(b.name);
    } catch (e) { /* 服务器不可达：保持空态（提示文案已说明） */ }
  }

  /* ---------------- PDF 载入 / 编译 ---------------- */
  // soft=true：打开书稿时的「只读本地 PDF」路径——载不到不算编译失败，只回落成未编译态
  // keep=true：重载（编译后 / 外部编译轮询命中）——载完停在重载前那一页（见 pdfview.js load 的 keep）
  function loadPdf(soft, keep) {
    if (!window.PdfView) return Promise.resolve();
    const v = S.pdfMtime || Date.now();
    return PdfView.load('api/pdf?book=' + encodeURIComponent(S.book) + '&v=' + v, (cur, total) => {
      $('pdf-page-ind').textContent = cur + ' / ' + total;
      markToc();                   // 目录抽屉开着时，高亮跟着翻页走
      emitState();                 // 翻页 = 状态变化（emitState 自带去重）
    }, onPick, keep).then(() => {
      setState('ok');
      if (tocOpen_()) renderToc();  // 换书/重编后书签要重读
    }).catch(err => {
      if (soft) {
        setState('none');
        F.toast('这篇还没有编译好的 PDF（main.pdf）——点「编译」生成');
      } else {
        setState('err');
        pushLocal({ ok: false, error: 'PDF 载入失败：' + (err && err.message || err), log: '' });
      }
    });
  }

  async function compile() {
    if (S.busy || !S.book) return;
    S.busy = true;
    setState('busy');
    let r;
    try {
      r = await F.post('api/compile', { book: S.book });
    } catch (e) {
      r = { ok: false, error: e.message || String(e), log: '' };
    }
    S.busy = false;
    const elapsed = r.time != null ? r.time : 0;
    loadHistory();                        // 服务端 run_compile 已把这条追加进账本 → 拉回来即合流
    if (r.ok) {
      S.pdfMtime = r.mtime || Date.now();
      setState('ok');
      F.toast('编译完成（' + elapsed + 's）');
      await loadPdf(false, true);            // 保持当前页码/缩放
    } else {
      setState('err');
      applyToolTab('log', true);   // 编译失败 → 自动摊开日志卡（force：已在日志上也要保留）
      F.toast('编译失败：' + (r.error || '见日志'));
    }
    emitState();
  }

  /* ---------------- 卡片 API（宿主桥，见 js/bridge.js） ----------------
     本页没有编辑器，「跳转」只能出帧：查到源码位置后既弹卡内 toast，又发一条 src-open 请宿主的编辑卡
     打开该文件并滚到该行（宿主侧 openWorkFile + 滚动到行）。 */
  // 状态快照（唯一读处）：页码/页数读翻页指示器（它是页码的唯一读数，勿另算），编译态读 S.state，缩放读 PdfView。
  function snapshot() {
    const ind = ($('pdf-page-ind').textContent || '').split('/');
    const page = parseInt(ind[0], 10), pages = parseInt(ind[1], 10);
    return {
      book: S.book || '',
      page: Number.isFinite(page) ? page : 0,
      pages: Number.isFinite(pages) ? pages : 0,
      state: S.state,
      busy: S.busy,
      zoom: window.PdfView ? PdfView.getZoom() : null,
    };
  }
  // 事件面去重（同 toolbar.js 的 lastPosted 教训）：翻页滚动会连着触发 onPage，不去重会把宿主刷屏。
  let lastEmit = '';
  function emitState() {
    const s = JSON.stringify(snapshot());
    if (s === lastEmit) return;
    lastEmit = s;
    F.bridge.emit('state', JSON.parse(s));
  }

  // 项目根相对 POSIX 路径 → 书稿名（= 目录，含多级，如 'PHYSIC/ch1'）。
  // 口径与 server 一致：一目录 = 一篇，书稿名 = 该目录的根相对 POSIX 路径。
  function bookFromPath(p) {
    p = String(p || '').replace(/\\/g, '/').replace(/^\/+/, '');
    return p.indexOf('/') < 0 ? '' : p.slice(0, p.lastIndexOf('/'));
  }
  function openBook(name) {                       // 同步壳：立刻 ack，不等 PDF 载完
    name = String(name || '');
    if (!name) throw new Error('缺少书稿名');
    open(name).then(emitState, () => {});
    return name;
  }

  function bindBridge() {
    const B = F.bridge;
    B.onCmd('open', d => {                       // {book, page?} 打开书稿（可顺带跳到某页）
      const name = String(d.book || '');
      if (!name) throw new Error('open 缺少 book');
      return open(name).then(() => {
        const n = parseInt(d.page, 10);
        if (Number.isFinite(n) && window.PdfView) PdfView.goTo(n);
        emitState();
        return name;
      });
    });
    // 广播/通知面：宿主「用户打开了某文件」→ 本卡据此切换预览。
    //   {type:'floria-pv-broadcast', ev:'file-open', path:'MATH/main.tex'}
    //   path = 项目根相对 POSIX 路径；书稿名由目录段推出（与 /gateway/file 同口径）。
    const onFileOpen = d => {
      const book = bookFromPath(d && (d.path || d.book));
      if (book) openBook(book);
    };
    B.onNote('floria-pv-broadcast', d => { if (d.ev === 'file-open') onFileOpen(d); });
    B.onNote('floria-pv-event', d => { if (d.ev === 'file-open') onFileOpen(d); });
    B.onCmd('page', d => {                       // {page} 在当前书稿内翻页（1 基）
      const n = parseInt(d.page, 10);
      if (!window.PdfView || !Number.isFinite(n)) throw new Error('page 参数非法');
      PdfView.goTo(n);
      return n;
    });
    B.onCmd('compile', () => compile());
    B.onCmd('zoom', d => {                       // {mode: 'fit' | 'fitpage' | 数字倍率}
      if (!window.PdfView) throw new Error('查看器未就绪');
      const m = d.mode;
      if (m === 'fit') PdfView.zoomFit();
      else if (m === 'fitpage') PdfView.zoomFitPage();
      else PdfView.zoomSet(m);
      markZoom();
      emitState();
      return m;
    });
    B.onCmd('state', () => snapshot());          // 探活/取态：ack 里带 result，另发一条 state 事件
    B.emit('ready', {});                         // 帧就绪：宿主据此下发命令（本页不缓存命令，重发由宿主负责）
    emitState();
  }

  /* ---------------- 点 PDF → 跳源码（卡内提示 + 请宿主跳） ---------------- */
  async function onPick(page, x, y) {
    if (!S.book) return;
    try {
      const d = await F.api('api/sync?book=' + encodeURIComponent(S.book) +
        '&page=' + page + '&x=' + x.toFixed(1) + '&y=' + y.toFixed(1));
      if (!d.ok) { F.toast(d.error || '没查到对应源码行'); return; }
      if (window.PdfView) PdfView.markAt(page, x, y);
      const tex = d.texName || 'main.tex';
      // path = 项目根相对 POSIX 路径（宿主 /gateway/file?label=&path= 同口径）：书稿名本身即根相对路径。
      // 查不到源文件位置（d.ok=false）时上面已 return，故此处必有 line。
      F.bridge.emit('src-open', { path: S.book + '/' + tex, line: d.line });
      F.toast('→ ' + tex + ' 第 ' + d.line + ' 行');
    } catch (e) {
      F.toast('查询失败：' + (e.message || e));
    }
  }

  /* ---------------- 打开 / 关闭书稿 ---------------- */
  async function open(name) {
    name = String(name || '');
    if (!name) { close(); return; }                 // 空名 = 回到空态（无书稿打开）
    if (S.book === name) return;                     // 已是当前篇：幂等（宿主重复广播不重载）
    S.book = name;
    S.pdfMtime = 0;
    setState('none');
    applyToolTab('');
    S.recs = [];
    loadHistory();                        // 拉这本书的编译账本（日志/版本两枚工具的数据源）
    showEmpty(false);
    // 本页是「本地 PDF 显示器」：打开 = 直接读书稿根的 main.pdf，**从不自动编译**。
    // 先问 /api/books 要该书的 pdf 状态：有才让 pdf.js 去取（**没 PDF 就不去取**——直接让 pdf.js
    // 撞 404 会在它内部留一个没人接的 promise 拒绝，控制台报 Uncaught）。
    let info = null;
    try {
      const books = await F.api('api/books');
      info = (books.find(b => b.name === name) || {}).pdf || null;
    } catch (e) { /* 状态取不到：只当没 PDF，不弹提示（目录页已负责服务器不可达的文案） */ }
    if (info && info.exists) {
      S.pdfMtime = info.mtime || 0;         // 记下当前 PDF mtime：轮询据此判「外部编译产出新 PDF」
      await loadPdf(true);
      if (!info.fresh) setState('stale');   // 源码比 PDF 新 → 编译按钮改口「重新编译」
    } else {
      S.pdfMtime = 0;
      setState('none');
      if (info) F.toast('这篇还没有编译好的 PDF（main.pdf）——点「编译」生成');
    }
    startWatch();                           // 书稿已打开 → 起轮询（A1：会话里 AI 编译也能自动换新）
  }

  /* ---------------- 外部编译感知（A1） ----------------
     用户在会话里让 AI 跑 compile_book.py（不经前端），卡片无感知、PDF 停旧版。
     轻量轮询 /api/books 比对本书 pdf.mtime：变了就重载（保持页码/缩放），不引入长连接。
     仅在已打开书稿时轮询；本页自己编译时 S.busy=true，跳过（其 mtime 由 compile 自己回填）。 */
  const POLL_MS = 4000;
  let watcher = null;
  async function pollOnce() {
    if (!S.book || S.busy) return;
    let info = null;
    try {
      const books = await F.api('api/books');
      info = (books.find(b => b.name === S.book) || {}).pdf || null;
    } catch (e) { return; }                 // 服务器不可达：本轮跳过，下轮再试
    if (!info || !info.exists) return;
    if (info.mtime && info.mtime !== S.pdfMtime) {
      S.pdfMtime = info.mtime;
      await loadPdf(false, true);           // 外部编译产出新 PDF → 换新，停在当前页
      loadHistory();                        // 外部编译（AI 会话/手工）的记录也同步进日志/版本
      F.toast('检测到新编译的 PDF，已刷新');
      emitState();
    }
  }
  function startWatch() {
    stopWatch();
    if (!S.book) return;
    watcher = setInterval(pollOnce, POLL_MS);
  }
  function stopWatch() { if (watcher) { clearInterval(watcher); watcher = null; } }

  function close() {
    stopWatch();
    S.book = '';
    S.busy = false;
    S.recs = [];
    if (window.PdfView) PdfView.destroy();
    $('pdf-pages').innerHTML = '';
    $('pdf-page-ind').textContent = '– / –';
    applyToolTab('');
    tocAct(false);
    showEmpty(true);                 // 无书稿 = 空态
  }

  /* ---------------- 缩放 / 翻页 / 下载绑定 ---------------- */
  function markZoom() {
    const z = window.PdfView ? PdfView.getZoom() : null;
    document.querySelectorAll('#zoom-panel .zp-row button[data-zoom]').forEach(b => {
      const v = b.dataset.zoom;
      b.classList.toggle('on', (v === 'fit' && z === 'fit') || (v === 'fitpage' && z === 'fitpage') ||
        (z !== 'fit' && z !== 'fitpage' && Math.abs(parseFloat(v) - z) < 0.001));
    });
  }

  function init() {
    // 空态文案随场景变：桥接时指向宿主文件树，单开时指向本机 .tex
    setHint(F.bridge.embedded()
      ? '在左侧文件树里点开任意一篇 .tex，这里会显示它的 PDF。'
      : '打开项目里的任意一篇 .tex（或直接起一篇已编译的书稿），这里会显示它的 PDF。');
    showEmpty(true);

    /* 注册两枚工具（本页工具栏的**全部** tab 来源，顺序 = 注册顺序） */
    T.registerTool({ id: 'ver', title: '版本', pane: 'tool-ver', mount: renderVerPane });
    T.registerTool({ id: 'log', title: '日志', pane: 'tool-log', mount: renderLogPane });

    $('tool-tabs').addEventListener('click', e => {
      const b = e.target.closest('[data-tool]');
      if (b) applyToolTab(b.dataset.tool);
    });
    syncScTitle();
    // 宿主工具栏（Floria 「工具栏」面板）选了某枚工具 → 本页切 pane（tab 由宿主渲，不在页内）
    window.addEventListener('floria:tool-select', e => applyToolTab((e.detail && e.detail.id) || ''));
    $('sc-close').addEventListener('click', () => applyToolTab(''));
    document.addEventListener('keydown', e => {
      if (e.key !== 'Escape') return;
      if (tocOpen_()) { tocAct(false); return; }        // 目录抽屉在最上层，先收它
      if (!$('side-card').classList.contains('hidden')) applyToolTab('');
    });

    // 目录抽屉：入口（顶栏图标）+ 卡头 ✕ + 点条目跳页（入口再点一次收起）
    $('toc-toggle').addEventListener('click', () => tocAct(!tocOpen_()));
    $('toc-close').addEventListener('click', () => tocAct(false));
    $('toc-body').addEventListener('click', e => {
      const b = e.target.closest('.toc-item');
      const n = b && +b.dataset.page;
      if (!b || !n || !window.PdfView) return;
      PdfView.goTo(n);
      markToc();
    });

    $('v-compile').addEventListener('click', compile);

    $('pdf-prev').addEventListener('click', () => { if (window.PdfView) PdfView.prev(); });
    $('pdf-next').addEventListener('click', () => { if (window.PdfView) PdfView.next(); });

    // 界面调整（缩放）面板
    $('pdf-zoom-toggle').addEventListener('click', e => {
      e.stopPropagation();
      $('zoom-panel').classList.toggle('hidden');
      markZoom();
    });
    document.addEventListener('click', e => {
      if (!e.target.closest('#zoom-panel') && !e.target.closest('#pdf-zoom-toggle')) {
        $('zoom-panel').classList.add('hidden');
      }
    });
    document.querySelectorAll('#zoom-panel .zp-row button').forEach(b => {
      b.addEventListener('click', () => {
        if (!window.PdfView) return;
        const v = b.dataset.zoom;
        if (v === 'fit') PdfView.zoomFit();
        else if (v === 'fitpage') PdfView.zoomFitPage();
        else PdfView.zoomSet(v);
        markZoom();
      });
    });
    $('pdf-zoom-range').addEventListener('input', e => {
      if (window.PdfView) PdfView.zoomSet(+e.target.value / 100);
    });
    $('pdf-zoom-range').addEventListener('change', markZoom);

    // 工具面板卡里的滚动/点击不该被「点 PDF」拾取（picker 只挂 #pdf-pages，天然隔离）

    bindBridge();   // 卡片 API：登记命令处理 + 向宿主报「就绪」
  }

  F.viewer = { init, open, close, compile, preload, applyToolTab, loadHistory, S };
})();
