# Pj18 · LaTeX 论文预览卡

纯黑白极简的 LaTeX 书稿预览与增量编译工具，作为 Floria 预览卡运行：打开书稿即见 PDF，
双击 PDF 跳回源码，改一章只编一章——整本几十秒的编译压到 1~6 秒。

当前版本 **v1.0.1**。编译机制与排障细节见 [`pdf/backend/README.md`](pdf/backend/README.md)。

## 特性

- **单文件书稿**：一本书只有 `main.tex`（导言区 + 全部正文），不需要手工拆章。
- **增量编译**：按块缓存，源码没变 0 秒；改一章只重编该块，其余复用上次的 PDF。
- **章内子块**：章内按「`\newpage` 紧跟标题」继续切块，改一节也不重编整章。
- **并行编译**：改动块进线程池并行跑 xelatex，并行度取核数（上限 9）。
- **忠实还原成品**：拼接时按 openright 补空白页，原位盖印页码与页眉，书签树与目录
  按各块 `\contentsline` 重建。
- **双击跳源码**：合并各块 SyncTeX，双击 PDF 任意位置跳回 `main.tex` 对应行。
- **临时标记**：顶栏高亮笔可在 PDF 上自由划线（五色可选、可一键清空），笔画按页面坐标保存、缩放不丢；只存内存，重新编译或换书即清空。
- **失败不毁稿**：合并全部成功后才原子替换 `main.pdf`，任一步失败保留上一份成品。
- **原生合并器**：Rust `floria_merge` 单次调用并发完成 PDF 盖印与 SyncTeX 合并
  （约 80 ms）；二进制缺失或执行失败时自动回退纯 Python 路径。
- **可脚本化**：完整 HTTP API，编译、读写、查询、源码定位都能脱离界面调用。

## 架构

```
浏览器（pdf.js 查看器 / 工具栏 / 编译五态）
        │ HTTP
        ▼
server.py（Python，常驻本地进程）
   ├─ fastpath        切块 · 指纹 · 并行 xelatex · 收敛判定 · 增量复用
   ├─ compiler        模式选择（快速路径 / 不可拼接结构报错）
   ├─ pdf_stamp       拼接 · 盖印 · 书签（纯 Python 回退路径）
   ├─ synctex         合并结果的源码定位
   ├─ floria_merge    Rust 原生合并器（PDF + SyncTeX，可选）
   └─ xelatex / xdvipdfmx（便携 MiKTeX）
```

Python 负责编排与可观察性，Rust 只承担最后一跳的高并发合并。

## 快速开始

### 作为 Floria 预览卡

把整个目录放到项目的 `.claude/preview/`。`preview.json` 已声明后端：

```json
{
  "backend": {
    "cmd": ["runtime/python.exe", "-B", "server.py", "--port", "{port}",
            "--root", "{projectRoot}", "--no-open", "--auto-restart"],
    "cwd": "pdf/backend",
    "readyPath": "/pdf/index.html"
  }
}
```

