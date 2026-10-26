/* 宿主桥 = 卡片 API（2026-10-11）：本页跑在 Floria 的预览帧（iframe）里时，与宿主之间只走 postMessage，
   两条通道共用一个命名空间 floria-pv-*：
     · 宿主 → 卡（命令面）：{type:'floria-pv-cmd', cmd, ...}   —— 宿主驱动本页（打开书稿 / 翻页 / 编译 / 缩放 / 要状态）
     · 宿主 → 卡（通知面）：{type:'floria-pv-event', ev, ...} / {type:'floria-pv-broadcast', ev, ...}
        —— 无配对的消息，不回 ack。event = 针对本卡；broadcast = 宿主对所有在场卡的广播
        （如「用户打开了一篇 .tex」→ 预览卡据此切换到该篇）
     · 卡 → 宿主（事件面）：{type:'floria-pv-event', ev, ...}  —— 本页汇报（就绪 / 状态 / 「请打开源文件第 N 行」）
   本表**只做收发 + 按 cmd 分派到登记的处理函数**，不写任何业务（处理函数由 viewer.js 注册，同 js/toolbar.js 的思路：
   表不管内容，谁的功能谁登记）。宿主侧门禁 = e.source 必须就是当前那枚 .preview-frame 的 contentWindow；
   本页门禁 = 只认 e.source === window.parent。两侧各自把门，本页不猜宿主是谁。
   帧内判据 = **身处 iframe**（window.parent !== window）：不能用 '/backend/' 前缀判——本机宿主走
   http://127.0.0.1:<port>/ 直连（pathname 为 '/'），带 /backend/ 前缀的只有远程端那一支反代。 */
(function () {
  'use strict';
  const F = (window.Floria = window.Floria || {});
  const EMBEDDED = window.parent !== window;
  const HANDLERS = new Map();   // cmd -> fn(payload)，同 cmd 覆盖

  function send(msg) {
    if (!EMBEDDED) return false;
    try { parent.postMessage(msg, '*'); return true; } catch (e) { return false; }   // 帧被拆：动作无声丢弃
  }

  // 卡 → 宿主 事件面（唯一出口）。data 的同名键不覆盖 type/ev，免得把信封撞坏。
  function emit(ev, data) {
    const m = { type: 'floria-pv-event', ev: String(ev || '') };
    if (data && typeof data === 'object') for (const k in data) if (!(k in m)) m[k] = data[k];
    return send(m);
  }

  // 宿主 → 卡 命令面的登记处（注册序无关，按 cmd 取用）
  function onCmd(cmd, fn) {
    if (typeof cmd === 'string' && cmd && typeof fn === 'function') HANDLERS.set(cmd, fn);
  }

  // 卡 → 宿主：请宿主把预览区切到另一张卡（多卡片架构，2026-10-18）。
  //   {type:'floria-wk-card-switch', card:'log', payload:{...}}
  // 宿主校验 e.source 与卡表后换 iframe src；目标卡加载后经 floria-wk-card-init 收到 payload。
  function switchCard(card, payload) {
    const m = { type: 'floria-wk-card-switch', card: String(card || '') };
    if (payload !== undefined) m.payload = payload;
    return send(m);
  }

  // 宿主 → 卡 的「通知」面（区别于命令：不配对、不回 ack，只是事实广播）。
  //   · floria-pv-event   —— 宿主对我（本卡）说的、无配对的消息
  //   · floria-pv-broadcast —— 宿主对**所有在场卡**说的广播（如「用户打开了某文件」）
  //   · floria-wk-*       —— 多卡片/工具栏消息（card-init / tool-select / tool-ask…）
  // 谁需要谁登记（同 onCmd 的思路，本表不管内容）。门禁同上：只认 e.source === parent。
  const NOTES = new Map();     // type -> Set<fn>
  function onNote(type, fn) {
    if (typeof type !== 'string' || !type || typeof fn !== 'function') return;
    if (!NOTES.has(type)) NOTES.set(type, new Set());
    NOTES.get(type).add(fn);
  }
  function dispatchNote(type, d) {
    const set = NOTES.get(type);
    if (!set) return;
    set.forEach(fn => { try { fn(d); } catch (e) { /* 单个监听者出错不拖累其余 */ } });
  }

  window.addEventListener('message', e => {
    if (!EMBEDDED || e.source !== window.parent) return;
    const d = e.data;
    if (!d || typeof d !== 'object') return;
    // 通知面：按 d.type 派给登记者（未登记的 no-op）。宿主/工具栏消息一律走这里收口，
    // 卡内模块不再各自 addEventListener('message')。
    dispatchNote(String(d.type || ''), d);
    if (d.type !== 'floria-pv-cmd') return;
    const cmd = String(d.cmd || '');
    const h = HANDLERS.get(cmd);
    if (!h) { emit('ack', { cmd: cmd, ok: false, error: '未注册的命令 ' + cmd }); return; }
    // 处理函数一律异步收口：同步抛错也要落成一条 ack —— 宿主至少要拿得到失败原因（别「发了没反应」）
    Promise.resolve().then(() => h(d)).then(
      r => emit('ack', { cmd: cmd, ok: true, result: r === undefined ? null : r }),
      err => emit('ack', { cmd: cmd, ok: false, error: String((err && err.message) || err) }),
    );
  });

  F.bridge = { embedded: () => EMBEDDED, send, emit, onCmd, onNote, switchCard };
})();
