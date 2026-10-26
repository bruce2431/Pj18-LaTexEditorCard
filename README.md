# Pj18 · LaTeX 论文预览卡

仿 OpenAI Prism 的个性化 LaTeX 论文预览网页（纯黑白极简）：打开书稿即看 PDF，点 PDF 跳源码，
改一章只编这一章——**按章并行编译把整本几十秒压到 1~6 秒**。

当前版本 **v0.7.0**。详细机制、目录规范与排障见 [`pdf/backend/README.md`](pdf/backend/README.md)。

## 两种用法

1. **作为 Floria 预览卡**：把整个目录放到项目的 `.claude/preview/`，`preview.json` 的 `backend`
   段声明了 `cwd: "pdf/backend"`，网关会按需拉起 `server.py`，随后以 `/backend/<label>/pdf/index.html` 反代。
2. **独立运行**：双击 `pdf/backend/启动.bat` → 浏览器打开
   `http://127.0.0.1:8765/pdf/index.html`（日志卡：`/log/index.html`）。

## 发行包

Release 附件 **解压即用**，已内置：

- `pdf/backend/runtime/` CPython 3.12.10 embeddable（不需要机器上装 Python）
- `pdf/backend/vendor/` pypdf 6.21.2（快速并行编译的 PDF 拼接依赖）
- `pdf/backend/tex/` MiKTeX Portable 26.5 + Perl（离线可编，已关闭自动装包）

解压后把整个目录放到 `<你的项目>/.claude/preview/`，项目根下每个含 `.tex` 的目录自动成为一篇书稿。

## 目录结构

```
preview.json            网关 backend 清单（唯一入口配置）
pdf/                    PDF 卡（pdf.js 查看器 / 工具栏 / 编译五态 / 点 PDF 跳源码）
  backend/              编译进程与 HTTP API 归属 PDF 卡
    server.py           本地服务器：静态托管 + /api/books|read|write|compile|pdf|sync|newbook|history|log
    floria/             编译链（fastpath / compiler / ledger / synctex / locks / toolchain）
    tests/              模式选择单测 + 真实 TeX 集成回归
    vendor/pypdf/       纯 Python 依赖
    runtime/            CPython 3.12 embeddable（发行包内置）
    tex/                MiKTeX Portable + Perl（发行包内置）
    启动.bat            独立运行入口
log/                    编译日志卡（读 .build/main.log 原文 + 账本时间线；全项目唯一日志实现）
```

## v0.7.0 变更

- **编译模式结构化**：快速路径返回 `ok / unsupported / failed` 三态与稳定原因码；
  结构不支持（单章 / `\appendix` / `\part`）明确选择整本模式，真实故障不再被静默吞成整本重编。
- **前置页新鲜度**：封面/目录基线旁车记录 `md5(导言区 + 第一章之前正文)`，改导言区或引言后
  自动整本重建前置页，避免出现「正文是新的、目录还是旧的」半新 PDF。
- **原子提交**：PDF 与 synctex 都合并成功后才一起落盘，任一失败不替换成品。
- **前端职责拆分**：`viewer.js` 拆为 `state / compile / toc` 等模块；PDF 卡不再自带日志面板，
  日志与编译历史只由 `log/` 卡一张维护；宿主消息统一走 `bridge.js`。
- **测试**：新增模式选择单测与真实 TeX 集成回归（单章 / 双章 / appendix / part / 快速失败 /
  前置页改动 / SyncTeX 合并失败）。

## 历史变更（v0.5.4）

- 依赖内聚：CPython embeddable 与 pypdf 随后端携带，不再依赖机器 Python。
- 修复并行编译中多个 `xelatex` 争用 MiKTeX 格式文件（`Permission denied: ...xelatex.fmt`）。
- 修复 MiKTeX 包数据库被写空导致每次编译重建格式的问题。

## 许可

未指定开源许可证，保留所有权利。
