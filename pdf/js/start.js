/* Floria LaTeX v2 — 启动序 + hash 路由（单界面：本页就是一张 PDF 预览卡）
   打开哪篇有两个来源：
     1. 宿主的通知/命令（iframe 里）—— floria-pv-broadcast{ev:'file-open',path} 或 floria-pv-cmd{cmd:'open'}
     2. hash `#/p/<书稿名>`（单开/直链时的退路）
   都没有 → 空态；未桥接时再自动预载一篇（见 viewer.preload）。
   必须最后加载：此时 util / bridge / toolbar / viewer 都已就位。 */
(function () {
  'use strict';
  const F = window.Floria;

  function route() {
    const m = /^#\/p\/(.+)$/.exec(location.hash || '');
    if (m) F.viewer.open(decodeURIComponent(m[1]));
    else F.viewer.close();
  }

  F.viewer.init();

  window.addEventListener('hashchange', route);
  route();
  F.viewer.preload();     // 未桥接：挑一篇顶上（桥接时是 no-op，等宿主的广播/命令）
})();
