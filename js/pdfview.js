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

  let doc = null;                 // pdf.js document
  let url = null;
  let pages = [];                 // {el, canvas, rendered, rendering, task, w, h, dirty}
  let zoom = null;                // null=fit
  let queue = [];                 // 渲染队列（串行）
  let rendering = false;
  let seq = 0;                    // 会话号：load/zoom 变更后旧任务作废
  let observer = null;
  let scrollTimer = null;
  let onPage = null;              // (cur, total) 回调
  let onPick = null;              // (page, x, y) 回调：x/y 为页面坐标 pt（原点页面左上）
  let disposed = true;

  const $ = id => document.getElementById(id);

  function myZoom() {
    if (zoom === null) return 'fit';
    if (zoom === 'fitpage') return 'fitpage';
    return zoom;
  }
  function saveZoom() {
    try { localStorage.setItem(ZKEY, String(myZoom())); } catch (e) {}
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

  function reset() {
    seq++;
    queue = []; rendering = false;
    if (observer) { observer.disconnect(); observer = null; }
    pages = [];
    const holder = $('pdf-pages');
    if (holder) holder.innerHTML = '';
    if (doc) { try { doc.destroy(); } catch (e) {} doc = null; }
  }

  function buildPages() {
    const holder = $('pdf-pages');
    pages = [];
    holder.innerHTML = '';
    for (let i = 1; i <= doc.numPages; i++) {
      const el = document.createElement('div');
      el.className = 'pv-page';
      el.dataset.n = i;
      holder.appendChild(el);
      pages.push({ el, canvas: null, rendered: false, rendering: null, task: null, w: 0, h: 0, dirty: false });
    }
    // 占位尺寸：用第 1 页基准比例（各页尺寸可能不同，渲染时再修正）
    return doc.getPage(1).then(p1 => {
      if (disposed) return;
      const b = p1.getViewport({ scale: 1 });
      const s = scaleOf(b.width, b.height);
      pages.forEach(pg => { pg.w = b.width; pg.h = b.height; sizePage(pg, s); });
      $('pdf-pages').style.setProperty('--pv-gap', GAP + 'px');
      observePages();
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
    observer = new IntersectionObserver(entries => {
      entries.forEach(en => {
        if (en.isIntersecting) {
          const n = +en.target.dataset.n;
          enqueue(n);
          if (n + 1 <= pages.length) enqueue(n + 1);   // 预热邻页
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
        pg.el.appendChild(canvas);
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

  // 重设缩放：全部页改占位尺寸、标脏，可见页由 observer 重新触发渲染；keepPage=缩放后停在当前页
  function applyZoom(keepPage) {
    if (!doc || disposed) return;
    const cur = keepPage ? currentPage() : 1;
    seq++;
    queue = []; rendering = false;
    // 取消进行中的渲染
    pages.forEach(pg => {
      if (pg.task) { try { pg.task.cancel(); } catch (e) {} }
      pg.rendering = null; pg.task = null;
      pg.rendered = false; pg.dirty = false;
      if (pg.canvas) { pg.canvas.remove(); pg.canvas = null; }
      if (pg.links) { pg.links.remove(); pg.links = null; }   // 链接层坐标随缩放失效，一并丢弃
      sizePage(pg, scaleOf(pg.w, pg.h));
    });
    if (observer) { observer.disconnect(); observePages(); }
    const z = myZoom();
    // 按钮文案固定 = 「界面调整」（顶栏只有三枚按钮，不拿按钮当读数）；当前值只在折叠面板里读
    // 折叠面板里的滑杆与读数跟随（面板未渲染时静默跳过）
    const rng = $('pdf-zoom-range'), zval = $('pdf-zoom-val');
    if (rng && zval) {
      const pct = (typeof z === 'number') ? Math.round(z * 100) : null;
      rng.value = pct === null ? 100 : Math.min(300, Math.max(40, pct));
      zval.textContent = pct === null ? (z === 'fitpage' ? '适页' : '适宽') : pct + '%';
    }
    saveZoom();
    if (keepPage && pages[cur - 1]) {
      requestAnimationFrame(() => {
        if (disposed || seq === 0) return;
        pages[cur - 1].el.scrollIntoView({ inline: 'nearest', block: 'start' });
      });
    }
  }

  function updateIndicator() {
    if (!onPage || !pages.length) return;
    const box = $('pdf-scroll');
    if (!box) return;
    const center = box.scrollTop + box.clientHeight / 2;   // 竖排：以视口垂直中点为基准
    let cur = 1;
    for (let i = 0; i < pages.length; i++) {
      const el = pages[i].el;
      const t = el.offsetTop, b = t + el.offsetHeight;
      if (center >= t && center <= b) { cur = i + 1; break; }
      if (b > center) { cur = i + 1; break; }
    }
    onPage(cur, pages.length);
  }

  function bindScroll() {
    const box = $('pdf-scroll');
    if (!box) return;
    box.addEventListener('scroll', () => {
      if (scrollTimer) return;
      scrollTimer = setTimeout(() => { scrollTimer = null; updateIndicator(); }, 120);
    }, { passive: true });
    window.addEventListener('resize', () => {
      if (!doc || disposed) return;
      if (zoom === null) applyZoom(true);       // fit 模式跟随窗口
    });
    bindWellResize(box);
  }

  /* 适宽/适页跟随预览井尺寸：拖分栏、开合大纲、窗口变化都会改变井宽，
     井窄于页宽时竖排无处横向滚动，必须重算缩放（防抖，拖拽过程不反复重建 canvas）。
     注意「防抖」挡不住**慢速拖拽**：拖一下停一下，140ms 的计时器照样到点，于是拖拽中途就整篇重建
     canvas（一百多页 = 一次大地图重绘）——用户报「调整到底线大小会有奇怪的卡顿」。
     故拖拽期间（body.dragging，由 js/core.js dragHandle 维护）不重算，松手后 'floria:dragend' 补一次。 */
  let ro = null, roW = 0, roH = 0, roTimer = null, roDragBind = false;
  function wellRefit(box) {
    if (!doc || disposed || !box.clientWidth) return;
    if (document.body.classList.contains('dragging')) return;   // 拖拽中一律不重算（含拖拽前就排上的那次）
    if (box.clientWidth === roW && box.clientHeight === roH) return;
    roW = box.clientWidth; roH = box.clientHeight;
    if (zoom !== null && zoom !== 'fitpage') return;
    applyZoom(true);
  }
  function bindWellResize(box) {
    if (typeof ResizeObserver === 'undefined') return;
    if (ro) ro.disconnect();
    roW = box.clientWidth; roH = box.clientHeight;
    ro = new ResizeObserver(() => {
      if (!doc || disposed || !box.clientWidth) return;
      if (document.body.classList.contains('dragging')) return;   // 拖拽中不排队，松手后由 dragend 补
      if (box.clientWidth === roW && box.clientHeight === roH) return;
      if (roTimer) clearTimeout(roTimer);
      roTimer = setTimeout(() => { roTimer = null; wellRefit(box); }, 140);
    });
    ro.observe(box);
    if (!roDragBind) {                      // bindWellResize 可能被多次调用，监听只挂一次
      roDragBind = true;
      window.addEventListener('floria:dragend', () => wellRefit(box));
    }
  }

  /* 点击/轻触页面 → 回调页面坐标（pt，原点左上）；拖动翻页不算点击 */
  function bindPick() {
    const holder = $('pdf-pages');
    if (!holder || holder.dataset.pickBound) return;
    holder.dataset.pickBound = '1';
    let downX = 0, downY = 0, moved = false, el = null;
    holder.addEventListener('pointerdown', e => {
      downX = e.clientX; downY = e.clientY; moved = false;
      el = e.target.closest ? e.target.closest('.pv-page') : null;
    });
    holder.addEventListener('pointermove', e => {
      if (Math.abs(e.clientX - downX) > 6 || Math.abs(e.clientY - downY) > 6) moved = true;
    });
    holder.addEventListener('pointerup', e => {
      if (e.target.closest && e.target.closest('.pv-link')) return;   // 点在链接上 = 链接的事，不跳源码
      const tgt = e.target.closest ? e.target.closest('.pv-page') : null;
      if (!onPick || moved || !el || tgt !== el) return;
      const pg = pages[+el.dataset.n - 1];
      if (!pg || !pg.canvas) return;             // 未渲染的页没有坐标基准
      const r = el.getBoundingClientRect();
      const x = (e.clientX - r.left) * pg.w / r.width;
      const y = (e.clientY - r.top) * pg.h / r.height;
      onPick(+el.dataset.n, x, y);
    });
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

    load(u, cb, pick) {
      onPage = cb || onPage;
      onPick = pick || null;
      reset();
      bindPick();
      if (!pdfjsLib) return Promise.reject(new Error('pdf.js 未加载'));
      disposed = false;
      url = u;
      const mySeq = seq;
      const q = (u + (u.indexOf('?') >= 0 ? '&' : '?') + '_pv=' + Date.now());
      return pdfjsLib.getDocument({ url: q, disableAutoFetch: false }).promise.then(d => {
        if (mySeq !== seq || disposed) return;
        doc = d;
        return buildPages().then(() => {
          if (mySeq !== seq || disposed) return;
          // 恢复记忆缩放（fit=适宽 / fitpage=适页 / 数字=固定倍率）
          let saved = null;
          try { saved = localStorage.getItem(ZKEY); } catch (e) {}
          zoom = (saved === 'fit' || saved === null) ? null
               : saved === 'fitpage' ? 'fitpage'
               : Math.min(MAXZ, Math.max(MINZ, parseFloat(saved) || null));
          applyZoom();
          boxStart();
        });
      });
    },

    destroy() {
      disposed = true;
      seq++;
      queue = []; rendering = false;
      if (observer) { observer.disconnect(); observer = null; }
      pages.forEach(pg => { if (pg.task) { try { pg.task.cancel(); } catch (e) {} } });
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

    markAt,

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
      const pg = pages[n - 1];
      if (!pg) return;
      pg.el.scrollIntoView({ behavior: 'smooth', inline: 'nearest', block: 'start' });
    },
    next() { this.goTo(currentPage() + 1); },
    prev() { this.goTo(currentPage() - 1); },

    refresh() { // 编译后重载同一 URL（跳到第 1 页）
      if (url) return this.load(url, onPage);
      return Promise.resolve();
    },
  };

  function currentPage() {
    const box = $('pdf-scroll');
    if (!box || !pages.length) return 1;
    const center = box.scrollTop + box.clientHeight / 2;
    for (let i = 0; i < pages.length; i++) {
      const el = pages[i].el;
      if (center >= el.offsetTop && center <= el.offsetTop + el.offsetHeight) return i + 1;
    }
    return 1;
  }

  function boxStart() { const box = $('pdf-scroll'); if (box) box.scrollTop = 0; }

  bindScroll();
  window.PdfView = PdfView;
})();
