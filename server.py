# -*- coding: utf-8 -*-
"""Floria LaTeX 本地服务器（纯 Python 标准库，零依赖）。

用法（或直接双击同目录 启动.bat）：
    python server.py [--port 8765] [--root BOOKS_DIR] [--no-open]

作用：像 Pj15 一样由本地进程直接读盘——页面经 http://127.0.0.1:<port> 打开后
自动列出/加载书稿文件夹（含图片），零弹窗；Ctrl+S 保存经 /api/write 写回磁盘真实文件。

安全边界：
- 只绑定 127.0.0.1，不监听外网
- 静态文件只出自 app/ 目录；书稿目录只出自 --root 下「递归扫到的含 .tex 目录」白名单
  （书稿名由发现结果反查，外部传入的名字不在白名单即 404，天然防目录穿越）
- 文件名一律取 basename，拒绝 '..'；写回只允许覆盖已存在的 .tex
"""
import argparse
import bisect
import gzip
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_DIR = os.path.dirname(os.path.abspath(__file__))
IMG_EXT = {'.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp', '.bmp'}

CONTENT_TYPES = {
    '.html': 'text/html; charset=utf-8', '.css': 'text/css; charset=utf-8',
    '.js': 'text/javascript; charset=utf-8', '.json': 'application/json; charset=utf-8',
    '.svg': 'image/svg+xml', '.png': 'image/png', '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg', '.gif': 'image/gif', '.webp': 'image/webp',
    '.bmp': 'image/bmp', '.woff': 'font/woff', '.woff2': 'font/woff2',
    '.ttf': 'font/ttf', '.ico': 'image/x-icon', '.map': 'application/json',
    '.pdf': 'application/pdf',
}

BOOKS_ROOT = None  # 运行时由 main() 设定
COMPILE_LOCK = threading.Lock()  # 全局同时只跑一个编译（书稿大，避免 CPU 叠爆）

# 递归发现书稿时跳过的目录名（隐藏目录另按前缀 '.' 跳过）
SKIP_DIRS = {'node_modules', '__pycache__', 'venv', '.venv', 'site-packages'}


def is_temp_dir(name):
    """工作区临时任务目录命名 YYYYMMDDHHMMSS-名称（其中的 .tex 多为测试夹具，不算书稿）。"""
    return len(name) > 15 and name[:14].isdigit() and name[14] == '-'


def discover_books():
    """递归扫描根目录下「含 .tex 文件」的目录 → 书稿列表（每个目录=一篇文章）。

    跳过：隐藏目录（.build/.git/…）、node_modules/__pycache__ 等构建垃圾、
    工作区临时任务目录（YYYYMMDDHHMMSS-名称，其中的 .tex 多为测试夹具）；
    根目录自身直接放的 .tex 不算书稿（工作区规则本就禁止根目录散文件）。
    书稿名 = 相对根目录的 POSIX 路径（可含 '/'，如 'papers/2026-xxx'），卡片直接显示。
    mtime = 该目录主 .tex 的修改时间（int 毫秒，与 book_pdf_info 一致；取不到退目录 mtime，再退 0）。
    """
    books = []
    root_abs = os.path.abspath(BOOKS_ROOT)
    for cur, dirs, files in os.walk(root_abs):
        dirs[:] = [d for d in dirs
                   if not d.startswith('.') and d not in SKIP_DIRS and not is_temp_dir(d)]
        if os.path.abspath(cur) == root_abs:
            continue  # 根目录自身不算书稿
        texs = sorted(f for f in files if f.lower().endswith('.tex') and not f.startswith('.'))
        if not texs:
            continue
        lower = [f.lower() for f in texs]
        tex_name = texs[lower.index('main.tex')] if 'main.tex' in lower else texs[0]
        images = sorted(f for f in files
                        if os.path.splitext(f)[1].lower() in IMG_EXT and not f.startswith('.'))
        rel = os.path.relpath(cur, root_abs).replace(os.sep, '/')
        try:
            mt = os.path.getmtime(os.path.join(cur, tex_name))
        except OSError:
            try:
                mt = os.path.getmtime(cur)
            except OSError:
                mt = 0
        books.append({'name': rel, 'texName': tex_name, 'images': images, 'mtime': int(mt * 1000)})
    books.sort(key=lambda b: b['name'])
    return books


def find_book(name):
    for b in discover_books():
        if b['name'] == name:
            return b
    return None


