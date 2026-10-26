# -*- coding: utf-8 -*-
"""整本单文件 main.tex 的自动分章并行编译（2026-10-22 改版）。

用户要求：**源文件层次永远只有一本 main.tex**。切分全部发生在 `.build/` 里：
  1. 读 main.tex，按顶层 `\\chapter`（必换页）切成若干块；
  2. 每块 + 原导言区生成 wrapper，独立 xelatex 编译（可并行），
     注入页码起点与章号，使页脚/图表编号与整本一致；
  3. 用 pypdf 把「上次整本编译留下的前置页（目录）」+ 各块 PDF 按 openright
     规则补齐空白页拼回 main.pdf，并合并 synctex（行号平移回 main.tex）。

未改动的块按内容 md5 复用上次 PDF。try_compile 始终返回结构化结果：
`status='ok' | 'unsupported' | 'failed'` + 稳定 `reason_code`——
`unsupported` 由 compiler 明确选择整本模式，`failed` 直接报错、不再隐式回落。
"""
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time

CACHE_NAME = '.floria_fast.json'
F_DIR = os.path.join('.build', 'floria')
CHUNK_DIR = os.path.join(F_DIR, 'chunks')
OUT_DIR = os.path.join(F_DIR, 'out')
FRONT_SYNC = os.path.join(F_DIR, 'front.synctex.gz')      # 整本编译留下的前置页基线
FRONT_META = os.path.join(F_DIR, 'front.meta.json')       # 基线对应的前置页内容 hash
STATUS_OK = 'ok'
STATUS_UNSUPPORTED = 'unsupported'     # 结构/环境不适用：明确选择整本模式
STATUS_FAILED = 'failed'               # 快速路径真实失败：直接报错，不再回落
LAST_SKIP = ''                    # 最近一次未走快速路径的人话原因（兼容 shim）
LAST_REASON = {}                  # {status, code, message}
LAST_PHASES = {}                  # 最近一次快速编译的耗时分解
LAST_CHUNKS = {}                  # {total, rebuilt, reused}
MAX_PARALLEL = max(1, min(4, os.cpu_count() or 2))   # 并行编译块数上限（避免 CPU 叠爆）

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
    return _split_document(book_dir)[0]


def _split_document(book_dir):
    """同 split_document，但附带不适用原因码：返回 (doc|None, reason_code|None)。"""
    main_tex = os.path.join(book_dir, 'main.tex')
    try:
        with open(main_tex, encoding='utf-8', errors='replace') as f:
            src = f.read()
    except OSError:
        return None, 'no_main_tex'
    b = _find_cmd(src, '\\begin{document}')
    if b < 0:
        return None, 'no_begin_document'
    brace = src.find('}', b)
    if brace < 0:
        return None, 'no_begin_document'
    body_start = brace + 1
    e = _find_cmd(src, '\\end{document}', body_start)
    body_end = e if e > b else len(src)
    preamble = src[:b]                       # 不含 \begin{document}
    body = src[body_start:body_end]

    # 附录 / 分卷会改变章编号规则，自动切分不适用 → 由调用方明确选择整本模式
    # 注意要检查下一个字符：`\partial` 也以 `\part` 开头（MATH 里到处都是）。
    if _has_cmd(body, '\\appendix'):
        return None, 'has_appendix'
    if _has_cmd(body, '\\part'):
        return None, 'has_part'

    marks = []
    for m in re.finditer(r'\\chapter\b(\s*\*)?', body):
        if _commented(body, m.start()):
            continue
        marks.append((m.start(), bool(m.group(1))))
    if len(marks) < 2:
        return None, 'single_chapter'

    line0 = src.count('\n', 0, body_start) + 1
    chunks = []
    for i, (pos, starred) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(body)
        chunks.append({'id': 'ch%d' % (i + 1),
                       'text': body[pos:end],
                       'line': line0 + body.count('\n', 0, pos),
                       'starred': starred})
    return {'preamble': preamble, 'front': body[:marks[0][0]], 'chunks': chunks}, None


