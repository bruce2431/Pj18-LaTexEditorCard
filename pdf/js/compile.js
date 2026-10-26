/* Floria LaTeX v2 — 编译控制（从 viewer.js 拆出，2026-10-26）
   职责：编译五态（按钮文案）、发起 /api/compile、成功后重载 PDF。
   失败时：桥接态切到独立的「编译日志」卡；单开态只 toast（本页不再维护日志面板）。
   模型：mode='fast' 快速分章；mode='full' 明确整本模式（reason_code 说明原因，不再叫「回落」）。 */
(function () {
  'use strict';
  const F = window.Floria;
  const $ = F.$;
  const S = F.state;

  const ST = { none: '编译', busy: '编译中…', ok: '编译', stale: '重新编译', err: '重试编译' };
  function setState(st) {
    S.state = st;
    const b = $('v-compile');
    if (!b) return;
    b.textContent = ST[st] || ST.none;
    b.className = 'vt-btn primary st-' + st;
    b.disabled = st === 'busy';
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
    if (r.ok) {
      S.pdfMtime = r.mtime || Date.now();
      setState('ok');
      const btn = $('v-compile');
      if (r.mode === 'full') {
        const why = r.reason || r.reason_code || '快速路径不适用';
        F.toast('整本编译完成（' + why + '，' + elapsed + 's）');
        if (btn) btn.title = '本次整本模式原因：' + why;
      } else {
        F.toast('编译完成（' + elapsed + 's）');
        if (btn) btn.title = '本机重新编译这篇论文';
      }
      await F.viewer.loadPdf(false, true);   // 保持当前页码/缩放
    } else {
      setState('err');
      // 编译失败 → 切到独立的「编译日志」卡（宿主顶栏随之落到日志 tab）。
      // 单开（没有 parent）时没有日志卡，只 toast 报错。
      const payload = {
        book: S.book,
        path: S.book + '/' + (S.texName || 'main.tex'),
        error: r.error || '编译失败',
        ts: Date.now() / 1000,
      };
      F.bridge.switchCard('log', payload);
      F.toast('编译失败：' + (r.error || '见日志'));
    }
    F.viewer.emitState();
  }

  F.compile = { compile, setState, ST };
})();