def book_pdf_path(name):
    """成品 PDF 的唯一落点 = 书稿根的 `main.pdf`（2026-10-11 起「成品与中间产物分离」）。

    中间产物（.aux/.log/.out/.toc/.xdv/.fls/.fdb_latexmk/.synctex.gz）全在 `<书稿>/.build/`，
    只有成品 PDF 出到书稿根 —— 与书稿目录里「PDF 与 main.tex 并排」的既有约定一致，
    `.build/` 因此可以整个删掉重建而不碰成品。
    """
    return os.path.join(BOOKS_ROOT, name, 'main.pdf')


def book_synctex_path(name):
    """「点 PDF 跳源码」用的 synctex —— latexmk 把它跟成品 PDF 一起输出到书稿根。"""
    return os.path.join(BOOKS_ROOT, name, 'main.synctex.gz')


def book_pdf_info(b):
    """书稿的 PDF 编译产物状态：exists=有 main.pdf；fresh=pdf 比 .tex 新（无需重编）。"""
    tex_p = os.path.join(BOOKS_ROOT, b['name'], b['texName'])
    pdf_p = book_pdf_path(b['name'])
    if not os.path.isfile(pdf_p):
        return {'exists': False, 'fresh': False, 'mtime': 0}
    fresh = os.path.getmtime(pdf_p) >= os.path.getmtime(tex_p) - 1
    return {'exists': True, 'fresh': fresh, 'mtime': int(os.path.getmtime(pdf_p) * 1000)}


# ---------- 内置编译链定位（MiKTeX Portable 随 preview/tex/ 携带） ----------
# 编译工具优先用**项目自带**的（APP_DIR/tex/ 下任意深度的同名可执行文件），
# 找不到才回落系统 PATH —— 使本应用自包含：整体拷到别的机器无需另装 TeX。
_TEX_TOOL_CACHE = {}


def find_tex_tool(name):
    """定位 TeX 工具：先项目自带 tex/<任意深度>/<name>(.exe)，再系统 PATH。结果缓存。"""
    if name in _TEX_TOOL_CACHE:
        return _TEX_TOOL_CACHE[name]
    exe = (name + '.exe') if os.name == 'nt' else name
    found = None
    root = os.path.join(APP_DIR, 'tex')
    # 快路径：MiKTeX Portable 的标准位置（省得对 40k+ 文件的全树 walk）
    for rel in ('texmfs/install/miktex/bin/x64', 'texmfs/install/miktex/bin'):
        cand = os.path.join(root, *rel.split('/'), exe)
        if os.path.isfile(cand):
            found = cand
            break
    if not found and os.path.isdir(root):
        for cur, _dirs, files in os.walk(root):
            for f in files:
                if f.lower() == exe.lower():
                    found = os.path.join(cur, f)
                    break
            if found:
                break
    if not found:
        found = shutil.which(name)
    _TEX_TOOL_CACHE[name] = found
    return found


# MiKTeX 的 latexmk.exe 只是**脚本引导器**，真正执行的是 latexmk.pl —— 必须有 perl 才能跑
# （报错原文：MiKTeX could not find the script engine 'perl' which is required to execute 'latexmk'）。
# MiKTeX Portable 自身不含 perl，故按「随卡片携带 → 工作区 .tools → 系统 PATH → Git/Strawberry 常见位置」找。
_PERL_CACHE = {}


