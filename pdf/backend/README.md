# Pj18 · LaTeX 论文预览

仿 OpenAI Prism 的个性化 LaTeX 论文预览网页（纯黑白极简）。打开书稿即看 PDF，点 PDF 跳源码，
改一章只编这一章——**并行编译把整本几十秒压到 1~6 秒**。

当前版本 **v0.6.0**（分支 `main`）。

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

前置页（封面/目录）从 `main.pdf` 原样搬，因此必须证明它们对应当前源码：基线旁车
`.build/floria/front.meta.json` 记录 `md5(导言区 + 第一章之前正文)`，缺失或不匹配时
快速路径直接让位给整本编译一次，重建前置页后再写回基线——否则改了导言区/引言，
快速编译会给出「正文是新的、封面目录还是旧的」的半新 PDF。

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
> 块 synctex 损坏（gzip 完整性校验不过）会被判为「需重编」，
> 合并失败则删掉 `main.synctex.gz` 并回落整本编译，不再出现「编译成功但点 PDF 无反应」。

---

## 功能

- **真 PDF 预览**：pdf.js 自绘，竖排连续翻页、适宽/适页/50–300% 缩放，翻页/缩放记忆。
- **文章目录抽屉**：读 PDF 书签树，点条目跳页；PDF 印刷目录里的红框内链同样可点。
- **点 PDF 跳源码**：SyncTeX 逆搜索（坐标单位/偏移已校正），向宿主发 `src-open {path, line}`；
  独立运行时在卡内弹行号提示。
- **编译按钮五态**：编译 / 编译中 / 编译 / 重新编译 / 重试编译；失败自动摊开日志。
- **编译日志卡**：独立的第二张卡片，按时间戳定位那次失败，可返回 PDF 卡。每条记录标注来源
  （页 / AI / 回溯）；「回溯」= 这次编译没经过账本（手工 latexmk、旧版本等），由 PDF 与 `.build/main.log`
  的 mtime 补一条，只能给出时间、成败、页数，耗时显示「耗时未知」。编译进行中不生成回溯记录。
- **多卡片架构**：`.claude/preview/<cardId>/card.json` 描述标题与顺序，宿主顶栏渲染 tab；
  卡片通过 `floria-pv-*` / `floria-wk-card-*` postMessage 与宿主通信。
- **宿主驱动打开**：宿主广播 `file-open{path}` → 卡片由目录段推出书稿名并切换，无目录页。
- **Windows 便携**：随卡片携带 MiKTeX Portable + Perl，开箱即用、离线可编。

## 目录结构规范

### 预览根 `.claude/preview/`

**只放两样东西**：清单 `preview.json` 与卡片目录。编译进程归属 PDF 卡，后端资产随卡片收在
`pdf/backend/` 里；日志卡只读日志与账本，不共享 PDF 卡的前端代码。

```
.claude/preview/
├─ preview.json          网关 backend 清单（必须留在预览根，唯一入口配置）
├─ pdf/                  PDF 卡（查看器 / 编译五态 / 点 PDF 跳源码）
│  ├─ card.json          卡片元信息（title 必填，order 排序，default 至多一张）
│  ├─ index.html
│  ├─ lib/pdfjs/         pdf.js 3.11.174 legacy UMD（离线相对路径，卡片自包含）
│  └─ backend/           编译后端与工具链（**不是静态资源，对外一律 404**）
│     ├─ server.py       入口：sys.path 修补（便携 runtime 的 _pth 不含脚本目录）+ vendor 注入
│     ├─ floria/         分层实现（v0.7.0）：
│     │     config/log/toolchain/books/ledger/synctex/locks  基础设施
│     │     fastpath      自动分章并行编译（缓存 / 拼接 / 书签 / synctex 合并）
│     │     compiler      编译服务（跨进程锁 → 快速路径 → 整本模式 → 账本 → synctex 基线）
│     │     doctor        自检（/api/health）：导入 / 工具链 / MiKTeX 包库与格式 / 缓存 / 前置页映射 / 源码指纹
│     │     http_api      Handler + 全部 endpoint + 统一异常边界
│     │     watchdog      --auto-restart：源码变更且空闲时自退，由网关重拉
│     │     app           进程装配
│     ├─ tests/          unittest + 真实 TeX 集成回归
│     ├─ fastbuild.py     兼容 shim（属性转发到 floria.fastpath）
│     ├─ runtime/         自带 CPython 3.12 便携运行时（python.org embeddable，未入版本控制）
│     ├─ vendor/          纯 Python 依赖（pypdf 6.x），由 server.py 插进 sys.path
│     ├─ tex/             自带 MiKTeX Portable 与 Perl（GB 级，未入版本控制）
│     ├─ README.md
│     └─ 启动.bat         独立运行入口
└─ log/                  编译日志卡（读 .build/main.log 原文 + 账本历史，自包含）
```

