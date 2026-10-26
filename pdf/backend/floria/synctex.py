# -*- coding: utf-8 -*-
"""SyncTeX：PDF 坐标 → 源码行（点击 PDF 跳回源码）。

记录格式（SyncTeX v1）：<类型><tag>,<line>[:<x>,<y>[:<width>,<height>,<depth>]]
页面分块 '{<页码>' … '}<页码>'；坐标单位 sp（除以 65536 得 TeX pt，magnification 归一为 1000）

★ 坐标换算（2026-10-12 修正）：SyncTeX 的「pt」是 TeX pt（1/72.27 in），
  PDF/pdf.js 用的是 bp（1/72 in）——两者差一个 72/72.27 的比例；
  且头部 X/Y Offset（TeX 原点距纸角 1 in，记录值 4736287）是 **加**在记录值上的：
      bp = (raw + offset) * unit/65536 * 1000/mag * 72/72.27
  反解即 raw = bp/k - offset。
"""
import bisect
import gzip
import os
import re

from . import books

SYNC_PT = 72.0 / 72.27                     # TeX pt → PDF bp
SYNC_REC = re.compile(r'^(?P<t>[-\[\]()<>xkgvhr$!{}])(?:(?P<tag>-?\d+),(?P<line>-?\d+))?'
                      r'(?::(?P<x>-?\d+),(?P<y>-?\d+))?(?::(?P<w>-?\d+),(?P<h>-?\d+),(?P<d>-?\d+))?')
SYNC_CACHE = {'key': None, 'data': None}   # 单条缓存（当前书稿）
SYNC_TOL = 2.0                             # 命中判定容差（bp）
SYNC_LINE_MAX = 45.0                       # 「文本行级盒」高度上限（bp）：排除整页外框这类大盒
SYNC_TRUST = 15                            # 命中盒与单调基准相差超过这么多行 → 判为伪记录


def sync_k(data):
    """记录值 → bp 的比例因子（含 TeX pt → bp）。"""
    return data['unit'] / 65536.0 * (1000.0 / data['mag']) * SYNC_PT


def _parse_synctex(path, tex_name, book_dir):
    """解析 .synctex.gz，按页分组。"""
    mag, unit, ox, oy = 1000.0, 1.0, 0.0, 0.0
    inputs, pages, page = {}, {}, 0
    root = 1

    def add_input(raw):
        nonlocal root
        parts = raw.rstrip('\n').split(':', 2)
        if len(parts) != 3:
            return
        try:
            tag = int(parts[1])
        except ValueError:
            return
        p = parts[2].strip()
        if not os.path.isabs(p):
            p = os.path.join(book_dir, p)
        inputs[tag] = os.path.normpath(p)
        if tex_name and os.path.basename(p).lower() == tex_name.lower():
            root = tag

    with gzip.open(path, 'rt', encoding='utf-8', errors='replace') as f:
        for raw in f:                # 头部：Input / Magnification / Unit / Offset，直到 Content:
            if raw.startswith('Content:'):
                break
            if raw.startswith('Input:'):
                add_input(raw)
            elif raw.startswith('Magnification:'):
                mag = float(raw.split(':', 1)[1] or 1000) or 1000.0
            elif raw.startswith('Unit:'):
                unit = float(raw.split(':', 1)[1] or 1) or 1.0
            elif raw.startswith('X Offset:'):
                ox = float(raw.split(':', 1)[1] or 0)
            elif raw.startswith('Y Offset:'):
                oy = float(raw.split(':', 1)[1] or 0)
        for raw in f:                # 正文
            if not raw:
                continue
            c = raw[0]
            if c == '{':
                page = int(raw[1:])
                pages.setdefault(page, [])
                continue
            if c == '}':
                page = 0
                continue
            if raw.startswith('Input:'):
                add_input(raw)
                continue
            if page == 0:
                continue
            m = SYNC_REC.match(raw)
            if not m:
                continue
            g = m.groupdict()
            if g['tag'] is None or g['x'] is None:
                continue
            pages[page].append((int(g['line']), int(g['x']), int(g['y']),
                                int(g['w'] or 0), int(g['h'] or 0), int(g['d'] or 0),
                                int(g['tag'])))
    base = os.path.abspath(book_dir)
    files = {}
    for tag, p in inputs.items():
        ap = os.path.abspath(p)
        try:
            rel = os.path.relpath(ap, base)
        except ValueError:           # 跨盘符
            continue
        segs = rel.replace(os.sep, '/').split('/')
        if rel.startswith('..') or any(s.startswith('.') for s in segs):
            continue
        if not ap.lower().endswith('.tex'):
            continue
        files[tag] = '/'.join(segs)
    # 目录页（\tableofcontents）的记录全指向 .build/main.toc，而 .build 被上面的
    # 过滤挡掉 → 点目录页永远「无源码映射」。把 .toc 记录统一折到 main.tex 的
    # \tableofcontents 行上，点目录空白处也能跳回目录命令。
    toc_line = _toc_command_line(book_dir, tex_name)
    toc_tags = set()
    if toc_line and root in files:
        for tag, p in inputs.items():
            if os.path.basename(p.replace('\\', '/')).lower().endswith('.toc'):
                toc_tags.add(tag)
    for pg, recs in pages.items():
        out = []
        for r in recs:
            if r[6] in toc_tags:
                out.append((toc_line, r[1], r[2], r[3], r[4], r[5], root))
            elif r[6] in files:
                out.append(r)
        pages[pg] = out
    return {'pages': pages, 'files': files, 'mag': mag, 'unit': unit,
            'ox': ox, 'oy': oy, 'root': root}


