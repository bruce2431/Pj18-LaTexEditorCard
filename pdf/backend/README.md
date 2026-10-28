# Pj18 · LaTeX 论文预览

仿 OpenAI Prism 的个性化 LaTeX 论文预览网页（纯黑白极简）。打开书稿即看 PDF，点 PDF 跳源码，
改一章只编这一章——**并行编译把整本几十秒压到 1~6 秒**。

当前版本 **v0.8.4**（分支 `main`）。

---

## 亮点：按章并行 + 缓存 + PDF 拼接（唯一编译路径）

书稿只保存**一个 `main.tex`**（导言区 + 全部章节正文），不需要手工拆文件。编译时自动：

1. **内部切块**：按正文顶层 `\chapter` / `\chapter*` / `\part` 在内存里切块，写到 `.build/floria/chunks/`——
   只是编译中间物，源结构不动。切点选章/分卷而非 `\section`，因为章必换页，独立编译的版面与书里完全一致。
   **单章、无章书稿整篇正文当一块**；`\appendix` 不能按章拼接 → 直接报错。
2. **缓存判据**：每块以 `md5(导言区 + 块正文)` 判定，未变的块直接复用上次的 `chapter.pdf`。
3. **并行重编**：只把改动块丢进线程池，每块直接驱动 `xelatex -no-pdf -synctex=1` 循环到 aux/out/toc
   稳定，再 `xdvipdfmx -E -z 6` 出 PDF（**不经过 latexmk**）。wrapper 在 `\input` 前用
   `\@starttoc{toc}` 触发一次目录写出，一遍就拿到带绝对页码的 `wrapper.toc`。
   **`-z 6` 是硬要求**：xdvipdfmx 默认 zlib 级别 9，对中文字体嵌入流要压 21 s（CHEMISTERY 单块
   21.5 s → 2.2 s，产物只大 5%），2026-10-27 之前这是冷编 90 s 的主因。
4. **拼接还原**：前置页 + 各块 PDF + 按 openright 规则补空白页 → 原子替换 `main.pdf`；
   书签树 `/Outlines` **按各块 `\contentsline` 合成**（part→chapter→section→subsection 嵌套，
   不再反解旧 PDF 的书签），并把各块 synctex 合并成书稿根 `main.synctex.gz`（页号偏移、行号平移回 `main.tex`）。
5. **前置页自己编**：`preamble + 第一章之前正文`（封面 / 目录 / 摘要）单独一块，目录 seed 是刚合成的
   总目录；`\@starttoc` 先读后截断，因此每一遍编译前都要重新写回 seed。改导言区/引言 → 前置页
   自然重编，不会出现「正文是新的、封面目录还是旧的」。前置页缓存指纹 =
   `md5(导言区 + 前置正文 + 种进去的整本目录)`，目录没变就一页都不重编；aux/out/toc 两遍稳定即退出
   （旧写法对前置页恒跑满 4 遍）。

冷启动（没有分块缓存、甚至没有 `main.pdf`）直接全量并行编一遍，不需要先整本编一次。
**分块缓存丢了但成品还在时**（`.build/floria/` 被删、换机器拷贝书稿），先用书稿根的
`main.synctex.gz` 反推各章起始页当初值（每页记录条数取「够多」的第一页，滤掉目录页上指向该章的
零星链接；偶数页按 openright 回退到前一页），没有 synctex 再用 `main.pdf` 书签页；估错只会多跑
一轮，不会编错。CHEMISTERY 实测：无 seed 42.9 s → 有 seed 24.5 s。

「不白跑」的保证：

- **源码没变 → 0 秒**：`.build/floria/last_ok.json` 记录上次成功编译的源码指纹，命中直接复用成品。
- **改一章只编一块**：其余块复用 `.build/floria/<chunk>/chapter.pdf`；只有页码被推后的块才重编
  （新增一章时后续块页码不变就不用重编）。
- 并行度取核数（上限 9）：CHEMISTERY 10 块排 2 波，长尾由最重的那块决定。

**实测（18 核，2026-10-27 v0.8.1）**：

| 书稿 | 页数 | 冷启动（无缓存无成品，2 轮收敛） | 重建缓存（保留 main.pdf/synctex，seed） | 无事可做 | 改一章 |
|---|---|---|---|---|---|
| MATH | 147 | 32.4 s | — | 0.03 s | 8.1 s |
| PHYSIC | 94 | 37.7 s | — | 0.03 s | 5.4 s |
| CHEMISTERY | 89 | 42.9 s | **24.5 s** | 0.1 s | 6.4 s |

> 冷启动要跑两轮：第一轮各块页码是猜的，`layout()` 定了真实起始页后第二轮才收敛。
> 改一章的耗时 = 该块两遍 xelatex + 整本 PDF 拼接 + synctex 合并（后两项合计 1~3 s，与书稿大小相关）。

**实测（v0.8.4 提速后，PHYSIC 3 块 / 98 页）**：源码没变 0 s；块全复用只重合并 0.55 s；
改一章正文、页码不变 **4.5 s**（原 14 s，1 遍 xelatex）；改一章导致后续章页码整体偏移 **9.9 s**（原 15.8 s）。

