# Pj18 · LaTeX 论文预览卡

仿 OpenAI Prism 的个性化 LaTeX 论文预览网页（纯黑白极简）：打开书稿即看 PDF，点 PDF 跳源码，
改一章只编这一章——**按章并行编译把整本几十秒压到 1~6 秒**。

当前版本 **v0.5.4**。详细机制、目录规范与排障见 [`_backend/README.md`](_backend/README.md)。

## 两种用法

1. **作为 Floria 预览卡**：把整个目录放到项目的 `.claude/preview/`，`preview.json` 的 `backend`
   段声明了 `cwd: "_backend"`，网关会按需拉起 `server.py`，随后以 `/backend/<label>/pdf/index.html` 反代。
2. **独立运行**：解压发行包 → 双击 `_backend/启动.bat` → 浏览器打开
   `http://127.0.0.1:8765/pdf/index.html`（日志卡：`/log/index.html`）。

## 发行包

Release 附件 `Pj18-LaTexPreview-v0.5.4-win64.zip` **解压即用**，已内置：

- `_backend/runtime/` CPython 3.12.10 embeddable（不需要机器上装 Python）
- `_backend/vendor/` pypdf 6.21.2（快速并行编译的 PDF 拼接依赖）
- `_backend/tex/` MiKTeX Portable 26.5 + Perl（离线可编，已关闭自动装包）

解压后把整个目录放到 `<你的项目>/.claude/preview/`，项目根下每个含 `.tex` 的目录自动成为一篇书稿。

## 目录结构

```
preview.json            网关 backend 清单（唯一入口配置）
pdf/                    PDF 卡（pdf.js 查看器 / 工具栏 / 编译五态 / 点 PDF 跳源码）
log/                    编译日志卡（账本时间线、来源标注）
_backend/
  server.py             本地服务器：静态托管 + /api/books|read|write|compile|pdf|sync|newbook|history
  fastbuild.py          自动分章并行编译（缓存 / 拼接 / 书签与命名目标 / synctex 合并）
  vendor/pypdf/         纯 Python 依赖
  runtime/              CPython 3.12 embeddable（发行包内置）
  tex/                  MiKTeX Portable + Perl（发行包内置）
  启动.bat              独立运行入口
```

## v0.5.4 变更

- 依赖内聚：CPython embeddable 与 pypdf 随 `_backend/` 携带，不再依赖机器 Python；
  `preview.json` 改用 `runtime/python.exe`，`启动.bat` 优先内置运行时。
- 修复并行编译中多个 `xelatex` 争用 MiKTeX 格式文件（`Permission denied: ...xelatex.fmt`）：
  检测到格式争用后串行重建格式并重试失败块。
- 修复 MiKTeX 包数据库被写空（0 字节）导致每次编译都重做 fndb/fontmaps/格式重建的问题，
  单次 `xelatex` 从 ~20 s 恢复到 0.7 s；排障方法写入 `_backend/README.md`。

## 许可

未指定开源许可证，保留所有权利。