def find_perl():
    """定位 perl（latexmk 的脚本引擎）；找不到返回 None（调用方回落直驱 xelatex）。结果缓存。"""
    if 'perl' in _PERL_CACHE:
        return _PERL_CACHE['perl']
    exe = 'perl.exe' if os.name == 'nt' else 'perl'
    cands = [os.path.join(APP_DIR, 'tex', 'perl', 'bin', exe),   # 与 MiKTeX 同处一室（若装了便携 perl）
             os.path.join(APP_DIR, 'tex', 'perl', exe)]
    d = APP_DIR                                                  # 工作区便携约定：逐级向上找 .tools/perl
    for _ in range(4):
        nd = os.path.dirname(d)
        if not nd or nd == d:
            break
        d = nd
        cands.append(os.path.join(d, '.tools', 'perl', 'bin', exe))
    w = shutil.which('perl')
    if w:
        cands.append(w)
    g = shutil.which('git')                                      # Git for Windows 自带 usr/bin/perl.exe
    if g:
        cands.append(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(g))), 'usr', 'bin', exe))
    cands += [r'C:\Program Files\Git\usr\bin\perl.exe',
              r'C:\Program Files (x86)\Git\usr\bin\perl.exe',
              r'C:\Strawberry\perl\bin\perl.exe',
              os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Programs', 'Git', 'usr', 'bin', 'perl.exe')]
    found = next((c for c in cands if c and os.path.isfile(c)), None)
    _PERL_CACHE['perl'] = found
    return found


def _hoist_outputs(book_dir):
    """把误落在 .build/ 里的成品搬回书稿根（latexmk -auxdir 已自己搬，此处兜直驱回落路径）。"""
    for f in ('main.pdf', 'main.synctex.gz'):
        src = os.path.join(book_dir, '.build', f)
        dst = os.path.join(book_dir, f)
        if os.path.isfile(src) and (not os.path.isfile(dst) or os.path.getmtime(src) > os.path.getmtime(dst)):
            try:
                shutil.move(src, dst)
            except OSError:
                pass


def run_compile(b):
    """latexmk -xelatex 编译书稿 → **书稿根的 main.pdf**（引擎优先用项目自带的 MiKTeX）。

    带 -synctex=1：产出书稿根的 main.synctex.gz，供「点 PDF 跳转源码」用。
    **成品与中间产物分离**（2026-10-11）：`-auxdir=.build` ⇒ 中间产物（aux/log/out/toc/xdv/
    fls/fdb_latexmk）全进 `<书稿>/.build/`，只有成品 PDF 与 synctex 出到书稿根 ⇒ `.build/`
    可整个删掉重建而不碰成品。
    **引擎选 xelatex 不选 lualatex**（2026-10-11 定案）：ctex/ctexbook 在 LuaTeX 下走 luatexja，
    要 require 几十个 ltj-*.lua，每次模块查找约 0.25s ⇒ 每次编译固定先烧 ~21s，与字体/文档无关；
    XeLaTeX 下走 xeCJK（C 实现），无这层。整本 MATH：lualatex 67.3s → xelatex 23.9s，
    页数/版面/字体一致，synctex 同坐标命中行号完全相同。
    根因报告与复检脚本见 .claude/neturon/neurons/Neuron-Pj18-LaTex论文书写/l3.raw/files/
    的「编译引擎_lualatex慢的根因与xelatex切换.md」。**别再改回 lualatex。**
    """
    book_dir = os.path.join(BOOKS_ROOT, b['name'])
    build_dir = os.path.join(book_dir, '.build')
    os.makedirs(build_dir, exist_ok=True)
    mk = find_tex_tool('latexmk')
    tex = find_tex_tool('xelatex')
    perl = find_perl()                       # latexmk 是 perl 脚本，没 perl 就跑不起来
    use_mk = bool(mk and perl)
    if not use_mk and not tex:
        return {'ok': False, 'error': '未找到可用的 TeX 引擎（项目自带 tex/ 与系统 PATH 均无 xelatex）', 'log': '', 'time': 0}
    env = os.environ.copy()
    if use_mk:
        # 老书稿（改前编译的）没有 synctex，而 latexmk 会认为 PDF 是最新的直接跳过
        # → 首次补一把 -g 强制全量，之后 synctex 已在，恢复增量（秒级）
        force = [] if os.path.isfile(book_synctex_path(b['name'])) else ['-g']
        cmd = [mk, '-xelatex', '-synctex=1'] + force + ['-interaction=nonstopmode', '-halt-on-error',
                                                        '-file-line-error', '-auxdir=.build', b['texName']]
        # MiKTeX 的 latexmk.exe 从 PATH 找 perl（网关拉起的后端 PATH 里常常没有）→ 显式补上
        env['PATH'] = os.path.dirname(perl) + os.pathsep + env.get('PATH', '')
    else:  # 没有 latexmk 或没有 perl → 直接驱动 xelatex 跑两遍（慢，但零外部依赖）
        # 直驱没法只把 aux 分流，故全写 .build 再搬成品（_hoist_outputs）
        cmd = [tex, '-synctex=1', '-interaction=nonstopmode', '-halt-on-error', '-file-line-error',
               '-output-directory=.build', b['texName']]
    chain = ('[编译链] latexmk -xelatex 增量（perl: %s）' % perl) if use_mk else \
            '[编译链] xelatex 直驱两遍（未找到 perl，已跳过 latexmk；较慢）'
    t0 = time.time()
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    try:
        p = subprocess.run(cmd, cwd=book_dir, timeout=420, capture_output=True, text=True,
                           encoding='utf-8', errors='replace', creationflags=flags, env=env)
        second = None
        if not use_mk and p.returncode == 0:  # 手动模式第二遍
            second = subprocess.run(cmd, cwd=book_dir, timeout=420, capture_output=True, text=True,
                                    encoding='utf-8', errors='replace', creationflags=flags, env=env)
        elapsed = round(time.time() - t0, 1)
        _hoist_outputs(book_dir)
        pdf_p = book_pdf_path(b['name'])
        if (second.returncode if second else p.returncode) == 0 and os.path.isfile(pdf_p):
            log = ((second.stdout if second else p.stdout) or '') + '\n' + chain
            return {'ok': True, 'time': elapsed, 'mtime': int(os.path.getmtime(pdf_p) * 1000),
                    'log': log[-1200:]}
        log = ((second or p).stdout or '') + '\n' + ((second or p).stderr or '')
        # 失败时提取关键错误行（LaTeX 的 ! 开头行 + file:line: 格式 + 脚本引擎缺失那类）
        errs = [ln for ln in log.splitlines()
                if ln.startswith('!') or ':error:' in ln or 'Emergency stop' in ln
                or 'did not succeed' in ln or 'could not find' in ln]
        tail = ('\n'.join(errs[-12:]) or log[-1500:]) + '\n' + chain
        return {'ok': False, 'time': elapsed, 'log': tail, 'error': '编译失败（返回码 %d）' % (second or p).returncode}
    except subprocess.TimeoutExpired:
        return {'ok': False, 'time': round(time.time() - t0, 1),
                'error': '编译超时（420s）——首次编译装包或图形宏包可能太慢，可先在 VSCode 里编译一次', 'log': ''}


# ---------- SyncTeX：PDF 坐标 → 源码行（点击 PDF 跳回源码） ----------
# 记录格式（SyncTeX v1）：<类型><tag>,<line>[:<x>,<y>[:<width>,<height>,<depth>]]
# 页面分块 '{<页码>' … '}<页码>'；坐标单位 sp（除以 65536 得 TeX pt，magnification 归一为 1000）
#
# ★ 坐标换算（2026-10-12 修正）：SyncTeX 的「pt」是 TeX pt（1/72.27 in），
#   PDF/pdf.js 用的是 bp（1/72 in）——两者差一个 72/72.27 的比例；
#   且头部 X/Y Offset（TeX 原点距纸角 1 in，记录值 4736287）是 **加**在记录值上的：
#       bp = (raw + offset) * unit/65536 * 1000/mag * 72/72.27
#   反解即 raw = bp/k - offset。旧写法 `raw = bp*65536 + offset` 既漏了 72/72.27、又把
#   offset 的符号搞反了，导致每次点击都被当成偏低 ~144 bp 的位置 → 稳定跳到**后面十来行**
#   （「源码映射存在偏移」的根因；换引擎后 offset 从 0 变成 4736287 才暴露）。
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


def _parse_synctex(path, tex_name):
    """解析 .synctex.gz，只留根文件（书稿 main.tex）的记录，按页分组。"""
    mag, unit, ox, oy = 1000.0, 1.0, 0.0, 0.0
    root, pages, page = 1, {}, 0     # SyncTeX 约定 Input 1 = 根文档
    with gzip.open(path, 'rt', encoding='utf-8', errors='replace') as f:
        for raw in f:                # 头部：Input / Magnification / Unit / Offset，直到 Content:
            if raw.startswith('Content:'):
                break
            if raw.startswith('Input:'):
                parts = raw.rstrip('\n').split(':', 2)
                if len(parts) == 3 and tex_name and os.path.basename(parts[2]).lower() == tex_name.lower():
                    root = int(parts[1])
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
            if page == 0:
                continue
            m = SYNC_REC.match(raw)
            if not m:
                continue
            g = m.groupdict()
            if g['tag'] is None or int(g['tag']) != root or g['x'] is None:
                continue
            pages[page].append((int(g['line']), int(g['x']), int(g['y']),
                                int(g['w'] or 0), int(g['h'] or 0), int(g['d'] or 0)))
    return {'pages': pages, 'mag': mag, 'unit': unit, 'ox': ox, 'oy': oy, 'root': root}


def load_synctex(path, tex_name):
    key = (path, os.path.getmtime(path), os.path.getsize(path))
    if SYNC_CACHE['key'] != key:
        SYNC_CACHE['key'], SYNC_CACHE['data'] = key, _parse_synctex(path, tex_name)
    return SYNC_CACHE['data']


def _page_lines(data, page):
    """取某页「可信的行级记录」，按 y（页面自上而下）排序，返回 (全部行级盒, 单调基准线)。

    页眉/页脚/整页外框这类记录，TeX 在 shipout 时把它们记成「当前行」= 本页末尾的行号，
    直接参与命中会把页边点击全拽到页尾。它们必然破坏「y 增大 → 行号增大」的顺序，
    故取最长不减子序列作为该页的单调基准：命中行与基准相差过远即判为这类伪记录。
    结果按页缓存（每页只算一次）。
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


def synctex_lookup(b, page, xpt, ypt):
    """(书稿, 页号, PDF 坐标 pt·原点页面左上) → {'line': 行号} 或 {'error': …}。"""
    path = book_synctex_path(b['name'])
    if not os.path.isfile(path):
        return {'error': '还没有 synctex 数据——请点状态胶囊重新编译一次（编译已带 -synctex=1）'}
    try:
        data = load_synctex(path, os.path.basename(b['texName']))
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
    # 该 y 处的「应有行号」：单调基准里离得最近的一条
    ys = [r[2] for r in mono]
    i = bisect.bisect_left(ys, vy)
    ref = min(mono[max(0, i - 1):i + 1] or mono, key=lambda r: abs(r[2] - vy))[0]
    best = None
    for (line, x, y, w, h, d) in cands:
        # 盒内判定：x 向右、y 向下（高度在基线上方、depth 在下方）；最小盒 = 最内层，最精确
        if w > 0 and (x - tol) <= vx <= (x + w + tol) and (y - h - tol) <= vy <= (y + d + tol):
            area = w * max(h + d, 1)
            if best is None or area <= best[0]:
                best = (area, line, x, y)              # 连 (x,y) 一起记，供下面同原点取最小行
    if best is None:
        return {'line': ref, 'page': page}             # 无盒命中（页边/纯图页）→ 正文最近行
    # ★ 同一 (x,y) 原点的记录里取最小行号（2026-10-12）：外层行盒的行号是 \par 收尾时的
    #   「当前行」，而它的内容记录（hbox/kern/glue）才带真正所在行 —— 外层行盒与首内容常共享
    #   同一原点，取最小即回到真值行（残差 +1 的主因）。实测以 synctex.exe 为参照
    #   （全本 MATH 每页抽 3 词，n=206）：完全一致 33%→39%、中位仍 +1、|Δ|≤2 98%。
    same = [r[0] for r in (data['pages'].get(page) or []) if r[1] == best[2] and r[2] == best[3]]
    cand = min(same) if same else best[1]
    # 页眉/页脚/大图上的 shipout 伪记录，其行号（= 本页末尾行）远离单调基准 → 被这条挡下，
    # 回落「该 y 处的正文最近行」。故 margin_band 那条独立判据已被本判据覆盖，删去。
    if abs(cand - ref) <= SYNC_TRUST:
        return {'line': cand, 'page': page}
    return {'line': ref, 'page': page}                 # 否则用最近的正文行（页边/大图/伪记录）


NEW_BOOK_TEX = r"""\documentclass[UTF8]{ctexart}
\usepackage{amsmath,amssymb,graphicx}
\title{%(title)s}
\author{}
\date{\today}
\begin{document}
\maketitle

\section{第一节}
在这里开始写。
\end{document}
"""


def create_book(name):
    """在项目根下新建书稿目录 <name>/main.tex（递归发现随后即扫到它）。"""
    name = (name or '').strip()
    if not name:
        return {'ok': False, 'error': '书稿名不能为空'}
    if any(ch in name for ch in '\\/:*?"<>|') or name[0] == '.':
        return {'ok': False, 'error': r'名字里不能有 \ / : * ? " < > | 或以 . 开头'}
    if any(ord(ch) < 32 for ch in name):
        return {'ok': False, 'error': '名字里有不可见控制字符，请删掉重输'}
    if name.upper().split('.')[0] in {'CON', 'PRN', 'AUX', 'NUL'} or \
       (len(name) >= 4 and name[:3].upper() in {'COM', 'LPT'} and name[3].isdigit()):
        return {'ok': False, 'error': '这是 Windows 保留设备名，换一个名字'}
    if name[-1] in '. ':
        return {'ok': False, 'error': '名字不能以空格或点结尾'}
    if len(name) > 60:
        return {'ok': False, 'error': '名字太长（限 60 字内）'}
    if is_temp_dir(name):
        return {'ok': False, 'error': '这个名字形如工作区临时任务目录（时间戳-名称），会被书库扫描跳过，换个名字'}
    d = os.path.join(BOOKS_ROOT, name)
    if os.path.exists(d):
        return {'ok': False, 'error': '同名书稿目录已存在'}
    try:
        os.makedirs(d)
        with open(os.path.join(d, 'main.tex'), 'w', encoding='utf-8') as f:
            f.write(NEW_BOOK_TEX % {'title': name})
    except OSError as e:
        return {'ok': False, 'error': str(e)}
    return {'ok': True, 'name': name}


class Handler(BaseHTTPRequestHandler):
    server_version = 'FloriaLaTeX/0.3.0'

    def log_message(self, fmt, *args):
        if self.path.startswith('/api') or self.path.startswith('/img'):
            sys.stderr.write('[%s] %s\n' % (self.log_date_time_string(), fmt % args))

    # ---------- helpers ----------
    def send_json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def send_file(self, path):
        try:
            with open(path, 'rb') as f:
                data = f.read()
        except OSError:
            self.send_json({'error': 'not found'}, 404)
            return
        ext = os.path.splitext(path)[1].lower()
        self.send_response(200)
        self.send_header('Content-Type', CONTENT_TYPES.get(ext, 'application/octet-stream'))
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def send_redirect(self, to):
        self.send_response(302)
        self.send_header('Location', to)
        self.send_header('Content-Length', '0')
        self.end_headers()

    # ---------- GET ----------
    def do_GET(self):
        path = posixpath.normpath(urllib.parse.unquote(self.path.split('?', 1)[0])).replace('\\', '/')
        if path.startswith('/api/books'):
            books = discover_books()
            for b in books:          # 目录页要显示「已编译 / 未编译」，随列表一次带回，省得逐本再请求
                b['pdf'] = book_pdf_info(b)
            return self.send_json(books)
        if path.startswith('/api/read'):
            qs = {}
            if '?' in self.path:
                for kv in self.path.split('?', 1)[1].split('&'):
                    if '=' in kv:
                        k, v = kv.split('=', 1)
                        qs[k] = urllib.parse.unquote(v)
            b = find_book(qs.get('book', ''))
            if not b:
                return self.send_json({'error': 'book not found'}, 404)
            p = os.path.join(BOOKS_ROOT, b['name'], os.path.basename(b['texName']))
            try:
                with open(p, 'r', encoding='utf-8', errors='replace') as f:
                    text = f.read()
            except OSError as e:
                return self.send_json({'error': str(e)}, 500)
            return self.send_json({'texName': b['texName'], 'text': text, 'images': b['images'],
                                   'pdf': book_pdf_info(b)})
        if path.startswith('/img/'):
            rel = path[len('/img/'):]
            if '/' not in rel:
                return self.send_json({'error': 'bad path'}, 400)
            book, fname = rel.rsplit('/', 1)  # 书稿名可含 '/'（递归发现出的相对路径）
            b = find_book(book)
            if not b:
                return self.send_json({'error': 'book not found'}, 404)
            ext = os.path.splitext(fname)[1].lower()
            if ext not in IMG_EXT:
                return self.send_json({'error': 'bad extension'}, 400)
            p = os.path.join(BOOKS_ROOT, b['name'], os.path.basename(fname))
            if not os.path.isfile(p):
                return self.send_json({'error': 'not found'}, 404)
            return self.send_file(p)
        if path.startswith('/api/sync'):
            qs = self.query()
            b = find_book(qs.get('book', ''))
            if not b:
                return self.send_json({'ok': False, 'error': 'book not found'}, 404)
            try:
                page = int(float(qs.get('page', '1')))
                x, y = float(qs.get('x', '0')), float(qs.get('y', '0'))
            except ValueError:
                return self.send_json({'ok': False, 'error': '坐标参数不对'}, 400)
            r = synctex_lookup(b, page, x, y)
            if 'error' in r:
                return self.send_json({'ok': False, 'error': r['error']})
            return self.send_json({'ok': True, 'line': r['line'], 'page': r['page'], 'texName': b['texName']})
        if path.startswith('/api/pdf'):
            qs = {}
            if '?' in self.path:
                for kv in self.path.split('?', 1)[1].split('&'):
                    if '=' in kv:
                        k, v = kv.split('=', 1)
                        qs[k] = urllib.parse.unquote(v)
            b = find_book(qs.get('book', ''))
            if not b:
                return self.send_json({'error': 'book not found'}, 404)
            pdf_p = book_pdf_path(b['name'])
            if not os.path.isfile(pdf_p):
                return self.send_json({'error': '尚未编译'}, 404)
            return self.send_file(pdf_p)
        if path in ('/', '/index.html'):
            return self.send_file(os.path.join(APP_DIR, 'index.html'))
        # 静态资源：仅 app/ 目录内部（normpath 已折叠 ../..，前缀必须含分隔符防兄弟目录）
        rel = path.lstrip('/')
        if rel == 'tex' or rel.startswith('tex/'):
            return self.send_json({'error': 'not found'}, 404)   # 自带编译链不对外托管
        full = os.path.normpath(os.path.join(APP_DIR, rel))
        if not (full == APP_DIR or full.startswith(APP_DIR + os.sep)) or not os.path.isfile(full):
            return self.send_json({'error': 'not found'}, 404)
        return self.send_file(full)

    # ---------- POST ----------
    def query(self):
        qs = {}
        if '?' in self.path:
            for kv in self.path.split('?', 1)[1].split('&'):
                if '=' in kv:
                    k, v = kv.split('=', 1)
                    qs[k] = urllib.parse.unquote(v)
        return qs

    def read_json(self):
        try:
            n = int(self.headers.get('Content-Length', '0'))
            return json.loads(self.rfile.read(n).decode('utf-8'))
        except (ValueError, OSError):
            return None

    def do_POST(self):
        path = posixpath.normpath(urllib.parse.unquote(self.path.split('?', 1)[0]))
        if path == '/api/newbook':
            body = self.read_json()
            if body is None:
                return self.send_json({'error': 'bad request'}, 400)
            return self.send_json(create_book(str(body.get('name', ''))))
        if path == '/api/compile':
            body = self.read_json()
            if body is None:
                return self.send_json({'error': 'bad request'}, 400)
            b = find_book(str(body.get('book', '')))
            if not b:
                return self.send_json({'error': 'book not found'}, 404)
            with COMPILE_LOCK:  # 串行化编译，页面端只需等待
                return self.send_json(run_compile(b))
        if path != '/api/write':
            return self.send_json({'error': 'unknown endpoint'}, 404)
        body = self.read_json()
        if body is None:
            return self.send_json({'error': 'bad request'}, 400)
        b = find_book(str(body.get('book', '')))
        if not b:
            return self.send_json({'error': 'book not found'}, 404)
        fname = os.path.basename(str(body.get('file', '')))
        if not fname.lower().endswith('.tex'):
            return self.send_json({'error': 'only .tex writable'}, 400)
        p = os.path.join(BOOKS_ROOT, b['name'], fname)
        if not os.path.isfile(p):
            return self.send_json({'error': 'refusing to create new file'}, 403)
        try:
            with open(p, 'w', encoding='utf-8', newline='') as f:
                f.write(str(body.get('content', '')))
        except OSError as e:
            return self.send_json({'error': str(e)}, 500)
        return self.send_json({'ok': True, 'file': fname})


def main():
    global BOOKS_ROOT
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=8765)
    ap.add_argument('--root', default=os.path.dirname(APP_DIR))
    ap.add_argument('--no-open', action='store_true')
    args = ap.parse_args()
    BOOKS_ROOT = os.path.abspath(args.root)

    srv = None
    for port in range(args.port, args.port + 10):
        try:
            srv = ThreadingHTTPServer(('127.0.0.1', port), Handler)
            break
        except OSError:
            continue
    if not srv:
        print('端口 %d-%d 都被占用，无法启动。' % (args.port, args.port + 9))
        sys.exit(1)

    url = 'http://127.0.0.1:%d/' % srv.server_address[1]
    print('Floria LaTeX 本地服务器已启动：' + url)
    print('书稿根目录：' + BOOKS_ROOT)
    print('（Ctrl+C 退出）')
    if not args.no_open:
        threading.Timer(0.6, webbrowser.open, [url]).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print('\n已退出。')


if __name__ == '__main__':
    main()