网关按需拉起 `server.py`，经 `/backend/<label>/pdf/index.html` 反代。编译日志卡是独立
仓库 [Pj18-LaTexLogCard](https://github.com/bruce2431/Pj18-LaTexLogCard)，放到
`.claude/preview/log/`。

### 独立运行

```bash
pdf/backend/启动.bat
```

或手动：

```bash
cd pdf/backend
runtime/python.exe -B server.py --port 8765 --root "F:/path/to/your/books" --no-open
```

浏览器打开 `http://127.0.0.1:8765/pdf/index.html`。

### 书稿布局

```
<项目根>/
├─ MATH/
│  ├─ main.tex       唯一源文件
│  ├─ image/         配图
│  └─ main.pdf       编译成品
└─ PHYSIC/
   └─ ...
```

根目录下**每个含 `.tex` 的目录**自动成为一篇书稿，书稿名为相对路径，根目录自身不算。
优先取 `main.tex`，没有则取字典序第一个 `.tex`。`.build/` 全是可再生产物，可整删。

## 编译机制

1. 按正文顶层 `\chapter` / `\chapter*` / `\part` 在内存中切块，章内再在
   「`\newpage` 紧跟 `\section` / `\subsection`」处细分子块；源文件不动，块文件写进
   `.build/floria/chunks/`。
2. 每块以「导言区 + 块正文 + 本块实际用到的计数器」计算指纹；未变的块直接复用
   `chapter.pdf`。
3. 只把改动块丢进线程池，每块驱动 `xelatex -no-pdf -synctex=1` 循环到编号收敛，
   再 `xdvipdfmx -E -z 1` 出页。
4. 前置页（封面 / 目录 / 摘要）单独一块，按整本目录快照编译；导言区或引言改动会自动
   重建，不会出现「正文是新的、目录还是旧的」。
5. 拼接各块、按 openright 补空白页、盖印页码页眉、重建书签，合并 SyncTeX，全部成功后
   原子替换成品。

`\appendix` 会改变章编号体系，不能按章拼接，快速路径直接报错，而不是静默退回整本重编。

## 性能

18 核实测（v0.8.1 基线）：

| 书稿 | 页数 | 冷启动 | 无事可做 | 改一章 |
|---|---:|---:|---:|---:|
| MATH | 147 | 32.4 s | 0.03 s | 8.1 s |
| PHYSIC | 94 | 37.7 s | 0.03 s | 5.4 s |
| CHEMISTERY | 89 | 42.9 s | 0.1 s | 6.4 s |

v0.8.4 起：块全复用只重合并 0.55 s；改一章且页码不变 4.5 s。v0.9.0 起 CHEMISTERY
33 块，重编 CH9 一节 5.2 s，冷编 39.4 s。实际耗时取决于机器、书稿与是否触发页码偏移。

## HTTP API

后端只监听 `127.0.0.1`。`book` 一律用书稿目录名。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/books` | 书稿列表与 PDF 状态 |
| GET | `/api/read?book=` | 读取主 `.tex` 与配图清单 |
| GET | `/api/pdf?book=` | 成品 PDF |
| GET | `/api/sync?book=&page=&x=&y=` | PDF 坐标 → 源码行 |
| GET | `/api/history?book=&reconcile=1` | 编译账本与当前编译快照 |
| GET | `/api/log?book=` | 最近一次编译日志 |
| GET | `/api/health?book=&refresh=1` | 编译链体检 |
| GET | `/img/<book>/<file>` | 书稿图片 |
| POST | `/api/compile` | `{"book": "MATH"}`，异步编译 |
| POST | `/api/write` | `{"book","file","content"}`，只覆盖已有 `.tex` |
| POST | `/api/newbook` | `{"name": "..."}`，新建书稿目录 |

编译是异步的：`POST /api/compile` 立即返回受理结果，随后轮询 `/api/history` 的
`compile` 快照直到 `idle`。同一时刻只允许编一本，并发请求返回 409。

## 配置

- `preview.json`：网关清单，含 `version`、`backend.cmd/cwd/port/idleMinutes/readyPath`
  与 `hotReload`。
- `FLORIA_NO_NATIVE=1`：强制回退纯 Python 合并，用于排查原生二进制问题。
- 编译链只依赖 `pdf/backend/` 下的便携运行时与 MiKTeX，不读取机器 PATH。

## 目录结构

```
preview.json              网关 backend 清单
pdf/                      前端卡（pdf.js 查看器、工具栏、编译状态、点 PDF 跳源码）
  index.html
  js/  css/  lib/pdfjs/
  backend/
    server.py             进程入口（静态托管 + HTTP API）
    floria/               编译链
      fastpath.py         切块、指纹、并行编译、收敛与增量复用
      pdf_stamp.py        拼接、盖印、书签（含纯 Python 回退）
      compiler.py         模式选择与编译状态机
      synctex.py          坐标 → 源码定位
      http_api.py         路由与 API
      books.py            书稿发现与 PDF 状态
      doctor.py           工具链体检
      ledger.py           编译账本
      locks.py            跨进程编译锁
      toolchain.py        MiKTeX 探测与命令构造
      config.py log.py watchdog.py app.py
    native/
      floria_merge/       Rust 源码（PDF 合并 + SyncTeX 合并）
      bin/floria_merge.exe 预编译二进制
    tests/                单测 + 真实 TeX 集成回归
    vendor/pypdf/         PDF 读写依赖
    runtime/              CPython 3.12 embeddable（发行包内置）
    tex/                  MiKTeX Portable + Perl（发行包内置）
    启动.bat              独立运行入口
```

## 构建与测试

Rust 合并器（可选，缺失时自动走 Python）：

```bash
cd pdf/backend/native/floria_merge
cargo build --release
cp target/release/floria_merge.exe ../bin/
```

需要 Rust 1.85+（edition 2024）。

单元与集成测试：

```bash
cd pdf/backend
python -m unittest discover -s tests -t . -v
```

真实 TeX 集成用例需要可用的 `xelatex`（内置 MiKTeX 或系统安装）。

## 版本

当前 **v1.0.0**。版本历史与变更说明见
[Releases](https://github.com/bruce2431/Pj18-LaTexEditorCard/releases)。

## 许可

未指定开源许可证，保留所有权利。
