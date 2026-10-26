# -*- coding: utf-8 -*-
"""编译账本：<书稿>/.build/compile-history.jsonl，一行一条 JSON。

v2 字段（新记录）：
    {ts, ok, elapsed, pages, by, fast, mode, reason, phases, chunks, error, log}
旧记录只有 {ts, ok, elapsed, pages, by, error, log} —— 读取时 normalize() 补默认值，
**绝不回写旧行**（旧进程/旧前端仍可读）。
"""
import json
import os
import time

from . import config
from .books import book_pdf_path
from .log import warn
from .toolchain import find_tex_tool


def ledger_path(name):
    return os.path.join(config.BOOKS_ROOT, name, '.build', config.LEDGER_NAME)


def ledger_append(name, rec):
    """追加一条编译记录（只追加；写失败记日志而不是静默吞掉）。"""
    p = ledger_path(name)
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, 'a', encoding='utf-8') as f:
            f.write(json.dumps(rec, ensure_ascii=False) + '\n')
    except OSError as e:
        warn('写编译账本失败 %s: %s' % (p, e))


def normalize(rec):
    """旧记录补默认值（纯内存，不落盘）。"""
    if 'mode' not in rec:
        if rec.get('fast') is True or rec.get('by') == 'chapter':
            rec['mode'] = 'fast'
        elif rec.get('fast') is False:
            rec['mode'] = 'full'
        else:
            rec['mode'] = 'unknown'
    rec.setdefault('fast', rec['mode'] == 'fast')
    rec.setdefault('reason', None)
    rec.setdefault('phases', None)
    rec.setdefault('chunks', None)
    return rec


def pages_in(text):
    """从编译输出里抓成品页数：优先 'Output written on …pdf (N pages)'，退其次任意一条。"""
    if not text:
        return None
    best = None
    import re
    for m in re.finditer(r'Output written on ([^\n(]*?)\((\d+)\s+pages?', text):
        if m.group(1).lower().endswith('.pdf'):
            return int(m.group(2))
        best = int(m.group(2))
    return best


def scrape_log(logpath):
    """从 .build/main.log 抓「上次编译」的成败与页数（供账本回溯）。"""
    try:
        with open(logpath, 'r', encoding='utf-8', errors='replace') as f:
            txt = f.read()
    except OSError:
        return (None, None)
    ok = ('Output written on' in txt and 'Fatal error occurred' not in txt
          and 'Emergency stop' not in txt)
    return (ok, pages_in(txt))


def _compiling(name, lock_probe=None):
    """该书是否正在编译：调用方的进程内锁，或 compile_book.py 落的 .build/.<job>.lock。"""
    if lock_probe is not None and lock_probe():
        return True
    bdir = os.path.join(config.BOOKS_ROOT, name, '.build')
    try:
        return any(fn.startswith('.') and fn.endswith('.lock') for fn in os.listdir(bdir))
    except OSError:
        return False


def _backfill(name, recs, lock_probe=None):
    """账本比磁盘编译痕迹旧 ⇒ 有绕过账本的编译 → 补一条 by='disk' 的回溯记录。

    痕迹 mtime = max(main.pdf, .build/main.log, .build/main.fdb_latexmk)；比账本最新 ts
    新 1s 以上才补。编译进行中跳过（那时 mtime 只是中途产物）。
    只在显式 reconcile 或编译结束后调用 —— GET 默认不再写盘。
    """
    if _compiling(name, lock_probe):
        return recs
    traces = [book_pdf_path(name),
              os.path.join(config.BOOKS_ROOT, name, '.build', 'main.log'),
              os.path.join(config.BOOKS_ROOT, name, '.build', 'main.fdb_latexmk')]
    mt = 0.0
    for p in traces:
        try:
            mt = max(mt, os.path.getmtime(p))
        except OSError:
            pass
    if mt <= 0:
        return recs
    newest = max((r.get('ts') or 0) for r in recs) if recs else 0
    if mt <= newest + 1:
        return recs
    ok, pages = scrape_log(os.path.join(config.BOOKS_ROOT, name, '.build', 'main.log'))
    rec = {'ts': round(mt, 3), 'ok': (ok if ok is not None else True), 'elapsed': None,
           'pages': pages, 'by': 'disk', 'fast': None, 'mode': 'unknown', 'reason': None,
           'error': '', 'log': '（由磁盘产物回溯的编译记录）'}
    ledger_append(name, rec)
    recs.append(rec)
    return recs


def ledger_read(name, reconcile=False, lock_probe=None):
    """读账本（最近 LEDGER_MAX 条，**时间倒序**：下标 0 = 最新）。坏行跳过。"""
    recs = []
    try:
        with open(ledger_path(name), 'r', encoding='utf-8', errors='replace') as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    recs.append(json.loads(ln))
                except ValueError:
                    continue
    except OSError:
        recs = []
    if reconcile:
        recs = _backfill(name, recs, lock_probe)
    recs = [normalize(r) for r in recs[-config.LEDGER_MAX:]]
    recs.reverse()
    return {'ok': True, 'records': recs}
