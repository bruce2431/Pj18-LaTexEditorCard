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

  /* ---- 编译计时器（2026-10-28 待办）----
     busy 态按钮文案改成「编译中 3.4s」，每 0.1s 刷新。计时起点是 setState('busy')
     那一刻，不看单条请求活多久：iPad 挂起时请求会丢、服务端还在编，用 Date.now()
     差值取整段经过时间才对得上真实耗时。切后台期间定时器被冻结也无所谓，
     回前台下一次 tick 直接算总差值。 */
  let clockTimer = null;
  let clockStart = 0;

  function busyLabel(ms) {
    return '编译中 ' + (ms / 1000).toFixed(1) + 's';
  }

  function stopClock() {
    if (clockTimer) { clearInterval(clockTimer); clockTimer = null; }
  }

  function startClock() {
    stopClock();
    clockStart = Date.now();
    const b = $('v-compile');
    if (b) b.textContent = busyLabel(0);
    clockTimer = setInterval(function () {
      const btn = $('v-compile');
      if (!btn || S.state !== 'busy') { stopClock(); return; }
      btn.textContent = busyLabel(Date.now() - clockStart);
    }, 100);
  }

  function setState(st) {
    const prev = S.state;
    S.state = st;
    // 只在进入 busy 时起表；busy 期间重复调用不重置计时
    if (st === 'busy') {
      if (prev !== 'busy' || !clockTimer) startClock();
    } else {
      stopClock();
    }
    const b = $('v-compile');
    if (!b) return;
    b.textContent = st === 'busy' ? busyLabel(Date.now() - clockStart) : (ST[st] || ST.none);
    b.className = 'vt-btn primary st-' + st;
    b.disabled = st === 'busy';
  }

  /* ---- 编译态自愈（2026-10-26 iPad 卡在「编译中…」定案）----
     iPad/手机切后台时 Safari 会挂起页面，这条长请求可能**永不返回**也无异常，
     S.busy 就永久为 true、按钮永远停在「编译中…」。这里在编译期间轮询
     服务端状态（/api/history 自带 compile 快照，毫秒级）：连续两次看到服务端
     已 idle 就认为那次请求的响应丢了，主动收尾并重载 PDF。 */
  let pollTimer = null;
  let pollTick = null;
  let tryId = 0;

  function stopPoll() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    pollTick = null;
  }

  // 回前台立刻查一次（iPad 后台期间定时器被冻结，恢复后可能迟迟不触发）
  document.addEventListener('visibilitychange', function () {
    if (document.hidden) {
      if (S.busy) F.toast('编译在后台继续，回到本页会自动刷新');
      return;
    }
    if (pollTick) pollTick();
  });

  // 服务端状态 + 最新一条账本记录（records[0] 最新）。
  // busy 为 null = 查不到（网络/网关抖动），调用方不断言、继续等。
  async function serverState() {
    try {
      const d = await F.api('api/history?book=' + encodeURIComponent(S.book));
      if (!d) return { busy: null, rec: null };
      return { busy: !!(d.compile && d.compile.status === 'busy'),
               rec: (d.records && d.records[0]) || null };
    } catch (e) {
      return { busy: null, rec: null };
    }
  }

  function refreshMtime() {
    F.api('api/books').then(list => {
      const b = (Array.isArray(list) ? list : []).find(x => x.name === S.book);
      if (b && b.pdf && b.pdf.mtime) S.pdfMtime = b.pdf.mtime;
    }).catch(() => {});
  }

  // 请求丢了（iPad 挂起 / 网关超时）但服务端已经跑完：按账本最后一条判定成败，
  // 别一律当成功——否则编译失败也会显示「编译完成」。
  async function recover(id) {
    if (id !== tryId) return;
    const st = await serverState();
    if (id !== tryId) return;
    stopPoll();
    tryId++;
    S.busy = false;
    const rec = st.rec;
    if (rec && rec.ok === false) {
      fail(rec);
      return;
    }
    setState('ok');
    F.toast('编译已在后台结束，正在刷新 PDF…');
    refreshMtime();
    F.viewer.loadPdf(false, true);
    F.viewer.emitState();
  }

  function startPoll(id) {
    stopPoll();
    let idle = 0;
    const tick = async () => {
      if (id !== tryId) { stopPoll(); return; }
      if (document.hidden) return;      // 后台先不查，回前台立刻补
      const st = await serverState();
      if (id !== tryId) { stopPoll(); return; }
      if (st.busy === false) {
        if (++idle >= 2) recover(id);
      } else {
        idle = 0;
      }
    };
    pollTick = tick;
    pollTimer = setInterval(tick, 2500);
  }

  // 编译失败 → 切到独立的「编译日志」卡（宿主顶栏随之落到日志 tab）。
  // 单开（没有 parent）时没有日志卡，只 toast 报错。
  function fail(r) {
    stopPoll();
    S.busy = false;
    setState('err');
    const why = r.error || '编译失败';
    F.bridge.switchCard('log', {
      book: S.book,
      path: S.book + '/' + (S.texName || 'main.tex'),
      error: why,
      ts: r.ts || Date.now() / 1000,
    });
    F.toast('编译失败：' + (r.error || '见日志'));
    F.viewer.emitState();
  }

  async function compile() {
    if (S.busy || !S.book) return;
    S.busy = true;
    setState('busy');
    const myId = ++tryId;
    startPoll(myId);
    let r;
    try {
      r = await F.post('api/compile', { book: S.book });
    } catch (e) {
      r = { ok: false, error: e.message || String(e), log: '' };
    }
    if (myId !== tryId) return;         // 已被轮询自愈接管（响应迟到的旧请求）
    const elapsed = r.time != null ? r.time : 0;

    if (!r.ok) {
      // 「这里显示又会退了」（2026-10-26 iPad）：POST 报错/超时返回时，服务端可能
      // 还在编译。先问一句，仍在跑就**保持编译态继续轮询**，别把按钮拨回失败。
      const st = await serverState();
      if (myId !== tryId) return;
      if (st.busy) {
        F.toast('编译仍在后台进行，完成后自动刷新');
        return;                          // S.busy 仍为 true，poll 继续
      }
      if (st.rec && st.rec.ok === true) {   // 其实成功了，只是响应丢了
        r = { ok: true, mode: st.rec.mode, reason: st.rec.reason };
      }
    }

    stopPoll();
    S.busy = false;
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
      F.viewer.emitState();
    } else {
      fail(r);
    }
  }

  F.compile = { compile, setState, ST };
})();