> **v0.8.4（2026-10-28）编译提速：改一章从 14 s 降到 4.5 s**。产出与「跑满 4 遍」逐字节一致
> （同一书稿 PDF md5 相同、synctex 解压后逐字节相同；单元 23 条 + 真实 TeX 集成 10 条全绿）：
> ① 收敛判据从「整份 aux/out/toc 稳定」细化成「**本块 `\ref` 出去的编号没变**」——`\ref` 印的是
> 编号不是页码，起始页平移不影响它；块里有 `\pageref`/`\vref`/`\cpageref`/`\nameref` 时自动退回
> 老判据。改一章正文从此 1 遍 xelatex 收工（原 2–3 遍，每遍 2.3–3.2 s）。另三点：稳定性签名
> 忽略 `\gdef \@abspage@last`（书稿用 `LastPage`/`lastpage` 时不忽略）；只是被前面章页码推后的块
> `single_pass` 一遍即停；前置页在一次布局迭代内按目录文本记忆，目录没变不重编。
> ② synctex 合并缓存（`.build/floria/synctex_merged.gz` + `.key`，键 = 各块 synctex md5 + 页偏移 +
> 块名 + 行平移）：输入没变直接拷成品，0.35 s → 0.03 s；合并写盘 gzip 级别 9→6（更快且产物略小）。
> ③ `xdvipdfmx -z 1`：比 `-z 6` 每块省约 0.45 s，产物大 17%（54 页章 3.5 MB → 4.1 MB）。
> 再往下已到头：只加载导言区、一行正文不排就要 1.3 s，改一章的理论下限约 3.2 s。

> **v0.8.4（2026-10-28）日志卡点报错行跳源码**：`log/` 卡把 `块文件:行号:` 与紧跟的
> `l.行号` 渲染成可点片段，点击发 `floria-pv-event {ev:'src-open', path, line}` 由宿主打开
> `main.tex` 并滚到行。块文件的行号是块内行号，`/api/log` 新增 `chunks:[{name,line}]`
> 起点表供前端平移回 `main.tex`；`wrapper.tex`（导言区）行号与 `main.tex` 一一对应。
> 同时修掉一个老问题：快速路径失败时错误输出只进 `log_cb`、而 compiler 从不传它，
> 失败日志里只有一句「块编译失败：chN」；现在 `try_compile` 自己收集过程日志并随失败
> 结果返回，`.build/main.log` 与账本里才有真实报错（各块尾部留 4000 字符）。

> **v0.8.3（2026-10-28）编译计时器 + 尾部提速**：编译中按钮文案改为「编译中 3.4 s」，
> 每 0.1 s 刷新（`compile.js` 进入 busy 态起表，取 `Date.now()` 差值，iPad 挂起丢请求后
> 自愈接管也不会倒退）；按钮定宽 104 px + 等宽数字，秒数进位不挤动顶栏。编译尾部把
> `_merge_pdfs` 返回的页数直接当 `total_pages`，省掉再开一次 pypdf 读整本成品，
> PHYSIC 改一章 4.62 s → 4.34 s（1 遍路径稳定在 4.3~5.5 s；页码变化走 2 遍时 7~9 s）。

> **v0.8.2（2026-10-28）article 类书稿**：`chapter` / `part` 计数器缺失时不再注入
> `\setcounter`，MARXISM（ctexart，无 chapter）可正常编译。

> **v0.8.1（2026-10-27）编译提速**：删掉整本兜底后发现冷编仍要 90 s，根因是 `xdvipdfmx`
> 默认 zlib 级别 9 压 CJK 字体流（单块 21.5 s）；改 `-z 6` 后 2.2 s。另把前置页改为两遍即停
> 并按 `_front_hash` 缓存、冷缓存用已有 synctex/书签反推起始页，CHEMISTERY 冷编 90.5 s →
> 24.5 s（重建缓存）/ 42.9 s（全新书稿），改一章 6.4 s。

> **v0.8.0（2026-10-27）删除整本 latexmk 兜底**：CHEMISTERY 新增一章后旧实现因
> 「书签数对不上」隐式整本重编，实测 88.7 s。现在只有快速路径，结构化结果
> `{status, reason_code}` 只有 `ok` / `failed` 两种：`\appendix`、缺 `pypdf`、
> 块编译失败、拼接失败、synctex 合并失败、页码 4 轮未收敛 → **直接报错**，原因码写进账本与日志，
> 不会再偷偷换一种编法。快速路径需要 `pypdf`（`vendor/` 已随卡片携带）。
> 块 synctex 损坏（gzip 完整性校验不过）会被判为「需重编」；合并失败时 PDF 与 synctex
> 都不会被替换，成品保持上一次的完整状态。

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
│     ├─ floria/         分层实现（v0.8.0）：
│     │     config/log/toolchain/books/ledger/synctex/locks  基础设施
│     │     fastpath      自动分章并行编译（缓存 / 拼接 / 书签 / synctex 合并）
│     │     compiler      编译服务（跨进程锁 → 源码指纹短路 → 快速路径 → 账本）
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
│  ├─ main.synctex.gz    点 PDF 跳源码所需（由各块 synctex 合并到书稿根）
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
  按章并行编译依赖 `vendor/pypdf`，缺失直接报 `missing_pypdf`（没有其它编译路径）
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
