# -*- coding: utf-8 -*-
"""Floria LaTeX 本地服务器入口（纯 Python 标准库，零依赖）。

用法（或直接双击同目录 启动.bat）：
    python server.py [--port 8765] [--root BOOKS_DIR] [--no-open]
                     [--auto-restart] [--doctor]

实现已拆分到同目录的 `floria/` 包（config/log/toolchain/books/ledger/synctex/
locks/fastpath/compiler/doctor/http_api/watchdog/app）。本文件只做：
  1. 便携 runtime 的 sys.path 修补（python312._pth 不含脚本目录）；
  2. vendor/（pypdf）注入；
  3. 交给 floria.app.main()。
"""
import os
import sys

APP_DIR = os.path.dirname(os.path.abspath(__file__))      # <预览根>/pdf/backend
CARD_ROOT = os.path.dirname(os.path.dirname(APP_DIR))     # <预览根>（后端在卡目录内）

# 便携 runtime（embeddable python）靠 python312._pth 定 sys.path，里面只有 runtime/，
# 不含脚本所在目录 —— 不补这一行，`import floria` 在网关拉起的后端里必失败。
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

# 网关用 PATH 里的系统 `python` 拉起本服务，那个解释器不一定装了 pypdf；
# 随后端携带一份纯 Python 的 pypdf（vendor/），保证按章并行的快速路径在任意解释器下都能用。
_VENDOR = os.path.join(APP_DIR, 'vendor')
if os.path.isdir(_VENDOR) and _VENDOR not in sys.path:
    sys.path.insert(0, _VENDOR)

from floria.app import main  # noqa: E402  （必须在 sys.path 修补之后）

if __name__ == '__main__':
    sys.exit(main() or 0)
