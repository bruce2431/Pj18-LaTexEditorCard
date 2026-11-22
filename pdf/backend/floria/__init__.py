# -*- coding: utf-8 -*-
"""Pj18 预览后端的分层实现。

依赖方向（严格单向，下层不得 import 上层）：
    server → app → http_api → compiler/doctor → fastpath/books/ledger/synctex/locks/toolchain → config/log
"""
import hashlib
import os
import sys

_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_VENDOR_DIR = os.path.join(_BACKEND_ROOT, 'vendor')
if os.path.isdir(_VENDOR_DIR) and _VENDOR_DIR not in sys.path:
    sys.path.insert(0, _VENDOR_DIR)

__version__ = '1.0.1'


def source_fingerprint():
    """pdf/backend 下所有 .py 的 mtime+size 指纹（不含 runtime/vendor/tex 与 __pycache__）。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    h = hashlib.sha256()
    files = []
    for cur, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d not in ('runtime', 'vendor', 'tex', '__pycache__')]
        for n in sorted(names):
            if not (n.endswith('.py') or n == 'floria_merge.exe'):
                continue
            p = os.path.join(cur, n)
            try:
                st = os.stat(p)
            except OSError:
                continue
            rel = os.path.relpath(p, root).replace(os.sep, '/')
            files.append(rel)
            h.update(('%s:%d:%d;' % (rel, st.st_mtime_ns, st.st_size)).encode('utf-8'))
    return h.hexdigest(), sorted(files)


BUILD_ID = source_fingerprint()[0][:12]
