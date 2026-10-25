# -*- coding: utf-8 -*-
"""整本单文件 main.tex 的自动分章并行编译（2026-10-22 改版）。

用户要求：**源文件层次永远只有一本 main.tex**。切分全部发生在 `.build/` 里：
  1. 读 main.tex，按顶层 `\\chapter`（必换页）切成若干块；
  2. 每块 + 原导言区生成 wrapper，独立 xelatex 编译（可并行），
     注入页码起点与章号，使页脚/图表编号与整本一致；
  3. 用 pypdf 把「上次整本编译留下的前置页（目录）」+ 各块 PDF 按 openright
     规则补齐空白页拼回 main.pdf，并合并 synctex（行号平移回 main.tex）。

未改动的块按内容 md5 复用上次 PDF；任何前提不满足/失败都返回 None，
由 server.run_compile 回落到原来的整本 latexmk。
"""
import gzip
import hashlib
import json
import os
import re
import subprocess
import threading
import time

CACHE_NAME = '.floria_fast.json'
F_DIR = os.path.join('.build', 'floria')
CHUNK_DIR = os.path.join(F_DIR, 'chunks')
OUT_DIR = os.path.join(F_DIR, 'out')

_REC = re.compile(r'^([\[\]()hvgk$x])(\d+),(\d+):(.*)$')


# ---------- 单文件 main.tex 的自动切分 ----------
def _commented(text, pos):
    """pos 处若处在 `%` 注释里（同一行、`%` 前反斜杠数为偶数）返回 True。"""
    start = text.rfind('\n', 0, pos) + 1
    for i, ch in enumerate(text[start:pos]):
        if ch != '%':
            continue
        bs = 0
        j = start + i - 1
        while j >= start and text[j] == '\\':
            bs += 1
            j -= 1
        if bs % 2 == 0:
            return True
    return False


def _has_cmd(text, cmd):
    """是否存在未被注释的独立命令 `\\cmd`（后面不能紧跟字母，防 `\\part` 命中 `\\partial`）。"""
    pos = 0
    while True:
        pos = text.find(cmd, pos)
        if pos < 0:
            return False
        nxt = text[pos + len(cmd):pos + len(cmd) + 1]
        if not nxt.isalpha() and not _commented(text, pos):
            return True
        pos += len(cmd)


def _find_cmd(text, cmd, start=0):
    """找未注释的 `\\cmd` 位置；找不到返回 -1。"""
    pos = start
    while True:
        pos = text.find(cmd, pos)
        if pos < 0:
            return -1
        if not _commented(text, pos):
            return pos
        pos += len(cmd)


def split_document(book_dir):
    """把 main.tex 切块。返回 {preamble, front, chunks:[{id,text,line,starred}]} 或 None。

    只按 `\\chapter` 切：`\\chapter` 必换页，独立编译版面与整本一致；
    `\\section` 不换页，按它切会让断行/浮动体/页码整体漂移。
    """
    main_tex = os.path.join(book_dir, 'main.tex')
    try:
        with open(main_tex, encoding='utf-8', errors='replace') as f:
            src = f.read()
    except OSError:
        return None
    b = _find_cmd(src, '\\begin{document}')
    if b < 0:
        return None
    brace = src.find('}', b)
    if brace < 0:
        return None
    body_start = brace + 1
    e = _find_cmd(src, '\\end{document}', body_start)
    body_end = e if e > b else len(src)
    preamble = src[:b]                       # 不含 \begin{document}
    body = src[body_start:body_end]

    # 附录 / 分卷会改变章编号规则，自动切分不适用 → 回落整本编译
    # 注意要检查下一个字符：`\partial` 也以 `\part` 开头（MATH 里到处都是）。
    if _has_cmd(body, '\\appendix') or _has_cmd(body, '\\part'):
        return None

    marks = []
    for m in re.finditer(r'\\chapter\b(\s*\*)?', body):
        if _commented(body, m.start()):
            continue
        marks.append((m.start(), bool(m.group(1))))
    if len(marks) < 2:
        return None

    line0 = src.count('\n', 0, body_start) + 1
    chunks = []
    for i, (pos, starred) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(body)
        chunks.append({'id': 'ch%d' % (i + 1),
                       'text': body[pos:end],
                       'line': line0 + body.count('\n', 0, pos),
                       'starred': starred})
    return {'preamble': preamble, 'front': body[:marks[0][0]], 'chunks': chunks}


