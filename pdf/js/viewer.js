/* Floria LaTeX v2 — PDF 卡主控
   职责（2026-10-26 拆分后）：打开/关闭书稿、载入 PDF、宿主桥命令、缩放/翻页/工具面板外壳。
   编译 → js/compile.js；目录抽屉 → js/toc.js；共享态 → js/state.js。
   多卡片架构：编译失败时向宿主发 floria-wk-card-switch 切到「编译日志」卡；
   单开（无 parent）时只 toast 报错——本页不再维护日志面板（日志/历史只由 log/ 卡一张负责）。 */
(function () {
  'use strict';
  const F = window.Floria;
  const $ = F.$;
  const T = F.toolbar;
  const S = F.state;

  /* ---------------- 工具面板卡（注册表消费方） ---------------- */
  function markTabs(id) {
    document.querySelectorAll('#tool-tabs [data-tool]').forEach(b => b.classList.toggle('on', b.dataset.tool === id));
  }
  // 工具面板开合的唯一开关。面板占满整块视图区（见 css #side-card），故只需显隐。
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
    // pane 显隐按去重后的 pane id 收敛：先全隐，再只显示当前工具的 pane。
    const panes = new Set();
    T.toolDefs().forEach(d => { if (d.pane) panes.add(d.pane); });
    panes.forEach(p => { const el = $(p); if (el) el.classList.add('hidden'); });
    const def = T.toolDef(nid);
    if (def && def.pane) { const el = $(def.pane); if (el) el.classList.remove('hidden'); }
    $('sc-title').textContent = def ? def.title : '';
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

  /* ---------------- 空态 ---------------- */
  function showEmpty(on) {
    const el = $('view-empty');
    if (el) el.classList.toggle('hidden', !on);
  }
  function setHint(t) { const h = $('ve-hint'); if (h && t) h.textContent = t; }

  /* ---------------- 单开预载 ----------------
     未桥接（file:// / 直起 server）时没有宿主来「打开某篇」，启动即挑第一篇**有 PDF 的**书稿顶上。
     桥接（iframe）时不抢跑，等宿主的 open 命令 / floria-pv-file-open 广播。 */
  async function preload() {
    if (F.bridge.embedded()) return;
    if (S.book) return;                 // hash 路由已选定书稿：别再挑一篇把它顶掉
    try {
      const books = await F.api('api/books');
      const b = (books || []).find(x => x.pdf && x.pdf.exists) || (books || [])[0];
      if (b && b.name) await open(b.name);
    } catch (e) { /* 服务器不可达：保持空态 */ }
  }

  /* ---------------- PDF 载入 ---------------- */
  // soft=true：打开书稿时的「只读本地 PDF」路径——载不到不算编译失败，只回落成未编译态
  // keep=true：重载（编译后 / 外部编译轮询命中）——载完停在重载前那一页
  function loadPdf(soft, keep) {
    if (!window.PdfView) return Promise.resolve();
    const v = S.pdfMtime || Date.now();
    // API 基准在 <base>/api/（卡片在 <base>/<cardId>/ 下），不能直接用相对地址
    return PdfView.load(F.apiUrl('api/pdf?book=' + encodeURIComponent(S.book) + '&v=' + v), (cur, total) => {
      $('pdf-page-ind').textContent = cur + ' / ' + total;
      F.toc.mark();                // 目录抽屉开着时，高亮跟着翻页走
      emitState();                 // 翻页 = 状态变化（emitState 自带去重）
    }, onPick, keep).then(() => {
      F.compile.setState('ok');
      if (F.toc.isOpen()) F.toc.render();  // 换书/重编后书签要重读
    }).catch(err => {
      if (soft) {
        F.compile.setState('none');
        F.toast('这篇还没有编译好的 PDF（main.pdf）——点「编译」生成');
      } else {
        F.compile.setState('err');
        F.toast('PDF 载入失败：' + (err && err.message || err));
      }
    });
  }

  /* ---------------- 卡片 API（宿主桥） ---------------- */
  // 状态快照（唯一读处）：页码/页数读翻页指示器，编译态读 S.state，缩放读 PdfView。
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
  // 事件面去重：翻页滚动会连着触发 onPage，不去重会把宿主刷屏。
  let lastEmit = '';
  function emitState() {
    const s = JSON.stringify(snapshot());
    if (s === lastEmit) return;
    lastEmit = s;
    F.bridge.emit('state', JSON.parse(s));
  }

  // 项目根相对 POSIX 路径 → 书稿名（= 目录，含多级，如 'PHYSIC/ch1'）。
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
    const onFileOpen = d => {
      const book = bookFromPath(d && (d.path || d.book));
      if (book) openBook(book);
    };
    B.onNote('floria-pv-broadcast', d => { if (d.ev === 'file-open') onFileOpen(d); });
    B.onNote('floria-pv-event', d => { if (d.ev === 'file-open') onFileOpen(d); });
    // 多卡片切换：宿主在 iframe load 后下发 floria-wk-card-init{card,payload}。
    B.onNote('floria-wk-card-init', d => {
      const p = (d && d.payload) || {};
      const name = String(p.book || '') || bookFromPath(p.path || '');
      if (name) { openBook(name); return; }
      let last = '';
      try { last = localStorage.getItem('florialatex.lastbook') || ''; } catch (e) { /* 隐私模式：忽略 */ }
      if (last) openBook(last);
    });
    B.onCmd('page', d => {                       // {page} 在当前书稿内翻页（1 基）
      const n = parseInt(d.page, 10);
      if (!window.PdfView || !Number.isFinite(n)) throw new Error('page 参数非法');
      PdfView.goTo(n);
      return n;
    });
    B.onCmd('compile', () => F.compile.compile());
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
    B.onCmd('state', () => snapshot());          // 探活/取态
    B.emit('ready', {});
    emitState();
  }

  /* ---------------- 点 PDF → 跳源码 ---------------- */
  async function onPick(page, x, y) {
    if (!S.book) return;
    try {
      const d = await F.api('api/sync?book=' + encodeURIComponent(S.book) +
        '&page=' + page + '&x=' + x.toFixed(1) + '&y=' + y.toFixed(1));
      if (!d.ok) { F.toast(d.error || '没查到对应源码行'); return; }
      if (window.PdfView) PdfView.markAt(page, x, y);
      const tex = d.texName || 'main.tex';
      F.bridge.emit('src-open', { path: S.book + '/' + tex, line: d.line });
      F.toast('→ ' + tex + ' 第 ' + d.line + ' 行');
    } catch (e) {
      F.toast('查询失败：' + (e.message || e));
    }
  }

  /* ---------------- 打开 / 关闭书稿 ---------------- */
  async function open(name) {
    name = String(name || '');
    if (!name) { close(); return; }                 // 空名 = 回到空态
    if (S.book === name) return;                     // 已是当前篇：幂等
    S.book = name;
    S.texName = '';
    try { localStorage.setItem('florialatex.lastbook', name); } catch (e) { /* 隐私模式：忽略 */ }
    S.pdfMtime = 0;
    F.compile.setState('none');
    applyToolTab('');
    showEmpty(false);
    // 本页是「本地 PDF 显示器」：打开 = 直接读 main.pdf，**从不自动编译**。
    let info = null;
    try {
      const books = await F.api('api/books');
      const b0 = books.find(b => b.name === name) || {};
      info = b0.pdf || null;
      if (b0.texName) S.texName = b0.texName;
    } catch (e) { /* 状态取不到：只当没 PDF */ }
    if (info && info.exists) {
      S.pdfMtime = info.mtime || 0;         // 轮询据此判「外部编译产出新 PDF」
      await loadPdf(true);
      if (!info.fresh) F.compile.setState('stale');   // 源码比 PDF 新 → 「重新编译」
    } else {
      S.pdfMtime = 0;
      F.compile.setState('none');
      if (info) F.toast('这篇还没有编译好的 PDF（main.pdf）——点「编译」生成');
    }
    startWatch();
  }

  /* ---------------- 外部编译感知（A1） ----------------
     会话里 AI 编译（不经前端）或手工编译 → 轮询 /api/books 比对 pdf.mtime，变了就重载。 */
  const POLL_MS = 4000;
  let watcher = null;
  async function pollOnce() {
    if (!S.book || S.busy) return;
    let info = null;
    try {
      const books = await F.api('api/books');
      info = (books.find(b => b.name === S.book) || {}).pdf || null;
    } catch (e) { return; }
    if (!info || !info.exists) return;
    if (info.mtime && info.mtime !== S.pdfMtime) {
      S.pdfMtime = info.mtime;
      await loadPdf(false, true);           // 外部编译产出新 PDF → 换新，停在当前页
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
    S.texName = '';
    S.busy = false;
    if (window.PdfView) PdfView.destroy();
    $('pdf-pages').innerHTML = '';
    $('pdf-page-ind').textContent = '– / –';
    applyToolTab('');
    F.toc.act(false);
    showEmpty(true);
  }

  /* ---------------- 缩放 ---------------- */
  function markZoom() {
    const z = window.PdfView ? PdfView.getZoom() : null;
    document.querySelectorAll('#zoom-panel .zp-row button[data-zoom]').forEach(b => {
      const v = b.dataset.zoom;
      b.classList.toggle('on', (v === 'fit' && z === 'fit') || (v === 'fitpage' && z === 'fitpage') ||
        (z !== 'fit' && z !== 'fitpage' && Math.abs(parseFloat(v) - z) < 0.001));
    });
  }

  function init() {
    setHint(F.bridge.embedded()
      ? '在左侧文件树里点开任意一篇 .tex，这里会显示它的 PDF。'
      : '打开项目里的任意一篇 .tex（或直接起一篇已编译的书稿），这里会显示它的 PDF。');
    showEmpty(true);

    $('tool-tabs').addEventListener('click', e => {
      const b = e.target.closest('[data-tool]');
      if (b) applyToolTab(b.dataset.tool);
    });
    syncScTitle();
    window.addEventListener('floria:tool-select', e => applyToolTab((e.detail && e.detail.id) || ''));
    $('sc-close').addEventListener('click', () => applyToolTab(''));
    document.addEventListener('keydown', e => {
      if (e.key !== 'Escape') return;
      if (F.toc.isOpen()) { F.toc.act(false); return; }   // 目录抽屉在最上层，先收它
      if (!$('side-card').classList.contains('hidden')) applyToolTab('');
    });

    // 目录抽屉：入口 + 卡头 ✕ + 点条目跳页
    $('toc-toggle').addEventListener('click', () => F.toc.act(!F.toc.isOpen()));
    $('toc-close').addEventListener('click', () => F.toc.act(false));
    $('toc-body').addEventListener('click', e => {
      const b = e.target.closest('.toc-item');
      const n = b && +b.dataset.page;
      if (!b || !n || !window.PdfView) return;
      PdfView.goTo(n);
      F.toc.mark();
    });

    $('v-compile').addEventListener('click', F.compile.compile);

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

    bindBridge();   // 卡片 API：登记命令处理 + 向宿主报「就绪」
  }

  F.viewer = { init, open, close, loadPdf, preload, applyToolTab, emitState, S };
})();