def _chunk_hash(preamble, chunk):
    h = hashlib.md5()
    h.update(preamble.encode('utf-8', 'replace'))
    h.update(b'\0')
    h.update(chunk['text'].encode('utf-8', 'replace'))
    return h.hexdigest()


def _front_hash(doc):
    """前置页（封面/目录）的输入指纹 = 导言区 + 第一章之前的内容。

    前置页由同一份导言区排版：只改页边距/字体/hyperref 也会让旧前置页与新正文
    不是一套版式，所以导言区必须计入，否则改了导言区的快速编译会给出「半个新版」。
    """
    h = hashlib.md5()
    h.update(doc['preamble'].encode('utf-8', 'replace'))
    h.update(b'\0')
    h.update(doc['front'].encode('utf-8', 'replace'))
    return h.hexdigest()


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


def sync_pages(path):
    """synctex 里出现过的页号集合（只扫页块标记，不解析记录）；失败返回 None。"""
    pages = set()
    try:
        with gzip.open(path, 'rt', encoding='utf-8', errors='replace') as f:
            for line in f:
                if line.startswith('{'):
                    try:
                        pages.add(int(line[1:]))
                    except ValueError:
                        pass
    except (OSError, EOFError):
        return None
    except Exception:                       # noqa: BLE001  zlib.error 等损坏
        return None
    return pages


def front_baseline_ok(book_dir, front):
    """前置页基线是否可用：文件存在且 1..front 每页都有映射。"""
    if not front:
        return True
    path = os.path.join(book_dir, FRONT_SYNC)
    if not os.path.isfile(path):
        return False
    pages = sync_pages(path)
    if pages is None:
        return False
    return all(p in pages for p in range(1, int(front) + 1))


def _read_front_meta(book_dir):
    try:
        with open(os.path.join(book_dir, FRONT_META), encoding='utf-8') as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def front_hash_of(book_dir):
    """当前 main.tex 前置页正文的 hash（拆分失败返回 None）。"""
    doc = split_document(book_dir)
    return _front_hash(doc) if doc else None


def ensure_front_baseline(book_dir, front, front_hash=None):
    """确保前置页基线可用；缺了就尝试**就地采纳**现有整本 synctex。

    返回 (可用, 是否刚采纳)。已有可用基线不动；`main.synctex.gz` 若覆盖 1..front
    （说明它来自整本编译，而不是旧的按章合并）就直接复制成基线——省掉一次整本重编。
    只有现有 synctex 也不含前置页时才返回 False，由调用方回落整本编译。

    前置页正文（封面/目录）改过、或前置页数变了 ⇒ 旧基线的行号已失效，必须整本重编。
    侧车缺失/没记 hash 也算过期（无法证明基线对应当前正文），宁可整本重编一次。
    """
    if not front:
        return True, False
    meta = _read_front_meta(book_dir)
    # 没有 sidecar（或没记 front_hash）就无法证明现有前置页对应现在的封面/目录正文，
    # 一律当过期：否则改了前置页正文，快速路径仍会从旧 main.pdf 搬旧的前置页，
    # 编出来的 PDF 看起来「没有任何变化」（2026-10-26 PHYSIC 实测）。
    stale = (not meta or meta.get('front_hash') is None) or (
        (front_hash is not None and meta.get('front_hash') != front_hash)
        or (meta.get('front') is not None and int(meta['front']) != int(front)))
    if not stale and front_baseline_ok(book_dir, front):
        return True, False
    if stale:
        return False, False
    src = os.path.join(book_dir, 'main.synctex.gz')
    pages = sync_pages(src) if os.path.isfile(src) else None
    if pages and all(p in pages for p in range(1, int(front) + 1)):
        return save_front_baseline(book_dir, front, front_hash), True
    return False, False


