# -*- coding: utf-8 -*-
"""兼容 shim：实现在 `floria/fastpath.py`。

旧代码（含外部脚本）`import fastbuild; fastbuild.try_compile(...)` 继续可用；
属性一律转发到 floria.fastpath（包括会变的 LAST_SKIP / LAST_PHASES / LAST_CHUNKS）。
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from floria import fastpath as _impl  # noqa: E402


def __getattr__(name):
    return getattr(_impl, name)


def __dir__():
    return sorted(dir(_impl))