def _chunk_hash(preamble, chunk):
    h = hashlib.md5()
    h.update(preamble.encode('utf-8', 'replace'))
    h.update(b'\0')
    h.update(chunk['text'].encode('utf-8', 'replace'))
    return h.hexdigest()


def _front_hash(doc):
    return hashlib.md5(doc['front'].encode('utf-8', 'replace')).hexdigest()


def _chunk_dir(book_dir, cid):
    return os.path.join(book_dir, OUT_DIR, cid)


def _chunk_pdf(book_dir, cid):
    return os.path.join(_chunk_dir(book_dir, cid), 'chapter.pdf')


def _chunk_sync(book_dir, cid):
    return os.path.join(_chunk_dir(book_dir, cid), 'wrapper.synctex.gz')


def _cache_path(book_dir):
    return os.path.join(book_dir, '.build', CACHE_NAME)


def _sync_ok(path):
    """块 synctex 能否完整解压。**只看文件在不在不够**：进程被中断或磁盘写坏会留下
    半截 gzip，页数/缓存都还自洽，直到合并 synctex 时才抛 zlib.error；旧逻辑把异常吞掉
    并删掉 main.synctex.gz，于是编译报成功、点 PDF 却永远「还没有 synctex 数据」。"""
    try:
        with gzip.open(path, 'rb') as f:
            while f.read(1 << 20):
                pass
        return True
    except Exception:          # noqa: BLE001（zlib.error 不在 OSError 下）
        return False


