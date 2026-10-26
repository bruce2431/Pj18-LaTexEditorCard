/* Floria LaTeX v2 — 目录抽屉（PDF 自带书签 → 跳页）
   书签树由 pdf.js 读（PdfView.getOutline），本文件只管画与跳。
   入口 = 顶栏 #toc-toggle；收起 = 卡头 ✕ / Esc / 再点入口。 */
(function () {
  'use strict';
  const F = window.Floria;
  const $ = F.$, esc = F.esc;
  const S = F.state;

  const isOpen = () => !$('toc-drawer').classList.contains('hidden');

  function act(on) {
    $('toc-drawer').classList.toggle('hidden', !on);
    $('toc-toggle').classList.toggle('on', on);   // 入口在顶栏，常驻；开着时只是变「按下」态
    if (on) render();
  }

  function render() {
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
      if (!isOpen()) return;                        // 期间被关掉就别再画
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
      mark();
    }).catch(() => {
      if (isOpen()) body.innerHTML = empty('书签读取失败', '可以直接在 PDF 的目录页点链接跳转。');
    });
  }

  // 高亮「当前章节」= 起始页不超过当前页的最后一枚（滚动时跟着走）
  function mark() {
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

  F.toc = { act, render, mark, isOpen };
})();
