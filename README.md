# Pj18 · LaTeX 论文预览

仿 OpenAI Prism 的个性化 LaTeX 论文预览网页（纯黑白极简）。打开书稿即看 PDF，点 PDF 跳源码，
改一章只编这一章——**并行编译把整本几十秒压到 1~6 秒**。

当前版本 **v0.5.0**（分支 `v2`）。

---

## 亮点：按章并行 + 缓存 + PDF 拼接

书稿只保存**一个 `main.tex`**（导言区 + 全部章节正文），不需要手工拆文件。编译时自动：

1. **内部切章**：按正文顶层 `\chapter` / `\chapter*` 在内存里切块，写到 `.build/floria/chunks/`——
   只是编译中间物，源结构不动。切点选 `\chapter` 而非 `\section`，因为章必换页，独立编译的版面与书里完全一致。
2. **缓存判据**：每块以 `md5(导言区 + 块正文)` 判定，未变的块直接复用上次的 `chapter.pdf`。
3. **并行重编**：只把改动块丢进线程池，每块直接驱动 `xelatex -no-pdf -synctex=1` 循环到 aux/out/toc
   稳定，再 `xdvipdfmx` 出 PDF（不经过 latexmk，每章省 ~3.5s）。
4. **拼接还原**：前置页 + 各章 PDF + 按 openright 规则补空白页 → 原子替换 `main.pdf`；
   同时重建**书签树 `/Outlines`** 与**命名目标 `/Names/Dests`**（否则点印刷目录不跳页），
   并把各块 synctex 合并成书稿根 `main.synctex.gz`（页号偏移、行号平移回 `main.tex`）。

冷启动（还没有分块缓存）时章起始页从 PDF 顶层书签反解，先全量并行编一遍，之后就是增量。

**实测（18 核）**：

| 书稿 | 页数 | 冷启动（全部块并行） | 无事可做 | 改一章 |
|---|---|---|---|---|
| MATH | 147 | 11.3 s | **1.1 s** | ~5.7 s |
| PHYSIC | 89 | 22.8 s | **0.5 s** | **5.6 s** |
| CHEMISTERY | 55 | 35.8 s | **0.4 s** | 4.0 s |

对比整本 `latexmk` 增量：MATH 16.6 s、PHYSIC 29.9 s、CHEMISTERY 36 s。

> 任何前提不满足或中途失败都会自动回落到整本 `latexmk`，行为与旧版一致；
> 回落原因写在编译日志末尾 `[编译链] 未走快速路径：…`。快速路径需要 `pypdf`
> （`pip install pypdf`，装进**实际拉起 server.py 的那个 Python**）。

---

## 功能

- **真 PDF 预览**：pdf.js 自绘，竖排连续翻页、适宽/适页/50–300% 缩放，翻页/缩放记忆。
- **文章目录抽屉**：读 PDF 书签树，点条目跳页；PDF 印刷目录里的红框内链同样可点。
- **点 PDF 跳源码**：SyncTeX 逆搜索（坐标单位/偏移已校正），向宿主发 `src-open {path, line}`；
  独立运行时在卡内弹行号提示。
- **编译按钮五态**：编译 / 编译中 / 编译 / 重新编译 / 重试编译；失败自动摊开日志。
- **编译日志卡**：独立的第二张卡片，按时间戳定位那次失败，可返回 PDF 卡。
- **多卡片架构**：`.claude/preview/<cardId>/card.json` 描述标题与顺序，宿主顶栏渲染 tab；
  卡片通过 `floria-pv-*` / `floria-wk-card-*` postMessage 与宿主通信。
- **宿主驱动打开**：宿主广播 `file-open{path}` → 卡片由目录段推出书稿名并切换，无目录页。
- **Windows 便携**：随卡片携带 MiKTeX Portable + Perl，开箱即用、离线可编。

## 运行

```bash
# 独立运行（默认 8765）
python -B server.py --root <项目根>

# 或双击
启动.bat
```

Floria 网关模式：`preview.json` 声明 backend，网关懒拉起 `server.py` 并以
`/backend/<label>/pdf/index.html` 反代；卡片内 API 一律经 `util.js:apiUrl()` 折算到 `<base>/api/`。

第一访问根路径会 302 到 `pdf/index.html`。

## 目录

```
pdf/            PDF 卡（查看器 / 工具栏 / 桥 / 编译五态 / 点 PDF 跳源码）
log/            编译日志卡（自包含）
pdf/lib/pdfjs/  pdf.js 3.11.174 legacy UMD（离线相对路径，卡片自包含）
server.py       本地服务器：静态托管 + /api/books|read|write|compile|pdf|sync|newbook|history
fastbuild.py    自动分章并行编译（缓存 / 拼接 / 书签与命名目标 / synctex 合并）
preview.json    网关 backend 声明
tex/            自带 MiKTeX Portable 与 Perl（GB 级，未入版本控制）
```

书稿发现：递归扫描 `<root>` 下任意深度「含 `.tex` 的目录」，一目录 = 一篇（优先 `main.tex`）；
跳过隐藏目录、`node_modules`、虚拟环境与 `YYYYMMDDHHMMSS-*` 临时目录。

## 依赖

- Windows（便携编译链为 Windows 版；前端本身纯静态）
- Python 3.10+，仅用于 `server.py`；快速编译另需 `pypdf`
- 无需 Node / 构建步骤；前端为 classic script + 全局命名空间 `window.Floria`

## 许可

未指定开源许可证，保留所有权利。

---

前身：`bruce2431/latexeditor-plugin`（TypeScript 旧版 ⇒ `main` 分支；v1 留档 ⇒ `preview` 分支）。
本应用自 v2 起为纯静态重写。
