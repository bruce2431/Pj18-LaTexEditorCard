/* Floria LaTeX v2 — PDF 卡唯一共享态（2026-10-26 拆分）
   viewer（PDF/打开关闭）、compile（编译态）、toc（目录）共用这一份；
   模块之间不互相持有私有状态，一律读写 F.state。
   编译历史/版本面板已移出 PDF 卡（只由 log/ 卡实现）。 */
(function () {
  'use strict';
  const F = (window.Floria = window.Floria || {});
  F.state = {
    book: '',            // 当前书稿名（= /api/books 里的 name）
    texName: '',         // 主 .tex 文件名（/api/books 的 texName；切日志卡时拼 path 用）
    pdfMtime: 0,
    busy: false,
    tool: '',            // 当前展开的工具 id
    lastTool: '',        // 收起后记住上次工具
    state: 'none',       // 编译五态
  };
})();
