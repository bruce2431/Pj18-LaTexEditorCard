/* 工具栏注册表（2026-10-07）：编辑页顶浮条工具 tab（#tool-tabs）的登记处。
   契约逐条对齐 Floria 的 src/gateway/web-src/sidebar/work-tools.js（其 registerWkTool）：
   任何模块可调 registerTool({ id, title, pane, mount }) 挂一枚工具 tab —— 本表只做
   「注册 → 按序渲 tab → 交调用方切内容」，不写任何工具内容。
     PDF 卡当前不注册任何工具：完整编译日志与历史只在独立的 log/ 卡。
     注册表保留，供将来（或宿主）按需挂工具。
   契约：tab = <button class="v-tool-tab" data-tool="<id>">（点击委托挂在 viewer.js 的 #tool-tabs
   容器上，本表重渲不清事件）；active 态与 pane 互斥显隐由 viewer.js applyToolTab 唯一落。
   pane = 该工具内容所在元素 id；未注册工具的 pane 恒隐（applyToolTab 按 active id 判定）。 */
(function () {
  'use strict';
  const F = (window.Floria = window.Floria || {});
  const $ = F.$, esc = F.esc;
  const TOOL_DEFS = [];

  function registerTool(def) {
    if (!def || typeof def.id !== 'string' || !def.id) return;
    const entry = {
      id: def.id,
      title: String(def.title || def.id),
      pane: typeof def.pane === 'string' ? def.pane : '',
      mount: typeof def.mount === 'function' ? def.mount : null,
    };
    const i = TOOL_DEFS.findIndex(t => t.id === entry.id);
    if (i >= 0) TOOL_DEFS[i] = entry;
    else TOOL_DEFS.push(entry);
    renderToolTabs();
  }

  function toolDefs() { return TOOL_DEFS; }
  function toolDef(id) { return TOOL_DEFS.find(t => t.id === id) || null; }
  // 归一化 tab id：已注册则原样，否则回落首枚（无工具时空串）
  function toolNormId(id) {
    if (toolDef(id)) return id;
    return TOOL_DEFS.length ? TOOL_DEFS[0].id : '';
  }

  // tab 条渲染（唯一口）：数据源 = TOOL_DEFS 注册序。active 态由 viewer.js applyToolTab 落。
  // 已被宿主工具栏认领（hosted）时本页不画 tab —— 工具就是「注册在工具栏」的，不落在页面里。
  function renderToolTabs() {
    const box = $('tool-tabs');
    if (box) {
      box.innerHTML = hosted ? '' : TOOL_DEFS.map(
        t => `<button type="button" class="v-tool-tab" data-tool="${esc(t.id)}">${esc(t.title)}</button>`,
      ).join('');
    }
    postTools();
  }

  // 内容挂载：某工具第一次被激活时调一次 def.mount(paneEl)（靠 dataset.mounted 去重；无 mount 即静态 pane）
  function mountTool(id) {
    const def = toolDef(id);
    if (!def || !def.mount || !def.pane) return;
    const el = $(def.pane);
    if (!el || el.dataset.mounted) return;
    el.dataset.mounted = '1';
    try { def.mount(el); } catch (e) { console.error('工具挂载失败 ' + id + ':', e); }
  }

  /* ---------------- 宿主桥：工具注册到 Floria「工具栏」面板 ----------------
     本页跑在宿主的预览帧（iframe，本机 = 127.0.0.1:<port> 直连 / 远程 = /backend/<label>/ 反代）里时，
     把工具表申报给宿主工具栏（Floria work 右栏的
     「工具栏」面板）：`{type:'floria-wk-tool-register', tools:[{id,title}]}`（**整份替换**，不送 pane）。
     宿主渲 tab 条、**保预览帧在场**（帧工具的内容归本页自管），点 tab 回 `floria-wk-tool-select{id}`
     （`''` = 回到默认/预览态）→ 本页切 pane（CustomEvent 'floria:tool-select'，viewer.js 接）。
     宿主在收到申报后会立刻回传一次当前选中，故**收到 select 即证明桥是活的** → 置 hosted：本页不再是
     tab 的落点（`#tool-tabs` 清空），tab 只长在宿主工具栏上。
     未桥接时（单开 / `file://` / 旧宿主）hosted 恒 false → 本页自渲 tab（落在工具面板卡头；
     顶栏只有三枚按钮，面板不开则 tab 不可见——非桥接下唯一的面板入口是编译失败自动摊开）。 */
  // 帧内判据唯一一份 = js/bridge.js（此前本文件自己写「parent !== window 且 pathname 以 /backend/ 开头」，
  // 而本机宿主走 http://127.0.0.1:<port>/ 直连、pathname 是 '/' ⇒ 恒判为未桥接，申报根本发不出去）。
  const EMBEDDED = F.bridge.embedded();
  let hosted = false;

  function setHosted(on) {
    if (hosted === on) return;
    hosted = on;
    renderToolTabs();                                   // tab 落点切换：宿主接管 → 本页清空
  }

  let lastPosted = '';   // 去重：宿主每次收到申报都会回传选中 → 不去重就 register/select 互踢成死循环
  function postTools() {
    if (!EMBEDDED) return;
    const tools = TOOL_DEFS.map(t => ({ id: t.id, title: t.title }));
    const payload = JSON.stringify(tools);
    if (payload === lastPosted) return;
    lastPosted = payload;
    F.bridge.send({ type: 'floria-wk-tool-register', tools: tools });
  }

  // 宿主/工具栏消息统一经 bridge 的通知面收口（不再自建 message 监听）。
  F.bridge.onNote('floria-wk-tool-select', d => {  // 宿主选了某枚工具（'' = 回默认态）→ 本页切 pane
    setHosted(true);
    window.dispatchEvent(new CustomEvent('floria:tool-select', { detail: { id: String(d.id || '') } }));
  });
  F.bridge.onNote('floria-wk-tool-host', () => setHosted(true));   // 宿主认领（旧 ack，保留兼容）
  F.bridge.onNote('floria-wk-tool-ask', () => {                    // 宿主索要工具表
    lastPosted = '';
    postTools();
  });

  F.toolbar = { registerTool, renderToolTabs, toolDef, toolDefs, toolNormId, mountTool,
                embedded: () => EMBEDDED, hosted: () => hosted };
})();