def read_cache(book_dir):
    try:
        with open(_cache_path(book_dir), encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _write_cache(book_dir, data):
    p = _cache_path(book_dir)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


# ---------- PDF 工具 ----------
def _pdf_pages(path):
    try:
        import pypdf
        return len(pypdf.PdfReader(path).pages)
    except Exception:                       # noqa: BLE001
        try:
            import fitz
            d = fitz.open(path)
            n = d.page_count
            d.close()
            return n
        except Exception:                   # noqa: BLE001
            return None


def _merge_pdfs(parts, out, outline_from=None, page_map=None):
    """parts = [('pages', path, a, b) | ('blanks', n, (w, h))]，拼成 out（原子替换）。"""
    import pypdf
    w = pypdf.PdfWriter()
    dests = {}                  # 命名目标 name -> 合并后页下标（0 基）
    for part in parts:
        if part[0] == 'blanks':
            n, (bw, bh) = part[1], part[2]
            for _ in range(n):
                w.add_blank_page(width=bw, height=bh)
            continue
        _, path, a, b = part
        off = len(w.pages)
        r = pypdf.PdfReader(path)
        for i in range(a, min(b, len(r.pages))):
            w.add_page(r.pages[i])
        # 命名目标（/Names/Dests）必须逐份搬过来：print TOC 与 \ref/\cite 的 Link 注释
        # 里 dest 是名字（chapter.1 / section.2.3 …），pypdf 合并不会自动保留——
        # 丢了它们，点印刷目录/交叉引用就没反应（书签还正常，所以不易察觉）。
        try:
            for name, dest in r.named_destinations.items():
                try:
                    pi = r.get_destination_page_number(dest)
                except Exception:       # noqa: BLE001
                    continue
                if pi is None or pi < a:
                    continue
                idx = off + (pi - a)
                if idx < len(w.pages):
                    dests.setdefault(str(name), idx)
        except Exception:               # noqa: BLE001
            pass
    if outline_from and os.path.isfile(outline_from):
        try:
            r0 = pypdf.PdfReader(outline_from)

            def walk(items, parent=None):
                # pypdf 的 outline 用「紧跟在父项后的嵌套 list」表示子项，不要访问
                # Destination.children（那是方法，不是列表，会抛异常整棵树只恢复第一项）。
                last = None
                for it in items:
                    if isinstance(it, list):
                        walk(it, last if last is not None else parent)
                        continue
                    last = None
                    try:
                        idx = r0.get_destination_page_number(it)
                        title = it.title
                    except Exception:       # noqa: BLE001
                        continue
                    if idx is None or idx < 0:
                        continue
                    if page_map is not None:
                        idx = page_map.get(idx, idx)
                    if idx >= len(w.pages):
                        continue
                    last = w.add_outline_item(title, idx, parent=parent)

            walk(r0.outline)
        except Exception:                   # noqa: BLE001
            pass
        # 前置页（封面/目录）的命名目标只在旧整本 PDF 里；按 page_map 平移后再收回，
        # 已被各块 PDF 声明的同名目标优先（setdefault）。
        try:
            r0 = pypdf.PdfReader(outline_from)
            for name, dest in r0.named_destinations.items():
                try:
                    pi = r0.get_destination_page_number(dest)
                except Exception:       # noqa: BLE001
                    continue
                if pi is None or pi < 0:
                    continue
                if page_map is not None:
                    pi = page_map.get(pi, pi)
                if 0 <= pi < len(w.pages):
                    dests.setdefault(str(name), pi)
        except Exception:               # noqa: BLE001
            pass
    for name, idx in dests.items():
        try:
            w.add_named_destination(name, idx)
        except Exception:               # noqa: BLE001
            pass
    tmp = out + '.tmp'
    with open(tmp, 'wb') as f:
        w.write(f)
    os.replace(tmp, out)


# ---------- SyncTeX ----------
_HDR = ('Magnification', 'Unit', 'X Offset', 'Y Offset')


def _parse_synctex(text):
    inputs = {}
    content = []
    hdr = {}
    in_content = False
    pages = 0
    for line in text.splitlines():
        if line.startswith('Input:'):
            try:
                tag, path = line[6:].split(':', 1)
                inputs[int(tag)] = path
            except ValueError:
                pass
            continue
        for k in _HDR:
            if line.startswith(k + ':'):
                hdr[k] = line[len(k) + 1:]
                break
        if line == 'Content:':
            in_content = True
            continue
        if line.startswith('Postamble:'):
            in_content = False
            continue
        if line.startswith('!'):
            try:
                pages = max(pages, int(line[1:]))
            except ValueError:
                pass
            continue
        if in_content:
            content.append(line)
    return {'inputs': inputs, 'content': content, 'hdr': hdr, 'pages': pages}


def _merge_synctex(items, out):
    """items = [(synctex_path, page_offset, chunk_basename, chunk_start_line)]。

    块文件的 Input 路径改写成 `main.tex`，记录行号按块在 main.tex 里的起始行平移
    （chunk 第 1 行 = main.tex 第 chunk_start_line 行）。这样点 PDF 仍跳回 main.tex。
    """
    out_lines = ['SyncTeX Version:1']
    body = []
    tag_base = 0
    hdr = {}
    total_pages = 0
    for path, offset, chunk_name, chunk_line in items:
        with gzip.open(path, 'rt', encoding='utf-8', errors='replace') as f:
            d = _parse_synctex(f.read())
        for k, v in d['hdr'].items():
            hdr.setdefault(k, v)
        tag_map = {}
        shifts = {}
        for tag in sorted(d['inputs']):
            p = d['inputs'][tag]
            shift = 0
            if chunk_name and os.path.basename(p.replace('\\', '/')) == chunk_name:
                p = 'main.tex'
                shift = chunk_line - 1
            nt = tag + tag_base
            tag_map[tag] = nt
            shifts[nt] = shift
            out_lines.append('Input:%d:%s' % (nt, p))
        tag_base = (max(tag_map.values()) if tag_map else tag_base) + 1
        for line in d['content']:
            if not line:
                continue
            c = line[0]
            if c in '{}':
                try:
                    body.append('%s%d' % (c, int(line[1:]) + offset))
                except ValueError:
                    pass
                continue
            m = _REC.match(line)
            if not m:
                body.append(line)
                continue
            kind, tag, ln, rest = m.group(1), int(m.group(2)), int(m.group(3)), m.group(4)
            nt = tag_map.get(tag)
            if nt is None:               # 悬空 tag（输入表里没有）→ 丢弃
                continue
            body.append('%s%d,%d:%s' % (kind, nt, ln + shifts.get(nt, 0), rest))
        total_pages = max(total_pages, offset + d['pages'])
    out_lines += ['Output:pdf']
    for k in _HDR:
        out_lines.append('%s:%s' % (k, hdr.get(k, '1000' if k == 'Magnification' else '1')))
    out_lines.append('Content:')
    out_lines += body
    out_lines += ['Postamble:', 'Count:%d' % len(body), '!%d' % total_pages, 'Post scriptum:']
    tmp = out + '.tmp'
    with gzip.open(tmp, 'wt', encoding='utf-8') as f:
        f.write('\n'.join(out_lines))
    os.replace(tmp, out)


def _outline_starts(main_pdf):
    """整本书 PDF 顶层书签的页码（1 基）。

    **不要用 synctex 推章起点**：目录页的链接记录会把“当前行号”带到章首行号
    （TOC 里指向各章的 hyperref 目标），按行号判定会把目录页误判成章页；而且
    ctexbook 会把目录最后几页推迟到 \\clearpage 才 shipout，记录行号也是错的。
    编号章的书签是可靠锚点；`\\chapter*` 没有书签，由布局方程反解。
    """
    try:
        import pypdf
        r = pypdf.PdfReader(main_pdf)
        pages = []
        for it in r.outline:
            if isinstance(it, list):
                continue
            try:
                p = r.get_destination_page_number(it) + 1
            except Exception:               # noqa: BLE001
                continue
            if p and p > 0:
                pages.append(p)
        return pages or None
    except Exception:                       # noqa: BLE001
        return None


# ---------- 编译 ----------
def _write_if_changed(path, text):
    try:
        with open(path, encoding='utf-8') as f:
            if f.read() == text:
                return
    except OSError:
        pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(text)
    os.replace(tmp, path)


def _compile_chunk(book_dir, doc, idx, start_page, chapter_no, env,
                   texcmd, xelatex_alt, xdvipdfmx_alt, timeout=420):
    """编一块：wrapper = 导言区 + 页码/章号注入 + \\input{chunk}，产物在 .build/floria/out/chN/。"""
    chunk = doc['chunks'][idx]
    cid = chunk['id']
    chunk_path = os.path.join(book_dir, CHUNK_DIR, cid + '.tex')
    _write_if_changed(chunk_path, chunk['text'])
    outdir = _chunk_dir(book_dir, cid)
    os.makedirs(outdir, exist_ok=True)
    wrapper = os.path.join(outdir, 'wrapper.tex')
    wrap = (doc['preamble'] + '\n'
            # 跨块引用：从上次整本编译的 .build/main.aux 取外部标签（没有就不加）
            + '\\IfFileExists{.build/main.aux}{\\usepackage{xr-hyper}'
              '\\externaldocument{.build/main}}{}\n'
            + '\\begin{document}\n'
            # 必须用 begindocument/end 钩子（\\AtBeginDocument 太早：ctexbook 会在
            # 章节前 \\cleardoublepage 多插一张空白页，实测每章各多 1 页）。
            + '\\AddToHook{begindocument/end}{\\setcounter{chapter}{%d}'
              '\\setcounter{page}{%d}}\n' % (chapter_no, start_page)
            + '\\input{%s}\n' % os.path.join(CHUNK_DIR, cid + '.tex').replace('\\', '/')
            + '\\end{document}\n')
    _write_if_changed(wrapper, wrap)

    e = env.copy()
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    t0 = time.time()
    out = ''

    def run(cmd):
        nonlocal out
        p = subprocess.run(cmd, cwd=book_dir, timeout=timeout, capture_output=True,
                           text=True, encoding='utf-8', errors='replace',
                           creationflags=flags, env=e)
        out += (p.stdout or '') + (p.stderr or '')
        return p.returncode

    def aux_sig():
        h = []
        for n in ('wrapper.aux', 'wrapper.out', 'wrapper.toc'):
            f = os.path.join(outdir, n)
            try:
                with open(f, 'rb') as fh:
                    h.append(hashlib.md5(fh.read()).hexdigest())
            except OSError:
                h.append('')
        return '|'.join(h)

    rel_out = os.path.join(F_DIR, 'out', cid).replace('\\', '/')
    rel_wrap = os.path.join(rel_out, 'wrapper.tex').replace('\\', '/')
    rel_pdf = os.path.join(rel_out, 'chapter.pdf').replace('\\', '/')
    rel_xdv = os.path.join(rel_out, 'wrapper.xdv').replace('\\', '/')

    # 只用 PATH 里的裸名 xelatex（绝对路径在本机实测慢得多）；PATH 里没有再退回 mk 同目录的 exe。
    rc = 1
    for i in range(4):
        before = aux_sig()
        try:
            rc = run([texcmd, '-no-pdf', '-synctex=1', '-interaction=nonstopmode',
                      '-halt-on-error', '-file-line-error',
                      '-output-directory=' + rel_out, rel_wrap])
        except FileNotFoundError:
            if not xelatex_alt:
                raise
            texcmd = xelatex_alt
            rc = run([texcmd, '-no-pdf', '-synctex=1', '-interaction=nonstopmode',
                      '-halt-on-error', '-file-line-error',
                      '-output-directory=' + rel_out, rel_wrap])
        if rc != 0 or aux_sig() == before:
            break
    if rc == 0:
        try:
            rc = run(['xdvipdfmx', '-E', '-o', rel_pdf, rel_xdv])
        except FileNotFoundError:
            if not xdvipdfmx_alt:
                raise
            rc = run([xdvipdfmx_alt, '-E', '-o', rel_pdf, rel_xdv])
    return {'ok': rc == 0, 'secs': time.time() - t0, 'out': out,
            'pdf': _chunk_pdf(book_dir, cid), 'synctex': _chunk_sync(book_dir, cid)}


LAST_SKIP = ''                    # 最近一次未走快速路径的原因（run_compile 写进日志）


def _skip(reason):
    global LAST_SKIP
    LAST_SKIP = reason
    return None


def try_compile(book_dir, book_name, mk, perl, env, log_cb=None):
    """快速路径。成功返回 dict（同 run_compile 的成功返回），失败/不适用返回 None。"""
    global LAST_SKIP
    LAST_SKIP = ''
    doc = split_document(book_dir)
    if not doc:
        return _skip('顶层 \\chapter 少于 2 个（或含 \\appendix/\\part）')
    try:
        import pypdf  # noqa: F401
    except Exception as ex:                 # noqa: BLE001
        return _skip('缺少 pypdf：%s' % ex)
    cache = read_cache(book_dir)
    main_pdf = os.path.join(book_dir, 'main.pdf')
    if not cache or not os.path.isfile(main_pdf):
        return _skip('还没有快速缓存或 main.pdf（需先整本编译一次）')
    chunks = doc['chunks']
    n = len(chunks)
    cached = cache.get('chunks') or []
    if len(cached) != n or cache.get('front_hash') != _front_hash(doc):
        return _skip('缓存与当前 main.tex 不匹配（块数或导言区变了）')

    # 编号章（\chapter）在 PDF 里必有书签 → 起始页可信；\chapter* 没书签，靠布局反解。
    numbered = [i for i, c in enumerate(chunks) if not c['starred']]
    outline = _outline_starts(main_pdf)
    total_pdf = _pdf_pages(main_pdf)
    if not outline or len(outline) != len(numbered) or not total_pdf:
        return _skip('PDF 书签数与编号章数不一致，或页数读不出')

    # 冷缓存（整本刚编过、还没按块编过）→ 全部块都要编一遍才能知道各块页数
    cold = cache.get('front') is None or any(
        not isinstance(c.get('pages'), int) for c in cached)
    hashes = [_chunk_hash(doc['preamble'], c) for c in chunks]
    if cold:
        changed = list(range(n))
    else:
        changed = [i for i in range(n)
                   if hashes[i] != cached[i].get('built')
                   or not os.path.isfile(_chunk_pdf(book_dir, chunks[i]['id']))
                   or not _sync_ok(_chunk_sync(book_dir, chunks[i]['id']))]

    # 非星号章才推进章号；\setcounter{chapter}{n} 让章内编号与整本一致
    chapter_no = []
    k = 0
    for c in chunks:
        chapter_no.append(k)
        if not c['starred']:
            k += 1

    def layout(pages, fro):
        """给定前置页数与各块页数，算每块起始页（ctexbook openright：每章从奇数页起）。"""
        out = []
        pos = fro + 1
        for i in range(n):
            out.append(pos)
            pos += pages[i]
            if pos % 2 == 0:
                pos += 1
        return out

    def solve_front(pages):
        """用「编号章书签页 + 总页数」反解前置页数，唯一解才算通过。"""
        for fro in range(0, min(total_pdf, outline[0] + 1)):
            st = layout(pages, fro)
            if st[0] < 1:
                continue
            if [st[i] for i in numbered] != outline:
                continue
            if st[-1] + pages[n - 1] - 1 != total_pdf:
                continue
            return fro, st
        return None

    texcmd = 'xelatex'                  # 优先 PATH 解析（绝对路径在本机实测慢得多）
    texdir = os.path.dirname(mk) if mk else ''
    xelatex_alt = os.path.join(texdir, 'xelatex.exe') if texdir else ''
    xdvipdfmx_alt = os.path.join(texdir, 'xdvipdfmx.exe') if texdir else ''

    if cold:
        # 临时页码：编号章直接用书签页（准确），星号章取相邻编号章的近似值，
        # 解出真实页数/前置页数后只有星号章可能需要重编（通常 0–1 块）。
        starts = [1] * n
        for k, i in enumerate(numbered):
            starts[i] = outline[k]
        for i in range(n):
            if chunks[i]['starred']:
                nxt = min([j for j in numbered if j > i], default=None)
                prv = max([j for j in numbered if j < i], default=None)
                starts[i] = (max(1, starts[nxt] - 1) if nxt is not None
                             else (starts[prv] + 1 if prv is not None else 1))
        front = None
        pages_old = None
        old_starts = None
    else:
        front = cache['front']
        starts = [cached[i]['start'] for i in range(n)]
        pages_old = [cached[i]['pages'] for i in range(n)]
        old_starts = list(starts)
        if not changed and layout(pages_old, front) != starts:
            # 什么都没改却与记录的版式不符（前置页/分页假设不成立）→ 回落整本
            return _skip('缓存里的起始页与 openright 布局对不上')

    def compile_one(i, start_page):
        try:
            return _compile_chunk(book_dir, doc, i, start_page, chapter_no[i],
                                  env, texcmd, xelatex_alt, xdvipdfmx_alt)
        except Exception as ex:         # noqa: BLE001
            return {'ok': False, 'out': str(ex), 'pdf': '', 'synctex': '', 'secs': 0}

    def compile_chunks(todo, starts_for):
        if not todo:
            return []
        procs = []
        for i in todo:
            if log_cb:
                log_cb('[并行编译] %s（起始页 %d）' % (chunks[i]['id'], starts_for[i]))
            box = {}

            def work(ii=i, oo=starts_for[i], bx=box):
                bx['r'] = compile_one(ii, oo)

            th = threading.Thread(target=work)
            th.start()
            procs.append((i, th, box))
        errs = []
        for i, th, box in procs:
            th.join()
            r = box.get('r') or {'ok': False, 'out': '[并行编译] 无结果'}
            if not r['ok'] or not os.path.isfile(r['pdf']):
                errs.append((chunks[i]['id'], r['out']))
        return errs

    def compile_serially(todo, starts_for):
        """一块一块地编（并发失败后的兜底重试）。"""
        errs = []
        for i in todo:
            r = compile_one(i, starts_for[i])
            if not r['ok'] or not os.path.isfile(r['pdf']):
                errs.append((chunks[i]['id'], r['out']))
        return errs

    def rebuild_format():
        """便携 MiKTeX 的格式文件损坏/过期时，单独重建一次（必须串行）。"""
        mkm = os.path.join(texdir, 'miktex.exe') if texdir else ''
        if not mkm or not os.path.isfile(mkm):
            return False
        try:
            p = subprocess.run([mkm, '--disable-installer', 'formats', 'build',
                                'xelatex', '--engine', 'xetex'],
                               cwd=book_dir, timeout=180, capture_output=True, text=True,
                               encoding='utf-8', errors='replace',
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
                               env=env)
            return p.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def format_race(errs):
        """是否是「多个 xelatex 同时重建/抢占 xelatex.fmt」导致的启动级失败。"""
        for _, out in errs:
            s = out or ''
            if 'did not succeed' in s or 'Permission denied' in s or 'xelatex.fmt' in s:
                return True
        return False

    t0 = time.time()
    errors = compile_chunks(changed, starts)
    if errors and len(changed) > 1 and format_race(errors):
        # 2026-10-25：便携 MiKTeX 在缓存/配置变动后，首个 xelatex 会重建 xelatex.fmt；
        # 多个进程并发启动时只有一个能写、其余以 Permission denied 退出（被误判成块编译失败）。
        # 串行重建格式，再把失败的块逐个重试一遍。
        if log_cb:
            log_cb('[并行编译] 检测到便携 MiKTeX 格式文件争用，串行重建后重试失败块')
        rebuild_format()
        failed = [i for i in changed if chunks[i]['id'] in {c for c, _ in errors}]
        errors = compile_serially(failed, starts)
    if errors:
        if log_cb:
            for cid, out in errors:
                log_cb('[并行编译失败] %s\n%s' % (cid, (out or '')[-1500:]))
        return _skip('块编译失败：%s' % '; '.join(c for c, _ in errors))

    pages = {}
    for i in range(n):
        pages[i] = _pdf_pages(_chunk_pdf(book_dir, chunks[i]['id'])) if i in changed \
            else cached[i]['pages']
        if not isinstance(pages[i], int) or pages[i] < 1:
            return _skip('块 %s 的 PDF 页数读不出' % chunks[i]['id'])

    if cold:
        sol = solve_front(pages)
        if not sol:
            return _skip('冷启动反解前置页数失败（书签/总页数不自洽）')
        front, new_starts = sol
    else:
        new_starts = layout(pages, front)
    shifted = [i for i in range(n) if new_starts[i] != starts[i]]
    if shifted:
        if not changed:
            # 什么都没改却算出不同版式 ⇒ 假设不成立，别动成品
            return _skip('未改块却算出不同起始页')
        if log_cb:
            log_cb('[并行编译] 页码偏移变化，重编：%s'
                   % ', '.join(chunks[i]['id'] for i in shifted))
        errs = compile_chunks(shifted, new_starts)
        if errs:
            return _skip('页码平移重编失败')
        for i in shifted:
            if _pdf_pages(_chunk_pdf(book_dir, chunks[i]['id'])) != pages[i]:
                return _skip('重编后块页数仍不稳定（%s）' % chunks[i]['id'])
    starts = new_starts
    offsets = [s - 1 for s in starts]
    if cold:
        pages_old = list(pages)          # 冷启动时整本分页就是要复现的目标
        old_starts = list(starts)

    # 旧 PDF 页 → 新 PDF 页（书签目标用）。旧章起点来自缓存（= 生成当前 synctex 的边界）。
    old_total = _pdf_pages(main_pdf)
    page_map = {i: i for i in range(min(front, old_total or 0))}
    if old_total:
        for i in range(n):
            os_ = old_starts[i]
            for k in range(pages_old[i]):
                oi = os_ - 1 + k
                if 0 <= oi < old_total:
                    ni = starts[i] - 1 + min(k, pages[i] - 1)
                    page_map[oi] = ni

    # 合并 PDF：前置页取当前 main.pdf 的前 front 页；章间按起始页补空白页
    parts = [('pages', main_pdf, 0, front)]
    pos = front
    try:
        import pypdf
        for i in range(n):
            pdf = _chunk_pdf(book_dir, chunks[i]['id'])
            if starts[i] - 1 > pos:
                r = pypdf.PdfReader(pdf)
                mb = r.pages[0].mediabox
                parts.append(('blanks', starts[i] - 1 - pos,
                              (float(mb.width), float(mb.height))))
                pos = starts[i] - 1
            parts.append(('pages', pdf, 0, pages[i]))
            pos += pages[i]
    except Exception as ex:                 # noqa: BLE001
        return _skip('读取块 PDF 失败：%s' % ex)
    merged = os.path.join(book_dir, '.build', '_floria_main.pdf')
    try:
        _merge_pdfs(parts, merged, outline_from=main_pdf, page_map=page_map)
    except Exception as ex:                 # noqa: BLE001
        return _skip('拼 PDF/重建书签失败：%s' % ex)
    os.replace(merged, main_pdf)

    # 合并 synctex（页号偏移 + 行号平移回 main.tex）
    items = []
    for i in range(n):
        items.append((_chunk_sync(book_dir, chunks[i]['id']), offsets[i],
                      chunks[i]['id'] + '.tex', chunks[i]['line']))
    try:
        _merge_synctex(items, os.path.join(book_dir, 'main.synctex.gz'))
    except Exception as ex:                 # noqa: BLE001
        if log_cb:
            log_cb('[synctex 合并失败] %s' % ex)
        try:
            os.remove(os.path.join(book_dir, 'main.synctex.gz'))
        except OSError:
            pass
        # 不能带着「编译成功但没有同步数据」返回：回落整本 latexmk 重出 synctex。
        return _skip('synctex 合并失败：%s' % ex)

    newcache = []
    for i in range(n):
        newcache.append({'id': chunks[i]['id'], 'hash': hashes[i], 'built': hashes[i],
                         'pages': pages[i], 'start': starts[i]})
    _write_cache(book_dir, {'front': front, 'front_hash': _front_hash(doc),
                            'chunks': newcache, 'ts': round(time.time(), 3)})
    elapsed = round(time.time() - t0, 2)
    log = '\n'.join([
        '[编译链] 自动分章并行（%d 块，重编 %d 块，前置页 %d）%s'
        % (n, len(changed), front, ' · 冷启动（整本刚编过）' if cold else ''),
        '[编译链] 未变块复用缓存：%s'
        % (', '.join(chunks[i]['id'] for i in range(n) if i not in changed) or '无'),
        '[编译链] 拼接 %d 个 PDF（pypdf）+ 恢复书签树' % (n + 1),
        '[编译链] 各块起始页：%s'
        % ' / '.join('%s=%d' % (chunks[i]['id'], starts[i]) for i in range(n)),
    ])
    if any(pages[i] != pages_old[i] for i in range(n)):
        log += '\n[编译链] 块页数有变动：目录页码沿用上次整本编译，可能已过期（需要时用整本编译刷新）'
    return {'ok': True, 'time': elapsed, 'mtime': int(os.path.getmtime(main_pdf) * 1000),
            'pages': _pdf_pages(main_pdf), 'log': log, 'fast': True}


def refresh_cache(book_dir):
    """整本编译成功后调用：只登记「这本书可以走按块编译」+ 当前各块内容 hash。

    页数/起始页**不在这里猜**：整本 PDF 只能可靠拿到编号章的书签页（`\\chapter*`
    没有书签），而 synctex 的行号会因为目录超链接指向章首、以及目录末几页被推迟
    shipout 而失真。真正的页数由第一次快速编译并行编出各块后，
    用「编号章书签页 + 总页数」反解前置页数与 openright 补白（见 try_compile）。
    """
    doc = split_document(book_dir)
    if not doc:
        return None
    main_pdf = os.path.join(book_dir, 'main.pdf')
    if not os.path.isfile(main_pdf):
        return None
    out = [{'id': c['id'], 'hash': _chunk_hash(doc['preamble'], c),
            'built': None, 'pages': None, 'start': None} for c in doc['chunks']]
    _write_cache(book_dir, {'front': None, 'front_hash': _front_hash(doc),
                            'chunks': out, 'ts': round(time.time(), 3)})
    return {'front': None, 'chunks': len(out)}
