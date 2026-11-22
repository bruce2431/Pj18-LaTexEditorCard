/* ============================================================
 * pdfview.js — 自绘 PDF 查看器（pdf.js 渲染，Prism 风格）
 * 竖排页面 scroll-snap 上下翻页 + 缩放按钮 + 懒渲染
 * 依赖（index.html 先行加载）：lib/pdfjs/pdf.min.js（UMD → window.pdfjsLib）
 * ============================================================ */
(function () {
  'use strict';
  if (window.PdfView) return;

  const pdfjsLib = window.pdfjsLib || null;
  if (pdfjsLib) {
    try { pdfjsLib.GlobalWorkerOptions.workerSrc = 'lib/pdfjs/pdf.worker.min.js'; } catch (e) { /* file:// 下回退 fake worker */ }
  }

  const ZKEY = 'florialatex.pdfzoom.v2';   // v2：作废旧键（清掉旧版误存的缩放值）
  const GAP = 18;                 // 页间距 px
  const PAD = 20;                 // 预览井内边距 px（与 css #pdf-pages padding 一致）
  const MINZ = 0.4, MAXZ = 3.0;
  const KEEP_RADIUS = 6;          // 滑动窗口：中心页前后各保留 6 页 canvas，超出且不可见的及时回收防 OOM

  let doc = null;                 // pdf.js document
  let url = null;
  let pages = [];                 // {el, canvas, links, rendered, rendering, task, w, h, dirty, visible}
  let zoom = null;                // null=fit
  let queue = [];                 // 渲染队列（串行）
  let rendering = false;
  let seq = 0;                    // 会话号：load/zoom 变更后旧任务作废
  let observer = null;
  let scrollTimer = null;
  let onPage = null;              // (cur, total) 回调
  let onPick = null;              // (page, x, y) 回调：x/y 为页面坐标 pt（原点页面左上）
  let disposed = true;
  let lastCur = 1;                // 最近一次有效布局下算出的页码（隐藏态兜底）
  let pendingPage = 0;            // 隐藏态（display:none）下打开/跳页时暂存，恢复可见后立即定位

  /* 临时标记（高亮笔）：笔画只存内存，随重新载入文档（重新编译/换书）清空 */
  const SVGNS = 'http://www.w3.org/2000/svg';
  const PEN_W = 9;                // 笔宽（pt，页面坐标系，缩放自动跟随）
  let annotMode = false;
  let penColor = '#FFD84D';

  const $ = id => document.getElementById(id);

  function myZoom() {
    if (zoom === null) return 'fit';
    if (zoom === 'fitpage') return 'fitpage';
    return zoom;
  }
  function saveZoom() {
    try { localStorage.setItem(ZKEY, String(myZoom())); } catch (e) {}
  }
  function loadSavedZoom() {
    let saved = null;
    try { saved = localStorage.getItem(ZKEY); } catch (e) {}
    if (saved === 'fit' || saved === null) { zoom = null; return; }
    if (saved === 'fitpage') { zoom = 'fitpage'; return; }
    const n = parseFloat(saved);
    zoom = Number.isFinite(n) ? Math.min(MAXZ, Math.max(MINZ, n)) : null;
  }
  function syncZoomUi() {
    const z = myZoom();
    // 按钮文案固定 = 「界面调整」（顶栏只有三枚按钮，不拿按钮当读数）；当前值只在折叠面板里读
    // 折叠面板里的滑杆与读数跟随（面板未渲染时静默跳过）
    const rng = $('pdf-zoom-range'), zval = $('pdf-zoom-val');
    if (rng && zval) {
      const pct = (typeof z === 'number') ? Math.round(z * 100) : null;
      rng.value = pct === null ? 100 : Math.min(300, Math.max(40, pct));
      zval.textContent = pct === null ? (z === 'fitpage' ? '适页' : '适宽') : pct + '%';
    }
  }
  // 实际缩放系数：fit=容器宽自适应，fitpage=整页纳入视口，否则固定值
  function scaleOf(baseW, baseH) {
    const box = $('pdf-scroll');
    if (!box) return 1;
    const availW = box.clientWidth - PAD * 2;   // 竖排：#pdf-pages 左右 padding
    if (zoom === null) return Math.max(0.2, availW / baseW);
    if (zoom === 'fitpage') {
      const sw = availW / baseW;
      const sh = (box.clientHeight - PAD * 2) / (baseH || baseW * 1.414);
      return Math.max(0.2, Math.min(sw, sh));
    }
    return zoom;
  }

  // 释放单页 canvas 与链接层：先置 0×0 促使浏览器/WebKit 立即释放显存与位图缓冲
  function releasePage(pg) {
    if (!pg) return;
    if (pg.task) { try { pg.task.cancel(); } catch (e) {} }
    pg.rendering = null;
    pg.task = null;
    pg.rendered = false;
    pg.dirty = false;
    if (pg.canvas) {
      pg.canvas.width = 0;
      pg.canvas.height = 0;
      pg.canvas.remove();
      pg.canvas = null;
    }
    if (pg.links) {
      pg.links.remove();
      pg.links = null;
    }
  }

  // 滑动窗口回收：大文档滚久了会累计上百张高 DPR canvas 撑爆内存导致浏览器强杀重载
  function prunePages(center) {
    if (!pages.length) return;
    const c = Math.max(1, Math.min(pages.length, center || currentPage()));
    for (let i = 0; i < pages.length; i++) {
      const pg = pages[i];
      if (!pg || pg.visible || pg.rendering) continue;
      if (Math.abs((i + 1) - c) > KEEP_RADIUS && (pg.canvas || pg.rendered || pg.links)) {
        releasePage(pg);
      }
    }
    if (queue.length) {
      queue = queue.filter(n => {
        const pg = pages[n - 1];
        return pg && (pg.visible || Math.abs(n - c) <= KEEP_RADIUS);
      });
    }
  }

  function scrollToPage(n, smooth) {
    const box = $('pdf-scroll');
    if (!box || !pages.length) return;
    const idx = Math.max(0, Math.min(pages.length - 1, (n || 1) - 1));
    const top = Math.max(0, pages[idx].el.offsetTop - PAD);
    if (smooth && typeof box.scrollTo === 'function') {
      box.scrollTo({ top, behavior: 'smooth' });
    } else {
      box.scrollTop = top;
    }
  }

  function hasLayout(box) {
    return !!(box && box.clientWidth > 0 && box.clientHeight > 0 && pages.length && pages[0].el.offsetHeight > 0);
  }

  function reset() {
    seq++;
    queue = []; rendering = false;
    pendingPage = 0;
    if (observer) { observer.disconnect(); observer = null; }
    pages.forEach(releasePage);
    pages = [];
    const holder = $('pdf-pages');
    if (holder) holder.innerHTML = '';
    if (doc) { try { doc.destroy(); } catch (e) {} doc = null; }
  }

  function buildPages(targetPg, mySeq) {
    const holder = $('pdf-pages');
    pages = [];
    holder.innerHTML = '';
    for (let i = 1; i <= doc.numPages; i++) {
      const el = document.createElement('div');
      el.className = 'pv-page';
      el.dataset.n = i;
      holder.appendChild(el);
      pages.push({ el, canvas: null, links: null, svg: null, strokes: [], rendered: false, rendering: null, task: null, w: 0, h: 0, dirty: false, visible: false });
    }
    const want = Math.max(1, Math.min(pages.length, targetPg || 1));
    lastCur = want;
    // 占位尺寸：用第 1 页基准比例（各页尺寸可能不同，渲染时再修正）
    return doc.getPage(1).then(p1 => {
      if (mySeq !== seq || disposed) return;
      const b = p1.getViewport({ scale: 1 });
      const s = scaleOf(b.width, b.height);
      pages.forEach(pg => { pg.w = b.width; pg.h = b.height; sizePage(pg, s); ensureAnnotLayer(pg); });
      $('pdf-pages').style.setProperty('--pv-gap', GAP + 'px');
      const box = $('pdf-scroll');
      if (hasLayout(box)) {
        roW = box.clientWidth; roH = box.clientHeight;
        pendingPage = 0;
        if (want > 1) scrollToPage(want, false);
        else boxStart();
        observePages();
      } else {
        // 卡片处于隐藏态（display:none）：暂存目标页，等切回可见由 ResizeObserver 补定位与缩放
        roW = 0; roH = 0;
        pendingPage = want;
      }
      updateIndicator();
    });
  }

  function sizePage(pg, s) {
    pg.el.style.width = Math.round(pg.w * s) + 'px';
    pg.el.style.height = Math.round(pg.h * s) + 'px';
    if (pg.canvas) {
      pg.canvas.style.width = '100%';
      pg.canvas.style.height = '100%';
    }
  }

  function observePages() {
    if (observer) observer.disconnect();
    pages.forEach(pg => { pg.visible = false; });
    observer = new IntersectionObserver(entries => {
      entries.forEach(en => {
        const n = +en.target.dataset.n;
        const pg = pages[n - 1];
        if (pg) pg.visible = en.isIntersecting;
        if (en.isIntersecting) {
          enqueue(n);
          if (n + 1 <= pages.length) enqueue(n + 1);   // 预热邻页
          if (n - 1 >= 1) enqueue(n - 1);
        }
      });
    }, { root: $('pdf-scroll'), rootMargin: '120px', threshold: 0.05 });
    pages.forEach(pg => observer.observe(pg.el));
  }

  function enqueue(n) {
    const pg = pages[n - 1];
    if (!pg || pg.rendered || pg.rendering) return;
    if (!queue.includes(n)) queue.push(n);
    pump();
  }

  function pump() {
    if (rendering || !queue.length || disposed || !doc) return;
    rendering = true;
    const n = queue.shift();
    const pg = pages[n - 1];
    const mySeq = seq;
    doc.getPage(n).then(page => {
      if (mySeq !== seq || disposed) { rendering = false; queue = []; return; }
      const base = page.getViewport({ scale: 1 });
      pg.w = base.width; pg.h = base.height;
      const s = scaleOf(base.width, base.height);
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      const viewport = page.getViewport({ scale: s * dpr });
      sizePage(pg, s);
      let canvas = pg.canvas;
      if (!canvas) {
        canvas = document.createElement('canvas');
        canvas.style.width = '100%';
        canvas.style.height = '100%';
        pg.el.insertBefore(canvas, pg.el.firstChild);   // 垫在标记层/链接层之下
        pg.canvas = canvas;
      }
      canvas.width = Math.floor(viewport.width);
      canvas.height = Math.floor(viewport.height);
      const ctx = canvas.getContext('2d');
      const task = page.render({ canvasContext: ctx, viewport });
      pg.rendering = task;
      pg.task = task;
      return task.promise.then(() => {
        if (mySeq !== seq || disposed) return;
        pg.rendered = true; pg.rendering = null; pg.task = null;
        attachLinks(pg, page, s);          // 链接层随 canvas 一起铺（缩放重建 canvas 时会一并重铺）
        prunePages();
      });
    }).catch(err => {
      if (mySeq === seq && !disposed && err && err.name !== 'RenderingCancelledException') {
        console.error('PDF 页渲染失败 p' + n + ':', err);
      }
    }).then(() => {
      rendering = false;
      if (mySeq === seq && !disposed) pump();
    });
  }

  // 重设缩放：全部页改占位尺寸、标脏，可见页由 observer 重新触发渲染；
  // keepPage=true 停在当前页，正整数则跳到该页
  function applyZoom(keepPage) {
    if (!doc || disposed) return;
    const cur = (typeof keepPage === 'number' && keepPage >= 1) ? Math.min(pages.length, Math.floor(keepPage))
      : keepPage ? currentPage() : 1;
    seq++;
    queue = []; rendering = false;
    // 取消进行中的渲染并立刻归还 canvas 内存
    pages.forEach(pg => {
      releasePage(pg);
      sizePage(pg, scaleOf(pg.w, pg.h));
    });
    const box = $('pdf-scroll');
    if (box && box.clientWidth) { roW = box.clientWidth; roH = box.clientHeight; }
    syncZoomUi();
    saveZoom();
    if (keepPage && pages[cur - 1]) {
      scrollToPage(cur, false);
    }
    observePages();
    updateIndicator();
  }

  function updateIndicator() {
    if (!pages.length) return;
    const cur = currentPage();
    prunePages(cur);
    if (onPage) onPage(cur, pages.length);
  }

  function bindScroll() {
    const box = $('pdf-scroll');
    if (!box) return;
    box.addEventListener('scroll', () => {
      if (scrollTimer) return;
      scrollTimer = setTimeout(() => { scrollTimer = null; updateIndicator(); }, 120);
    }, { passive: true });
    bindWellResize(box);
  }

  /* 适宽/适页跟随预览井尺寸：拖分栏、开合大纲、窗口变化都会改变井宽，
     井窄于页宽时竖排无处横向滚动，必须重算缩放（防抖，拖拽过程不反复重建 canvas）。
     注意「防抖」挡不住**慢速拖拽**：拖一下停一下，140ms 的计时器照样到点，于是拖拽中途就整篇重建
     canvas（一百多页 = 一次大地图重绘）——用户报「调整到底线大小会有奇怪的卡顿」。
     故拖拽期间（body.dragging，由 js/core.js dragHandle 维护）不重算，松手后 'floria:dragend' 补一次。 */
  let ro = null, roW = 0, roH = 0, roTimer = null, roDragBind = false;
  function sizeNeedsRefit(box) {
    if (zoom === null) return Math.abs(box.clientWidth - roW) > 1;
    if (zoom === 'fitpage') {
      return Math.abs(box.clientWidth - roW) > 1 || Math.abs(box.clientHeight - roH) > 1;
    }
    return false;
  }
  function wellRefit(box) {
    if (!doc || disposed || !hasLayout(box)) return;
    if (document.body.classList.contains('dragging')) return;   // 拖拽中一律不重算（含拖拽前就排上的那次）
    if (pendingPage) {
      const target = pendingPage;
      pendingPage = 0;
      if (sizeNeedsRefit(box)) {
        lastCur = target;
        roW = box.clientWidth; roH = box.clientHeight;
        applyZoom(target);
      } else {
        roW = box.clientWidth; roH = box.clientHeight;
        if (target > 1) scrollToPage(target, false);
        else boxStart();
        observePages();
        updateIndicator();
      }
      return;
    }
    if (!sizeNeedsRefit(box)) {
      roW = box.clientWidth; roH = box.clientHeight;
      return;
    }
    roW = box.clientWidth; roH = box.clientHeight;
    applyZoom(true);
  }
  function scheduleRefit(box) {
    if (!doc || disposed || !hasLayout(box)) return;
    if (document.body.classList.contains('dragging')) return;   // 拖拽中不排队，松手后由 dragend 补
    if (pendingPage) {
      if (roTimer) { clearTimeout(roTimer); roTimer = null; }
      wellRefit(box);
      return;
    }
    if (!sizeNeedsRefit(box)) {
      roW = box.clientWidth; roH = box.clientHeight;
      return;
    }
    if (roTimer) clearTimeout(roTimer);
    roTimer = setTimeout(() => { roTimer = null; wellRefit(box); }, 140);
  }
  function bindWellResize(box) {
    roW = box.clientWidth; roH = box.clientHeight;
    if (typeof ResizeObserver !== 'undefined') {
      if (ro) ro.disconnect();
      ro = new ResizeObserver(() => scheduleRefit(box));
      ro.observe(box);
    } else {
      window.addEventListener('resize', () => scheduleRefit(box));
    }
    if (!roDragBind) {                      // bindWellResize 可能被多次调用，监听只挂一次
      roDragBind = true;
      window.addEventListener('floria:dragend', () => wellRefit(box));
    }
  }

  /* 双击页面 → 回调页面坐标（pt，原点左上）跳源码；单击留给标记落笔，不再触发跳转 */
  function bindPick() {
    const holder = $('pdf-pages');
    if (!holder || holder.dataset.pickBound) return;
    holder.dataset.pickBound = '1';
    holder.addEventListener('dblclick', e => {
      if (e.target.closest && e.target.closest('.pv-link')) return;   // 点在链接上 = 链接的事，不跳源码
      const el = e.target.closest ? e.target.closest('.pv-page') : null;
      if (!onPick || !el) return;
      const pg = pages[+el.dataset.n - 1];
      if (!pg || !pg.canvas) return;             // 未渲染的页没有坐标基准
      e.preventDefault();
      const p = pagePoint(el, pg, e);
      onPick(+el.dataset.n, p.x, p.y);
    });
  }

  /* ---------------- 临时标记（高亮笔） ----------------
     笔画按页面 pt 坐标存进 SVG 折线（viewBox = 页尺寸），缩放/滑动窗口重建 canvas 都自动跟随；
     只存内存，重新载入文档（重新编译/换书）即清空。 */
  function ensureAnnotLayer(pg) {
    if (pg.svg) return pg.svg;
    const svg = document.createElementNS(SVGNS, 'svg');
    svg.setAttribute('class', 'pv-annot');
    svg.setAttribute('preserveAspectRatio', 'none');
    svg.setAttribute('viewBox', '0 0 ' + pg.w + ' ' + pg.h);
    pg.el.appendChild(svg);
    pg.svg = svg;
    pg.strokes.forEach(s => { if (!s.el) drawStroke(pg, s); });
    return svg;
  }
  function strokePoints(pts) {
    return pts.map(p => p.x.toFixed(1) + ',' + p.y.toFixed(1)).join(' ');
  }
  function drawStroke(pg, s) {
    if (!pg.svg) return null;
    let el;
    if (s.pts.length === 1) {                 // 点一下 = 一个圆点
      el = document.createElementNS(SVGNS, 'circle');
      el.setAttribute('cx', s.pts[0].x.toFixed(1));
      el.setAttribute('cy', s.pts[0].y.toFixed(1));
      el.setAttribute('r', PEN_W / 2);
      el.setAttribute('fill', s.color);
      el.setAttribute('stroke', 'none');
    } else {
      el = document.createElementNS(SVGNS, 'polyline');
      el.setAttribute('points', strokePoints(s.pts));
      el.setAttribute('fill', 'none');
      el.setAttribute('stroke', s.color);
      el.setAttribute('stroke-width', PEN_W);
      el.setAttribute('stroke-linecap', 'round');
      el.setAttribute('stroke-linejoin', 'round');
    }
    pg.svg.appendChild(el);
    s.el = el;
    return el;
  }
  function pagePoint(el, pg, e) {
    const r = el.getBoundingClientRect();
    return {
      x: r.width ? (e.clientX - r.left) * pg.w / r.width : 0,
      y: r.height ? (e.clientY - r.top) * pg.h / r.height : 0,
    };
  }
  function bindDraw() {
    const holder = $('pdf-pages');
    if (!holder || holder.dataset.drawBound) return;
    holder.dataset.drawBound = '1';
    let cur = null;                            // {el, pg, start, stroke|null}
    holder.addEventListener('pointerdown', e => {
      if (!annotMode || e.button !== 0) return;
      const el = e.target.closest ? e.target.closest('.pv-page') : null;
      if (!el) return;
      const pg = pages[+el.dataset.n - 1];
      if (!pg || !pg.w) return;
      e.preventDefault();
      // 落笔推迟到指针真的挪动：单击（双击跳源码的第一下）不留墨点
      cur = { el, pg, start: pagePoint(el, pg, e), stroke: null };
      try { el.setPointerCapture(e.pointerId); } catch (err) { /* 捕获失败也能画，只是移出页面就断笔 */ }
    });
    holder.addEventListener('pointermove', e => {
      if (!cur) return;
      const pt = pagePoint(cur.el, cur.pg, e);
      if (!cur.stroke) {
        if (Math.hypot(pt.x - cur.start.x, pt.y - cur.start.y) < 1.5) return;
        cur.stroke = { color: penColor, pts: [cur.start, pt], el: null };
        cur.pg.strokes.push(cur.stroke);
        ensureAnnotLayer(cur.pg);
        drawStroke(cur.pg, cur.stroke);
        return;
      }
      const last = cur.stroke.pts[cur.stroke.pts.length - 1];
      if (Math.hypot(pt.x - last.x, pt.y - last.y) < 0.6) return;
      cur.stroke.pts.push(pt);
      cur.stroke.el.setAttribute('points', strokePoints(cur.stroke.pts));
    });
    const end = () => { cur = null; };
    holder.addEventListener('pointerup', end);
    holder.addEventListener('pointercancel', end);
  }

  /* ---------------- PDF 内链（超链接注释） ----------------
   页面上的链接是 PDF 的 Link 注释，不画在 canvas 里——自绘查看器必须自己铺一层可点区域，
   否则目录页/交叉引用/\cite 那些框看着像链接、点下去只会触发「跳源码」。
   坐标：注释 rect 是 PDF 用户空间（原点右下），经 viewport 换算成页面元素内的左上原点像素。 */
  function attachLinks(pg, page, s) {
    if (pg.links) { pg.links.remove(); pg.links = null; }
    const mySeq = seq;
    return page.getAnnotations({ intent: 'display' }).then(annots => {
      if (mySeq !== seq || disposed) return;
      const links = annots.filter(a => a.subtype === 'Link' && a.rect);
      if (!links.length) return;
      const vp = page.getViewport({ scale: s });
      const layer = document.createElement('div');
      layer.className = 'pv-links';
      links.forEach(a => {
        const r = vp.convertToViewportRectangle(a.rect);      // [x1,y1,x2,y2]
        const el = document.createElement('a');
        el.className = 'pv-link';
        el.style.left = Math.min(r[0], r[2]) + 'px';
        el.style.top = Math.min(r[1], r[3]) + 'px';
        el.style.width = Math.abs(r[2] - r[0]) + 'px';
        el.style.height = Math.abs(r[3] - r[1]) + 'px';
        // 链接框（hyperref 的 linkbordercolor 红/绿/青）= **注释边框**，不在页面内容流里，
        // canvas 画不出来，Edge 等阅读器是按 /Border + /C 现画的——我们也照画，否则看着不像链接。
        const bs = a.borderStyle || {};
        const bw = bs.width || 0;
        if (bw > 0) {
          const c = a.color;                                  // Uint8ClampedArray [r,g,b]
          const rgb = c ? 'rgb(' + c[0] + ',' + c[1] + ',' + c[2] + ')' : 'rgb(17,17,17)';
          const w = Math.max(1, Math.round(bw * s)) + 'px';
          // PDF 边框样式：0/缺省=实线 1=虚线（hyperref 默认就是这个）2=凸 3=凹 4=下划线
          const st = ({ 1: 'dashed', 2: 'solid', 3: 'solid', 4: 'solid' })[bs.style] || 'solid';
          if (bs.style === 4) {                             // 下划线：只留下边
            el.style.borderBottom = w + ' ' + st + ' ' + rgb;
          } else {
            el.style.border = w + ' ' + st + ' ' + rgb;
          }
        }
        if (a.url) {                                          // 外部链接：新标签页打开
          el.href = a.url; el.target = '_blank'; el.rel = 'noopener';
        } else if (a.dest) {                                  // 内部链接：跳到目标页
          el.href = '#';
          el.addEventListener('click', ev => {
            ev.preventDefault(); ev.stopPropagation();
            destPage(a.dest).then(n => { if (n) PdfView.goTo(n); });
          });
        } else return;
        layer.appendChild(el);
      });
      if (layer.children.length) { pg.el.appendChild(layer); pg.links = layer; }
    }).catch(() => { /* 注释读取失败不该影响正文渲染 */ });
  }

  /* 命名目标 / 目标数组 → 页码（1 基）；解析不出来给 null（条目仍列出，只是不可点） */
  function destPage(dest) {
    if (!doc) return Promise.resolve(null);
    return Promise.resolve()
      .then(() => (typeof dest === 'string' ? doc.getDestination(dest) : dest))
      .then(d => {
        if (!d || !d.length) return null;
        const ref = d[0];
        if (typeof ref === 'number') return ref + 1;
        return doc.getPageIndex(ref).then(i => i + 1);
      })
      .catch(() => null);
  }

  /* 在点中的位置画一个淡出的圈（跳转成功的视觉确认） */
  function markAt(page, x, y) {
    const pg = pages[page - 1];
    if (!pg) return;
    const el = pg.el;
    const r = el.getBoundingClientRect();
    const mark = document.createElement('div');
    mark.className = 'pv-mark';
    mark.style.left = (r.width ? x * r.width / pg.w : 0) + 'px';
    mark.style.top = (r.height ? y * r.height / pg.h : 0) + 'px';
    el.appendChild(mark);
    setTimeout(() => mark.remove(), 900);
  }

  /* ---------------- 公开 API ---------------- */
  const PdfView = {
    supported: !!pdfjsLib,

    // keep：
    //   · true   = 编译/轮询重载——回到重载前所在页（页码须在 reset() 前取）
    //   · 正整数 = 显式指定初始页（如从 localStorage 恢复上次页码，或宿主 open{page}）
    //   · 其它   = 第 1 页
    load(u, cb, pick, keep) {
      let targetPg = 0;
      if (typeof keep === 'number' && Number.isFinite(keep) && keep >= 1) {
        targetPg = Math.floor(keep);
      } else if (keep === true && pages.length) {
        targetPg = currentPage();
      }
      onPage = cb || onPage;
      onPick = pick || null;
      reset();
      bindPick();
      bindDraw();
      if (!pdfjsLib) return Promise.reject(new Error('pdf.js 未加载'));
      disposed = false;
      url = u;
      loadSavedZoom();
      syncZoomUi();
      const mySeq = seq;
      const q = (u + (u.indexOf('?') >= 0 ? '&' : '?') + '_pv=' + Date.now());
      return pdfjsLib.getDocument({ url: q, disableAutoFetch: false }).promise.then(d => {
        if (mySeq !== seq || disposed) { try { d.destroy(); } catch (e) {} return; }
        doc = d;
        return buildPages(targetPg, mySeq);
      });
    },

    destroy() {
      disposed = true;
      if (roTimer) { clearTimeout(roTimer); roTimer = null; }
      if (scrollTimer) { clearTimeout(scrollTimer); scrollTimer = null; }
      seq++;
      queue = []; rendering = false;
      if (observer) { observer.disconnect(); observer = null; }
      pages.forEach(releasePage);
      const holder = $('pdf-pages');
      if (holder) holder.innerHTML = '';
      pages = [];
      if (doc) { try { doc.destroy(); } catch (e) {} doc = null; }
    },

    zoomIn() { const s = scaleOf(pages[0] ? pages[0].w : 612, pages[0] ? pages[0].h : 792); const cur = (zoom === null || zoom === 'fitpage') ? s : zoom; zoom = Math.min(MAXZ, Math.round(cur * 1.2 * 100) / 100); applyZoom(true); },
    zoomOut() { const s = scaleOf(pages[0] ? pages[0].w : 612, pages[0] ? pages[0].h : 792); const cur = (zoom === null || zoom === 'fitpage') ? s : zoom; zoom = Math.max(MINZ, Math.round(cur / 1.2 * 100) / 100); applyZoom(true); },
    zoomFit() { zoom = null; applyZoom(true); },
    zoomFitPage() { zoom = 'fitpage'; applyZoom(true); },
    zoomSet(v) { const n = parseFloat(v); if (isNaN(n)) return; zoom = Math.min(MAXZ, Math.max(MINZ, n)); applyZoom(true); },
    getZoom() { return myZoom(); },
    currentPage() { return currentPage(); },

    markAt,

    /* 临时标记：开/关标记模式（开 = 笔落在页面上画高亮线，点 PDF 不再跳源码） */
    setAnnot(on) {
      annotMode = !!on;
      const holder = $('pdf-pages');
      if (holder) holder.classList.toggle('annot-mode', annotMode);
      return annotMode;
    },
    annotOn() { return annotMode; },
    setPen(color) {
      if (typeof color === 'string' && /^#[0-9a-fA-F]{3,8}$/.test(color)) penColor = color;
      return penColor;
    },
    penColor() { return penColor; },
    clearAnnot() {
      pages.forEach(pg => {
        pg.strokes = [];
        if (pg.svg) pg.svg.innerHTML = '';
      });
    },

    /* PDF 自带书签（hyperref 生成的章节树）→ 归一成 {title, page, children[]}；
       LaTeX 文档只要交了 hyperref 就有，没书签给空数组。解析不出的条目 page=null（仍列出，只是不可点）。 */
    getOutline() {
      if (!doc) return Promise.resolve([]);
      const walk = it => destPage(it.dest).then(page => {
        const kids = (it.items && it.items.length)
          ? Promise.all(it.items.map(walk)) : Promise.resolve([]);
        return kids.then(children => ({
          title: String(it.title || '').replace(/\s+/g, ' ').trim() || '(无标题)',
          page, children,
        }));
      });
      return doc.getOutline()
        .then(items => (items && items.length) ? Promise.all(items.map(walk)) : [])
        .catch(() => []);
    },

    goTo(n) {
      if (!pages[n - 1]) return;
      const box = $('pdf-scroll');
      if (!hasLayout(box)) {
        pendingPage = n;
        lastCur = n;
        updateIndicator();
        return;
      }
      scrollToPage(n, true);
    },
    next() { this.goTo(currentPage() + 1); },
    prev() { this.goTo(currentPage() - 1); },

    refresh() { // 编译后重载同一 URL（保持当前页）
      if (url) return this.load(url, onPage, onPick, true);
      return Promise.resolve();
    },
  };

  function currentPage() {
    const box = $('pdf-scroll');
    if (!pages.length) return 1;
    if (!hasLayout(box)) {
      return Math.max(1, Math.min(pages.length, pendingPage || lastCur || 1));
    }
    const center = box.scrollTop + box.clientHeight / 2;
    for (let i = 0; i < pages.length; i++) {
      const el = pages[i].el;
      if (el.offsetTop + el.offsetHeight > center) {
        lastCur = i + 1;
        return lastCur;
      }
    }
    lastCur = pages.length;
    return lastCur;
  }

  function boxStart() { const box = $('pdf-scroll'); if (box) box.scrollTop = 0; }

  bindScroll();
  window.PdfView = PdfView;
})();