def _front_content(d, max_page):
    """只保留「整页块」页码 ≤ max_page 的记录（含 {N/}N 标记，成对搬运）。

    前置页（封面/目录）没有对应的分块，必须从整本编译留下的基线里按页块原样搬，
    否则整块内容被丢掉，点目录页会报「本页无源码映射」。
    返回 (保留的行, 实际保留的页数)。
    """
    out, page, keep = [], 0, False
    kept = set()
    for line in d['content']:
        if not line:
            continue
        c = line[0]
        if c == '{':
            try:
                page = int(line[1:])
            except ValueError:
                page = 0
            keep = 0 < page <= max_page
            if keep:
                kept.add(page)
        elif c == '}':
            if keep:
                out.append(line)
            page, keep = 0, False
            continue
        if keep:
            out.append(line)
    return out, len(kept)


def _merge_synctex(items, out):
    """items = [{path, offset, chunk_name, chunk_line, max_page}]。

    块文件的 Input 路径改写成 `main.tex`，记录行号按块在 main.tex 里的起始行平移
    （chunk 第 1 行 = main.tex 第 chunk_start_line 行）。这样点 PDF 仍跳回 main.tex。
    `max_page` 不为 None 的项只搬前 max_page 页（前置页基线，行号不平移）。
    """
    out_lines = ['SyncTeX Version:1']
    body = []
    tag_base = 0
    hdr = {}
    total_pages = 0
    for item in items:
        with gzip.open(item['path'], 'rt', encoding='utf-8', errors='replace') as f:
            d = _parse_synctex(f.read())
        # 头部参数必须一致，否则合并出的坐标全错 → 让调用方回落整本
        for k, v in d['hdr'].items():
            if k in hdr and hdr[k] != v:
                raise ValueError('synctex 头部参数不一致（%s: %s ≠ %s）' % (k, hdr[k], v))
            hdr.setdefault(k, v)
        offset = item.get('offset') or 0
        chunk_name = item.get('chunk_name')
        chunk_line = item.get('chunk_line') or 0
        max_page = item.get('max_page')
        if max_page is not None:
            content, kept = _front_content(d, max_page)
            if kept < max_page:
                raise ValueError('前置页同步映射不完整（%d/%d）' % (kept, max_page))
            npages = max_page
        else:
            content, npages = d['content'], d['pages']
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
        for line in content:
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
        total_pages = max(total_pages, offset + npages)
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


def save_front_baseline(book_dir, front=None, front_hash=None):
    """整本编译成功后调用：把 main.synctex.gz 留作前置页基线（快速路径永不覆盖它）。

    先验真：synctex 必须真的覆盖 1..front（否则下次仍被判不可用，白留一份坏基线）。
    `front_hash` 记进 sidecar，前置页正文一改就判定基线过期。
    """
    src = os.path.join(book_dir, 'main.synctex.gz')
    dst = os.path.join(book_dir, FRONT_SYNC)
    if not os.path.isfile(src):
        return False
    if front is None:
        try:
            outline = _outline_starts(os.path.join(book_dir, 'main.pdf'))
            front = max(0, int(outline[0]) - 1) if outline else 0
        except Exception:                   # noqa: BLE001
            front = 0
    if front:
        pages = sync_pages(src)
        if not pages or not all(p in pages for p in range(1, int(front) + 1)):
            return False
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(src, dst)
    except OSError:
        return False
    try:
        with open(os.path.join(book_dir, FRONT_META), 'w', encoding='utf-8') as f:
            json.dump({'front': front, 'front_hash': front_hash,
                       'ts': round(time.time(), 3)}, f, ensure_ascii=False)
    except OSError:
        pass
    return True


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


def _result(status, code, message, **extra):
    """结构化退出：unsupported = 明确走整本；failed = 快速路径真实失败。"""
    global LAST_SKIP, LAST_REASON
    LAST_SKIP = message
    LAST_REASON = {'status': status, 'code': code, 'message': message}
    r = {'status': status, 'reason_code': code, 'reason': message}
    r.update(extra)
    return r


