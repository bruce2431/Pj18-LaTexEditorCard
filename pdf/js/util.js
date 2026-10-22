/* Floria LaTeX v2 — 基础设施：$ / toast / 相对时间 / HTML 转义 / api() 取数
   全局命名空间 window.Floria（classic script，零构建、file:// 可开）。 */
(function () {
  'use strict';
  const F = (window.Floria = window.Floria || {});
  const $ = id => document.getElementById(id);

  let toastTimer = null;
  function toast(text) {
    const t = $('toast');
    if (!t) return;
    t.textContent = text;
    t.classList.remove('hidden');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.add('hidden'), 2200);
  }

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  /* 相对时间：刚刚 / N分钟前 / N小时前 / N天前 / YYYY/M/D；ms 取不到给「—」 */
  function fmtRel(ms) {
    if (!ms || ms < 0) return '—';
    const d = Date.now() - ms;
    if (d < 60e3) return '刚刚';
    if (d < 3600e3) return Math.floor(d / 60e3) + '分钟前';
    if (d < 86400e3) return Math.floor(d / 3600e3) + '小时前';
    if (d < 7 * 86400e3) return Math.floor(d / 86400e3) + '天前';
    const t = new Date(ms);
    return t.getFullYear() + '/' + (t.getMonth() + 1) + '/' + t.getDate();
  }

  /* 反代门票（floria_bp）自愈：托管帧跑在 /backend/<label>/ 反代后面时，网关凭**内存里**的门票校验
     每次请求（门票随网关进程存亡，网关重启/换进程即丢），而浏览器里那条 cookie 还在 —— 丢票后 API
     一律回 401 {"error":"unauthorized"}，表现为「查询失败：unauthorized」，只能重开文件（宿主重挂帧）才恢复。
     这里在 401 时自己补票：同源的 /gateway/backend 会重新签发并 Set-Cookie，随后原请求重试一次。
     单开（直起 server.py / file://）没有 /backend/ 前缀 → bpLabel() 为空，完全不介入。 */
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
    if (!ticketP) {          // 并发 401 共用同一次补票
      ticketP = fetch('/gateway/backend?label=' + encodeURIComponent(label))
        .then(r => r.ok).catch(() => false)
        .then(ok => { ticketP = null; return ok; });
    }
    return ticketP;
  }

  /* 卡片跑在 <base>/<cardId>/<entry> 里，而 API 恒挂在 <base>/api/ 下：
       /backend/<label>/pdf/index.html → /backend/<label>/api/...
       单开 /pdf/index.html            → /api/...
     调方一律写 'api/xxx'，这里统一改写（绝对地址等其它写法原样放过）。 */
  function apiUrl(url) {
    const s = String(url == null ? '' : url);
    if (!/^api\//.test(s)) return s;
    const p = (window.location && window.location.pathname) || '';
    const cut = p.lastIndexOf('/');                    // 去掉 entry 文件名
    const dir = cut >= 0 ? p.slice(0, cut) : p;        // <base>/<cardId>
    const cut2 = dir.lastIndexOf('/');
    const base = cut2 >= 0 ? dir.slice(0, cut2) : '';  // <base>
    return base + '/' + s;
  }

  /* 取数封装：解析 JSON 后按 HTTP 状态抛错；业务失败（200 + ok:false）不抛，交调用方判 ok。
     网络不通抛「连不上本地服务器」——目录页的错误态据此出启动提示。 */
  async function api(url, opts) {
    url = apiUrl(url);
    const doFetch = async () => {
      try { return await fetch(url, opts); }
      catch (e) {
        const err = new Error('连不上本地服务器');
        err.net = true;
        throw err;
      }
    };
    let r = await doFetch();
    if (r.status === 401) {                      // 门票丢了：补票后重试一次（见上）
      const b = await r.clone().json().catch(() => ({}));
      if (b && b.error === 'unauthorized' && await renewTicket()) r = await doFetch();
    }
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.error || ('HTTP ' + r.status));
    return d;
  }

  function post(url, body) {
    return api(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    });
  }

  F.$ = $;
  F.toast = toast;
  F.esc = esc;
  F.fmtRel = fmtRel;
  F.api = api;
  F.apiUrl = apiUrl;
  F.post = post;
})();