**卡片契约**：一卡一目录，目录内自带 `card.json` 与全部资源，禁跨卡 `../` 引用；
跨卡跳转只走宿主消息。静态托管**只放行带 `card.json` 的卡片目录**——
`backend/`、`runtime/`、`vendor/`、`tex/`、隐藏目录、无 `card.json` 的目录一律 404
（`server.py` 静态处理器按此白名单校验）。

### 项目根与书稿目录

书稿发现 = 递归扫描项目根下任意深度「含 `.tex` 的目录」，**一目录 = 一篇**。
根目录自身散落的 `.tex` 不算书稿（工作区规则本就禁止根目录散文件）。

```
<项目根>/
├─ <书名>/               一个目录一篇书稿（MATH / PHYSIC / …）
│  ├─ main.tex           主文件（优先；没有则取目录内字典序第一个 .tex）
│  ├─ main.pdf           唯一成品落点（PDF 卡显示的就是这一份）
│  ├─ main.synctex.gz    点 PDF 跳源码所需（latexmk 与 PDF 一同输出到书稿根）
│  ├─ image/             配图素材（正文可用 \graphicspath{{image/}} 后写裸文件名）
│  ├─ assets/            非 LaTeX 源素材（MATLAB 脚本 / 建模文件 / 原始文档等）
│  ├─ backup/            历史 .tex 备份（main.tex.bak*），不参与编译
│  └─ .build/            中间产物（aux/log/out/toc/xdv/floria 分块缓存），可整个删掉重建
└─ YYYYMMDDHHMMSS-名称/  临时任务书稿：**同样按书稿发现**（v0.5.2 起兼容）
```

**书稿目录命名**：

- 常规书稿用语义名（`MATH`、`PHYSIC`）。
- 临时任务目录 `YYYYMMDDHHMMSS-名称` **不再被跳过**，会正常出现在 `/api/books` 与卡片里，
  `POST /api/newbook` 也接受这种名字。想让它彻底不出现在书库里，请把目录移出项目根（`.trash/`）。
- 隐藏目录（`.` 开头）、`node_modules`、`__pycache__`、`venv`/`.venv`、`site-packages` 仍跳过。

**书稿名口径**：`/api/books` 返回的 `name` 是相对项目根的 POSIX 路径（可含 `/`，
如 `papers/2026-xxx`），卡片直接显示；外部传入的名字不在发现白名单即 404（防目录穿越）。

## 运行

```bash
# 在 pdf/backend/ 内独立运行（默认 8765）：
runtime/python.exe -B server.py --root <项目根>

# 或在 pdf/backend/ 里双击
启动.bat
```

`runtime/python.exe` 是随后端携带的 CPython 3.12 embeddable，`vendor/pypdf` 也一并携带——
后端因此不依赖机器上的 Python，也不依赖工作区 `.claude/runtime/`。启动.bat 在 runtime 缺失时才回落系统 Python。

Floria 网关模式：预览根的 `preview.json` 声明 backend（`cwd: "pdf/backend"`），网关在此目录懒拉起
`server.py`，随后以 `/backend/<label>/pdf/index.html` 反代。卡片内 API 一律经
`util.js:apiUrl()` 折算到 `<base>/api/`。
注意：网关把子进程日志固定写到**预览根** `backend.log`（宿主行为，不可配置），已在 `.gitignore` 排除。

第一访问根路径会 302 到 `pdf/index.html`。

## 依赖

- Windows（便携编译链为 Windows 版；前端本身纯静态）
- `runtime/`（CPython 3.12 embeddable）与 `vendor/`（pypdf）**已随后端携带**，不需要安装 Python；
  快速编译的按章并行依赖 `vendor/pypdf`，缺失时才回落整本 latexmk
- 无需 Node / 构建步骤；前端为 classic script + 全局命名空间 `window.Floria`
- 便携 MiKTeX 已关闭自动装包（`tex/texmfs/config/miktex/config/miktex.ini` 里 `AutoInstall=0`）：
  缺包会立刻报 `File ... not found`，不会卡在网络重试；补包时从 CTAN 下载 zip，
  解压到 `tex/texmfs/install/tex/latex/<包名>/` 即可（`kpsewhich` 能查到就生效）。
- **编译突然每次多出十几秒 → 先查 MiKTeX 包数据库是否被写成 0 字节**：
  `texmfs/install/miktex/config/package-manifests.ini`、`packages.ini` 与
  `texmfs/data/miktex/cache/packages/*/mpm.ini` 若变成空文件（断电/磁盘异常），MiKTeX 每次
  启动都会重做 fndb + fontmaps + 重建 `xelatex.fmt`，单次 `xelatex` 从 0.7 s 涨到 20 s。
  修复（需能联网）：`miktex --disable-installer packages update-package-database`；
  `miktex.ini` 的 `RemoteRepository` 可改国内镜像（清华源当前 403，USTC 实测可用）。

## 许可

未指定开源许可证，保留所有权利。

---

前身：`bruce2431/latexeditor-plugin`（TypeScript 旧版 ⇒ `main` 分支；v1 留档 ⇒ `preview` 分支）。
本应用自 v2 起为纯静态重写。