def _skip(code, message):
    """结构/环境不适用：由 compiler 明确选择整本模式。"""
    return _result(STATUS_UNSUPPORTED, code, message)


def _fail(code, message, **extra):
    """快速路径真实失败：compiler 直接报错，不再隐式整本重编。"""
    return _result(STATUS_FAILED, code, message, **extra)


def try_compile(book_dir, book_name, mk, perl, env, log_cb=None):
    """快速路径。始终返回结构化结果（status/reason_code/reason + 成功时同旧字段）。"""
    global LAST_SKIP, LAST_REASON, LAST_PHASES, LAST_CHUNKS
    LAST_SKIP = ''
    LAST_REASON = {}
    LAST_PHASES, LAST_CHUNKS = {}, {}
    doc, shape_reason = _split_document(book_dir)
    if not doc:
        return _skip(shape_reason, {
            'single_chapter': '顶层 \\chapter 少于 2 个',
            'has_appendix': '文档含 \\appendix',
            'has_part': '文档含 \\part',
            'no_main_tex': '找不到可读的 main.tex',
            'no_begin_document': 'main.tex 里没有 \\begin{document}',
        }.get(shape_reason, '结构不适用自动分章'))
    try:
        import pypdf  # noqa: F401
    except Exception as ex:                 # noqa: BLE001
        return _skip('missing_pypdf', '缺少 pypdf：%s' % ex)
    cache = read_cache(book_dir)
    main_pdf = os.path.join(book_dir, 'main.pdf')
    if not os.path.isfile(main_pdf):
        return _skip('no_main_pdf', '还没有 main.pdf（需先整本编译一次）')
    chunks = doc['chunks']
    n = len(chunks)
    cached = cache.get('chunks') or [] if cache else []
    if not cache or len(cached) != n or cache.get('front_hash') != _front_hash(doc):
        # 冷启动：缓存不可用（首次 / 改导言区 / 增删章）但 main.pdf 还在
        # ⇒ 直接全块并行重建，不再回落整本 latexmk（2026-10-25 定案）。
        cache = {'front': None, 'front_hash': _front_hash(doc),
                 'chunks': [{'id': c['id'], 'built': None, 'pages': None, 'start': None}
                            for c in chunks]}
        cached = cache['chunks']

    # 编号章（\chapter）在 PDF 里必有书签 → 起始页可信；\chapter* 没书签，靠布局反解。
    numbered = [i for i, c in enumerate(chunks) if not c['starred']]
    outline = _outline_starts(main_pdf)
    total_pdf = _pdf_pages(main_pdf)
    if not outline or len(outline) != len(numbered) or not total_pdf:
        return _skip('outline_mismatch', 'PDF 书签数与编号章数不一致，或页数读不出'
                     '（多为 main.pdf 已过期，需要一次整本编译）')

    guess_front = cache.get('front')
    if guess_front is None and outline:
        guess_front = max(0, outline[0] - 1)
    if guess_front:
        ok_front, _adopted = ensure_front_baseline(book_dir, guess_front,
                                                   _front_hash(doc))
        if not ok_front:
            return _skip('front_baseline_missing',
                         '前置页基线与当前封面/目录不匹配（正文改过、'
                         '基线缺失或已过期；需整本编译一次重建前置页）')

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
            return _skip('cache_layout_mismatch', '缓存里的起始页与 openright 布局对不上')

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
        sem = threading.Semaphore(MAX_PARALLEL)
        for i in todo:
            if log_cb:
                log_cb('[并行编译] %s（起始页 %d）' % (chunks[i]['id'], starts_for[i]))
            box = {}

            def work(ii=i, oo=starts_for[i], bx=box, sm=sem):
                with sm:
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
        return _fail('chunk_compile_failed',
                     '块编译失败：%s' % '; '.join(c for c, _ in errors))

    t_chunks = time.time()
    pages = {}
    for i in range(n):
        pages[i] = _pdf_pages(_chunk_pdf(book_dir, chunks[i]['id'])) if i in changed \
            else cached[i]['pages']
        if not isinstance(pages[i], int) or pages[i] < 1:
            return _fail('chunk_pages_unreadable',
                         '块 %s 的 PDF 页数读不出' % chunks[i]['id'])

    if cold:
        sol = solve_front(pages)
        if not sol:
            return _fail('front_solve_failed', '冷启动反解前置页数失败（书签/总页数不自洽）')
        front, new_starts = sol
    else:
        new_starts = layout(pages, front)
    shifted = [i for i in range(n) if new_starts[i] != starts[i]]
    if shifted:
        if not changed:
            # 什么都没改却算出不同版式 ⇒ 假设不成立，别动成品
            return _fail('page_shift_failed', '未改块却算出不同起始页')
        if log_cb:
            log_cb('[并行编译] 页码偏移变化，重编：%s'
                   % ', '.join(chunks[i]['id'] for i in shifted))
        errs = compile_chunks(shifted, new_starts)
        if errs:
            return _fail('page_shift_failed', '页码平移重编失败')
        for i in shifted:
            if _pdf_pages(_chunk_pdf(book_dir, chunks[i]['id'])) != pages[i]:
                return _fail('unstable_pages', '重编后块页数仍不稳定（%s）' % chunks[i]['id'])
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
        return _fail('read_chunk_pdf_failed', '读取块 PDF 失败：%s' % ex)
    t_pdf0 = time.time()
    merged = os.path.join(book_dir, '.build', '_floria_main.pdf')
    try:
        _merge_pdfs(parts, merged, outline_from=main_pdf, page_map=page_map)
    except Exception as ex:                 # noqa: BLE001
        return _fail('merge_pdf_failed', '拼 PDF/重建书签失败：%s' % ex)
    t_pdf = time.time()

    # 合并 synctex 到临时文件（前置页基线 + 各块：页号偏移 + 行号平移回 main.tex）
    sync_out = os.path.join(book_dir, 'main.synctex.gz')
    sync_tmp = os.path.join(book_dir, '.build', '_floria_main.synctex.gz')
    items = []
    front_sync = os.path.join(book_dir, FRONT_SYNC)
    if front and os.path.isfile(front_sync):
        items.append({'path': front_sync, 'offset': 0, 'chunk_name': None,
                      'chunk_line': 0, 'max_page': front})
    for i in range(n):
        items.append({'path': _chunk_sync(book_dir, chunks[i]['id']), 'offset': offsets[i],
                      'chunk_name': chunks[i]['id'] + '.tex', 'chunk_line': chunks[i]['line'],
                      'max_page': None})
    t_sync0 = time.time()
    try:
        _merge_synctex(items, sync_tmp)
    except Exception as ex:                 # noqa: BLE001
        if log_cb:
            log_cb('[synctex 合并失败] %s' % ex)
        return _fail('synctex_merge_failed', 'synctex 合并失败：%s' % ex)
    # PDF 与 synctex 都合并成功后才落盘：任一失败都不替换成品，
    # 避免出现「PDF 已换新、点开却全无映射」的撕裂状态。
    try:
        os.replace(merged, main_pdf)
        os.replace(sync_tmp, sync_out)
    except OSError as ex:
        return _fail('commit_failed', '替换成品失败：%s' % ex)
    t_sync = time.time()
    LAST_PHASES = {'chunks': round(t_chunks - t0, 2),
                   'pdf_merge': round(t_pdf - t_pdf0, 2),
                   'synctex': round(t_sync - t_sync0, 2)}
    LAST_CHUNKS = {'total': n, 'rebuilt': len(set(changed) | set(shifted)),
                   'reused': n - len(set(changed) | set(shifted))}

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
    return {'status': STATUS_OK, 'reason_code': None, 'reason': None,
            'ok': True, 'time': elapsed, 'mtime': int(os.path.getmtime(main_pdf) * 1000),
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