def _toc_command_line(book_dir, tex_name):
    """main.tex 里 \\tableofcontents 的行号（没有则 None）。"""
    try:
        with open(os.path.join(book_dir, tex_name), encoding='utf-8', errors='replace') as f:
            for i, ln in enumerate(f, 1):
                if '\\tableofcontents' in ln.split('%', 1)[0]:
                    return i
    except OSError:
        pass
    return None


def load_synctex(path, tex_name, book_dir):
    key = (path, os.path.getmtime(path), os.path.getsize(path))
    if SYNC_CACHE['key'] != key:
        SYNC_CACHE['key'], SYNC_CACHE['data'] = key, _parse_synctex(path, tex_name, book_dir)
    return SYNC_CACHE['data']


def _page_lines(data, page):
    """取某页「可信的行级记录」，按 y 排序，返回 (全部行级盒, 单调基准线)。

    页眉/页脚/整页外框这类记录必然破坏「y 增大 → 行号增大」的顺序，取最长不减子序列
    作为单调基准：命中行与基准相差过远即判为伪记录。
    """
    cache = data.setdefault('lines', {})
    if page in cache:
        return cache[page]
    recs = data['pages'].get(page) or []
    hmax = SYNC_LINE_MAX / sync_k(data)
    cands = [r for r in recs if r[3] > 0 and 0 < r[4] <= hmax]
    if not cands:
        cands = [r for r in recs if r[3] > 0] or recs
    order = sorted(range(len(cands)), key=lambda i: cands[i][2])   # 按 y
    tails, idxs, prev = [], [], [-1] * len(order)
    for k, i in enumerate(order):
        j = bisect.bisect_right(tails, cands[i][0])
        if j == len(tails):
            tails.append(cands[i][0]); idxs.append(k)
        else:
            tails[j] = cands[i][0]; idxs[j] = k
        prev[k] = idxs[j - 1] if j else -1
    keep, k = [], (idxs[-1] if idxs else -1)
    while k >= 0:
        keep.append(order[k]); k = prev[k]
    keep.sort(key=lambda i: cands[i][2])
    out = (cands, [cands[i] for i in keep])
    cache[page] = out
    return out


def lookup(b, page, xpt, ypt):
    """(书稿, 页号, PDF 坐标 pt·原点页面左上) → {'line','page','file'} 或 {'error': …}。"""
    path = books.book_synctex_path(b['name'])
    if not os.path.isfile(path):
        return {'error': '还没有 synctex 数据——请点状态胶囊重新编译一次（编译已带 -synctex=1）'}
    try:
        data = load_synctex(path, os.path.basename(b['texName']), books.book_dir(b['name']))
    except (OSError, EOFError, gzip.BadGzipFile) as e:
        return {'error': 'synctex 解析失败：%s' % e}
    page = int(page)
    if not (data['pages'].get(page) or []):
        return {'error': '本页无源码映射（封面 / 目录 / 纯图片页）'}
    cands, mono = _page_lines(data, page)
    if not cands:
        return {'error': '本页无源码映射（封面 / 目录 / 纯图片页）'}
    k = sync_k(data)                     # 记录值 → bp（含 TeX pt → bp）
    vx, vy = xpt / k - data['ox'], ypt / k - data['oy']   # 头部 X/Y Offset 是加在记录值上的
    tol = SYNC_TOL / k
    ys = [r[2] for r in mono]
    i = bisect.bisect_left(ys, vy)
    ref = min(mono[max(0, i - 1):i + 1] or mono, key=lambda r: abs(r[2] - vy))
    files = data.get('files') or {}

    def out(line, tag):
        return {'line': line, 'page': page, 'file': files.get(tag) or b['texName']}

    best = None
    for (line, x, y, w, h, d, tag) in cands:
        if w > 0 and (x - tol) <= vx <= (x + w + tol) and (y - h - tol) <= vy <= (y + d + tol):
            area = w * max(h + d, 1)
            if best is None or area <= best[0]:
                best = (area, line, x, y, tag)
    if best is None:
        return out(ref[0], ref[6])                         # 无盒命中（页边/纯图页）→ 正文最近行
    # 同一 (x,y) 原点的记录里取最小行号：外层行盒的行号是 \par 收尾时的「当前行」。
    same = [r[0] for r in (data['pages'].get(page) or [])
            if r[1] == best[2] and r[2] == best[3] and r[6] == best[4]]
    cand = min(same) if same else best[1]
    if abs(cand - ref[0]) <= SYNC_TRUST:
        return out(cand, best[4])
    return out(ref[0], ref[6])
