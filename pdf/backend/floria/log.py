# -*- coding: utf-8 -*-
"""统一日志：写 stderr（网关把后端 stdout/stderr 落到 preview/backend.log）。

原来的问题是异常被 `except: pass` 吞掉，出事后只能靠翻账本。这里给一个统一的出口：
所有「不影响主流程但要让人知道」的失败都走 warning/exception，至少留在 backend.log 里。
"""
import sys
import time
import traceback

_PREFIX = '[floria]'

# Windows 默认 cp936：中文日志落到 backend.log 会变 mojibake。显式 UTF-8（改不动就算了）。
for _s in (sys.stderr, sys.stdout):
    try:
        _s.reconfigure(encoding='utf-8', errors='replace')
    except Exception:          # noqa: BLE001  非 TextIOWrapper（重定向/测试环境）
        pass


def _emit(level, msg):
    try:
        sys.stderr.write('%s %s %s %s\n' % (time.strftime('%Y-%m-%d %H:%M:%S'), _PREFIX,
                                            level, msg))
        sys.stderr.flush()
    except Exception:          # noqa: BLE001  日志自身失败绝不影响业务
        pass


def info(msg):
    _emit('INFO', msg)


def warn(msg):
    _emit('WARN', msg)


def error(msg):
    _emit('ERROR', msg)


def exception(where, exc):
    _emit('EXC', '%s: %s: %s\n%s' % (where, type(exc).__name__, exc,
                                      traceback.format_exc(limit=4)))
