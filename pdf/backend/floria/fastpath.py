# -*- coding: utf-8 -*-
"""整本单文件 main.tex 的自动分章并行编译（2026-10-27：只保留快速路径）。

用户要求：**源文件层次永远只有一本 main.tex**，也**不再有整本 latexmk 兜底**。
切分全部发生在 `.build/` 里：
  1. 读 main.tex，按顶层 `\\chapter` / `\\chapter*` / `\\part`（必换页）切成若干块；
     第一个切点之前是前置页（封面/目录）；一个切点都没有时整篇正文算一块；
  2. 每块 + 原导言区生成 wrapper，独立 xelatex 编译（可并行），注入页码起点、
     章号、分卷号，使页脚/图表编号与整本一致；wrapper 里藏一个 `\\@starttoc{toc}`
     把 `.toc` 写流打开，于是每块都产出带**绝对页码**的目录条目；
  3. 各块目录条目拼成整本目录，前置页用同一份导言区 + 拼好的目录单独编出来
     （封面/目录不再依赖上一次整本编译的成品）；
  4. 用 pypdf 把前置页 + 各块 PDF 按 openright 规则补齐空白页拼回 main.pdf，
     按目录条目重建书签树，并合并 synctex（行号平移回 main.tex）。

未改动的块按内容 md5 复用上次 PDF。try_compile 始终返回结构化结果
（`status='ok' | 'failed'` + 稳定 `reason_code`）：真实失败直接报错。
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

from . import log

CACHE_NAME = '.floria_fast.json'
F_DIR = os.path.join('.build', 'floria')
CHUNK_DIR = os.path.join(F_DIR, 'chunks')
OUT_DIR = os.path.join(F_DIR, 'out')
FRONT_ID = 'front'                                         # 前置页在 out/ 下的块名
TOC_NAME = 'wrapper.toc'                                   # 每块编出的目录条目（绝对页码）
LAST_OK = os.path.join(F_DIR, 'last_ok.json')             # 上次成功编译的源码指纹（无改动直接复用）
STATUS_OK = 'ok'
STATUS_FAILED = 'failed'               # 真实失败：直接报错（整本兜底已删除）
LAST_SKIP = ''                    # 最近一次未走快速路径的人话原因（兼容 shim）
LAST_REASON = {}                  # {status, code, message}
LAST_PHASES = {}                  # 最近一次快速编译的耗时分解
LAST_CHUNKS = {}                  # {total, rebuilt, reused}
# 并行编译块数上限：一章约 3–7s（两遍 xelatex + xdvipdfmx），9 块 4 并发要排 3 轮
# ≈ 22s；放到 9 路一轮跑完 ≈ 7–9s（2026-10-26 CHEMISTERY 实测），仍留出余量给
# 浏览器/iPad 与 xdvipdfmx 的内存峰值。CPU 少的机器按核数收敛。
MAX_PARALLEL = max(2, min(9, os.cpu_count() or 4))

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
    """把 main.tex 切块。返回 {preamble, front, front_line, chunks:[…]} 或 None。

    切点 = 顶层 `\\chapter` / `\\chapter*` / `\\part`（都必换页），独立编译版面与
    整本一致；`\\section` 不换页，按它切会让断行/浮动体/页码整体漂移。
    没有任何切点时整篇正文当一块（单章 / 无章书稿）。
    """
    return _split_document(book_dir)[0]


def _split_document(book_dir):
    """同 split_document，但附带不可编译原因码：返回 (doc|None, reason_code|None)。"""
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
    body = src[body_start:body_end] if body_end >= body_start else ''

    # 附录把章编号改成字母并重置计数器，按章切块无法复现 → 明确报错
    if _has_cmd(body, '\\appendix'):
        return None, 'has_appendix'

    line0 = src.count('\n', 0, body_start) + 1
    marks = []                               # (pos, kind)  kind: chapter / part / star
    pat = r'(?P<chap>\\chapter\b(\s*\*)?)|(?P<part>\\part\b(\s*\*)?)'
    for m in re.finditer(pat, body):
        if _commented(body, m.start()):
            continue
        if m.group('chap') is not None:       # \chapter 或 \chapter*
            marks.append((m.start(), 'star' if '*' in m.group('chap') else 'chapter'))
        else:                                 # \part 或 \part*
            marks.append((m.start(), 'part'))

    if not marks:                            # 单章 / 无章：整篇正文一块，没有前置页
        return {'preamble': preamble, 'front': '', 'front_line': line0,
                'chunks': [{'id': 'ch1', 'text': body, 'line': line0,
                            'starred': True, 'parts': 0}]}, None

    chunks = []
    parts = 0
    for i, (pos, kind) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(body)
        chunks.append({'id': 'ch%d' % (i + 1),
                       'text': body[pos:end],
                       'line': line0 + body.count('\n', 0, pos),
                       'starred': kind != 'chapter',   # 只有 \chapter 推进章号
                       'parts': parts})
        if kind == 'part':
            parts += 1
    return {'preamble': preamble, 'front': body[:marks[0][0]],
            'front_line': line0, 'chunks': chunks}, None


def _front_hash(preamble, front, toc_text):
    """前置页指纹：导言区 + 第一章之前正文 + 种进去的整本目录。

    目录文字一变（增删章节、条目页码变化），前置页就必须重编；
    只改正文某章而目录不变时可以直接复用上一次的前置页。
    """
    h = hashlib.md5()
    h.update((preamble or '').encode('utf-8', 'replace'))
    h.update(b'\0')
    h.update((front or '').encode('utf-8', 'replace'))
    h.update(b'\0')
    h.update((toc_text or '').encode('utf-8', 'replace'))
    return h.hexdigest()


def _seed_from_synctex(book_dir, chunks):
    """冷缓存时用现有 main.synctex.gz 反推 (前置页数, 各块起始页)。

    合并后的 main.synctex.gz 里各块记录已平移回 main.tex 行号、页码已加偏移。
    **不能直接取「落在块行区间内的最小页码」**：目录页上的 hyperref 链接会把章首行号
    记到目录页（与 `_outline_starts` 踩的坑同源）。所以按「每页记录条数」判定——
    目录页对某章只有一两条链接记录，章页则有几十条。取第一/最后一张记录够多的页。
    `\\chapter*`（无书签）也能定位。估错只会多跑一轮，不影响正确性。
    """
    path = os.path.join(book_dir, 'main.synctex.gz')
    if not os.path.isfile(path):
        return None
    bounds = [c['line'] for c in chunks] + [10 ** 9]
    hits = [{} for _ in chunks]             # 块 → {页码: 记录条数}
    tags = {}
    page = 0
    try:
        with gzip.open(path, 'rt', encoding='utf-8', errors='replace') as f:
            for ln in f:
                if ln.startswith('Input:'):
                    # Input 是一张 tag → 路径的表；本链合并出的写作 `main.tex`，
                    # 整本 latexmk 留下的旧文件是绝对路径，按 basename 一起认。
                    try:
                        tag, p = ln[6:].split(':', 1)
                        tags[int(tag)] = os.path.basename(p.strip().replace('\\', '/'))
                    except (ValueError, IndexError):
                        pass
                    continue
                if ln.startswith('{'):
                    try:
                        page = int(ln[1:])
                    except ValueError:
                        page = 0
                    continue
                if not page:
                    continue
                m = _REC.match(ln)
                if not m:
                    continue
                try:
                    if tags.get(int(m.group(2))) != 'main.tex':
                        continue
                    line = int(m.group(3))
                except (ValueError, KeyError):
                    continue
                if line < bounds[0]:
                    continue
                for i in range(len(chunks)):
                    if line < bounds[i]:
                        break
                    if line < bounds[i + 1]:
                        hits[i][page] = hits[i].get(page, 0) + 1
                        break
    except (OSError, EOFError):
        return None
    except Exception:                       # noqa: BLE001  zlib.error 等损坏
        return None

    def solid(pages):
        """记录条数够多的页里取第一张；过滤掉目录页上指向该章的零星链接。"""
        if not pages:
            return None
        mx = max(pages.values())
        good = [p for p, n in pages.items() if n >= max(3, mx * 0.05)]
        return min(good) if good else None

    starts = []
    for h in hits:
        s = solid(h)
        if s is not None and s % 2 == 0:
            # openright 下章首必为奇数页：偶数是「章题页记录太少」被阈值挡掉的假首页
            if h.get(s - 1, 0) >= 2:
                s -= 1
            elif h.get(s + 1, 0) >= 2:
                s += 1
        starts.append(s)
    if any(s is None or s % 2 == 0 for s in starts):
        return None
    if starts != sorted(set(starts)):
        return None
    # 前置页数取章首页 - 1（偶数）：与 layout() 的 openright 方程自洽。
    # 差一张空白页时只多编一次前置页，不影响最终页码。
    front = max(0, starts[0] - 1)
    return front, starts


def _seed_from_pdf(book_dir, chunks):
    """再用书签页兜底（只有 main.pdf、没有 synctex 时）。

    只认**全是编号 `\\chapter`** 的书稿：书签页就是各章起始页。
    """
    if any(c.get('starred') or c.get('parts') for c in chunks):
        return None
    pdf = os.path.join(book_dir, 'main.pdf')
    if not os.path.isfile(pdf):
        return None
    pages = _outline_starts(pdf)
    if not pages or len(pages) != len(chunks):
        return None
    if any(p % 2 == 0 for p in pages):
        return None
    front = pages[0] - 1                 # 章首必为奇数页 ⇒ 前置页取偶数页数
    if front < 0:
        return None
    return front, list(pages)


def _chunk_hash(preamble, chunk):
    h = hashlib.md5()
    h.update(preamble.encode('utf-8', 'replace'))
    h.update(b'\0')
    h.update(chunk['text'].encode('utf-8', 'replace'))
    return h.hexdigest()


def source_fingerprint(book_dir):
    """源码指纹：main.tex 全文 + image/ 下每个文件的 (名字, 大小, mtime)。

    用它判断「什么都没改」：指纹相同且成品还是上次那份，就直接复用，0 秒返回。
    仅用 main.tex 的 mtime 不够（换图、改图不改 tex 也要重编）。
    """
    h = hashlib.md5()
    try:
        with open(os.path.join(book_dir, 'main.tex'), 'rb') as f:
            h.update(f.read())
    except OSError:
        return None
    img = os.path.join(book_dir, 'image')
    try:
        names = sorted(os.listdir(img))
    except OSError:
        names = []
    for name in names:
        p = os.path.join(img, name)
        try:
            st = os.stat(p)
        except OSError:
            continue
        if not os.path.isfile(p):
            continue
        h.update(b'\0')
        h.update(name.encode('utf-8', 'replace'))
        h.update(('%d:%d' % (st.st_size, st.st_mtime_ns)).encode('ascii'))
    return h.hexdigest()


def read_last_ok(book_dir):
    try:
        with open(os.path.join(book_dir, LAST_OK), encoding='utf-8') as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def write_last_ok(book_dir, src=None, pages=None):
    """成功编译后登记源码指纹与成品 mtime，供下次「无改动直接复用」。"""
    main_pdf = os.path.join(book_dir, 'main.pdf')
    syn = os.path.join(book_dir, 'main.synctex.gz')
    try:
        st = os.stat(main_pdf)
        syn_st = os.stat(syn)
    except OSError:
        return False
    rec = {'src': src or source_fingerprint(book_dir),
           'pdf_mtime_ns': st.st_mtime_ns, 'pdf_size': st.st_size,
           'synctex_mtime_ns': syn_st.st_mtime_ns,
           'pages': pages, 'ts': round(time.time(), 3)}
    try:
        os.makedirs(os.path.dirname(os.path.join(book_dir, LAST_OK)), exist_ok=True)
        tmp = os.path.join(book_dir, LAST_OK) + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(rec, f, ensure_ascii=False)
        os.replace(tmp, os.path.join(book_dir, LAST_OK))
    except OSError:
        return False
    return True


def unchanged_output(book_dir, src):
    """源码没变且成品就是上次那份 ⇒ 返回现有 PDF 页数；否则 None。"""
    last = read_last_ok(book_dir)
    if not last or not src or last.get('src') != src:
        return None
    for name in ('main.pdf', 'main.synctex.gz'):
        if not os.path.isfile(os.path.join(book_dir, name)):
            return None
    try:
        st = os.stat(os.path.join(book_dir, 'main.pdf'))
    except OSError:
        return None
    if last.get('pdf_mtime_ns') != st.st_mtime_ns or last.get('pdf_size') != st.st_size:
        return None
    pages = last.get('pages')
    return pages if isinstance(pages, int) else 0


def _pages_of(pdf):
    """PDF 页数：pypdf 读 xref（~10ms），比每条 spawn 一个 pdfinfo（~0.3s）快 30 倍。

    快速路径每轮要数 9 个块 + 成品，进程启动开销累计 2–3s，是「plan」阶段的
    主要耗时（2026-10-26 实测）。
    """
    try:
        import pypdf
        return pypdf.PdfReader(pdf).get_num_pages()
    except Exception:                       # noqa: BLE001
        return None


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


def _merge_pdfs(parts, out, outline=None):
    """parts = [('pages', path, a, b) | ('blanks', n, (w, h))]，拼成 out（原子替换）。

    `outline` = [{level,title,page}]（page 为合并后的 1 基绝对页码，取自目录条目），
    按 part → chapter → section → subsection 嵌套重建书签树。
    """
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
    ranks = {'part': 0, 'chapter': 1, 'section': 2, 'subsection': 3}
    parents = {}
    for e in outline or []:
        r = ranks.get(e.get('level'))
        if r is None:
            continue
        idx = int(e.get('page') or 0) - 1
        if idx < 0 or idx >= len(w.pages):
            continue
        for k in list(parents):
            if k >= r:                  # 同名层级及其子层级的旧父项作废
                del parents[k]
        parent = parents.get(r - 1) if r else None
        try:
            parents[r] = w.add_outline_item(e.get('title') or '', idx, parent=parent)
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
    return len(w.pages)             # 成品页数顺带返回，省得再开一次 pypdf 把整本读一遍


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


def _merge_key(items):
    """合并输入指纹：各块 synctex 内容 + 页偏移 + 块名 + 行平移，全一致则结果必然一致。"""
    h = hashlib.md5()
    for it in items:
        try:
            with open(it['path'], 'rb') as f:
                h.update(hashlib.md5(f.read()).digest())
        except OSError:
            h.update(b'?')
        h.update(('%d|%s|%d\n' % (it.get('offset') or 0, it.get('chunk_name') or '',
                                  it.get('chunk_line') or 0)).encode('utf-8'))
    return h.hexdigest()


def _merge_synctex_cached(items, out):
    """带缓存的 `_merge_synctex`：输入全没变就拷上次成品（0.3–1.4s → ~0.02s）。"""
    book_dir = os.path.dirname(os.path.dirname(out))
    cache = os.path.join(book_dir, F_DIR, 'synctex_merged.gz')
    sidecar = cache + '.key'
    key = _merge_key(items)
    try:
        with open(sidecar, 'r', encoding='utf-8') as f:
            if f.read().strip() == key and os.path.isfile(cache):
                shutil.copyfile(cache, out)
                return False                # 命中：没有真的合并
    except OSError:
        pass
    _merge_synctex(items, out)
    try:
        shutil.copyfile(out, cache)
        with open(sidecar, 'w', encoding='utf-8') as f:
            f.write(key)
    except OSError as ex:                   # noqa: BLE001  缓存写失败不该影响编译
        log.warn('写 synctex 合并缓存失败：%s' % ex)
    return True


def _merge_synctex(items, out):
    """items = [{path, offset, chunk_name, chunk_line}]。

    块文件的 Input 路径改写成 `main.tex`，记录行号按块在 main.tex 里的起始行平移
    （chunk 第 1 行 = main.tex 第 chunk_start_line 行）。这样点 PDF 仍跳回 main.tex。
    前置页同样以块身份参与（offset=0），目录页也能跳回源码。
    """
    out_lines = ['SyncTeX Version:1']
    body = []
    tag_base = 0
    hdr = {}
    total_pages = 0
    for item in items:
        with gzip.open(item['path'], 'rt', encoding='utf-8', errors='replace') as f:
            d = _parse_synctex(f.read())
        # 头部参数必须一致，否则合并出的坐标全错
        for k, v in d['hdr'].items():
            if k in hdr and hdr[k] != v:
                raise ValueError('synctex 头部参数不一致（%s: %s ≠ %s）' % (k, hdr[k], v))
            hdr.setdefault(k, v)
        offset = item.get('offset') or 0
        chunk_name = item.get('chunk_name')
        chunk_line = item.get('chunk_line') or 0
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
    # compresslevel=6：默认 9 在十几万行上要多花约 0.09s，产物反而不比 6 小。
    with gzip.open(tmp, 'wt', encoding='utf-8', compresslevel=6) as f:
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


def _toc_path(book_dir, cid):
    return os.path.join(_chunk_dir(book_dir, cid), TOC_NAME)


def _read_toc(book_dir, cid):
    """读某块编出的目录条目；文件不存在返回 None。"""
    try:
        with open(_toc_path(book_dir, cid), encoding='utf-8', errors='replace') as f:
            return f.read()
    except OSError:
        return None


def _agg_aux(book_dir):
    """把各块 wrapper.aux 里的 \\newlabel 抄成一份聚合 aux，供跨块 \\ref 使用。

    每块的页码计数器已经播成**绝对页码**，所以聚合出来的标签页码与整本一致。
    只抄 \\newlabel：其它行（\\@writefile / 包私有命令）对 xr-hyper 没有意义且可能报错。
    """
    lines = []
    for i in range(1, 64):
        p = os.path.join(_chunk_dir(book_dir, 'ch%d' % i), 'wrapper.aux')
        if not os.path.isfile(p):
            if i == 1:
                continue
            break
        try:
            with open(p, encoding='utf-8', errors='replace') as f:
                for ln in f:
                    if ln.startswith('\\newlabel'):
                        lines.append(ln.rstrip('\n'))
        except OSError:
            continue
    if lines:
        _write_if_changed(os.path.join(F_DIR, 'agg', 'main.aux'), '\n'.join(lines) + '\n')


def _compile_piece(book_dir, cid, preamble, text, inject, env,
                   texcmd, xelatex_alt, xdvipdfmx_alt, toc_seed=None, timeout=420,
                   single_pass=False, ignore_abspage=False):
    """编一块（章 / 前置页）：产物在 .build/floria/out/<cid>/。

    `inject` 写进 begindocument/end 钩子（页码 / 章号 / 分卷号）。
    章块的 wrapper 藏一行 `\\@starttoc{toc}`：把 .toc 写流打开，
    `\\addcontentsline` 就会落成带绝对页码的 wrapper.toc。
    `toc_seed` 不为 None（前置页）时**每一遍都重种** wrapper.toc——
    `\\tableofcontents` 先读该文件再清空重写，不重种第二遍就渲染成空目录。
    """
    _write_if_changed(os.path.join(book_dir, CHUNK_DIR, cid + '.tex'), text)
    outdir = _chunk_dir(book_dir, cid)
    os.makedirs(outdir, exist_ok=True)
    wrapper = os.path.join(outdir, 'wrapper.tex')
    opener = '' if toc_seed is not None else \
        '\\makeatletter\\setbox\\z@\\vbox{\\@starttoc{toc}}\\makeatother\n'
    wrap = (preamble + '\n'
            # 跨块引用：读上一轮各块聚合出的标签（没有就不加）
            + '\\IfFileExists{.build/floria/agg/main.aux}{\\usepackage{xr-hyper}'
              '\\externaldocument{.build/floria/agg/main}}{}\n'
            + '\\begin{document}\n'
            # 必须用 begindocument/end 钩子（\\AtBeginDocument 太早：ctexbook 会在
            # 章节前 \\cleardoublepage 多插一张空白页，实测每章各多 1 页）。
            + '\\AddToHook{begindocument/end}{%s}\n' % inject
            + opener
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

    # 本块自引用：`\ref`/`\autoref`/`\eqref`/`\cref` 印的是**编号**（1.2 这种），
    # 起始页平移不会改它；`\pageref`/`\vref`/`\cpageref`/`\nameref` 印页码或标题，
    # 会随页码平移变化 → 块里有它们就退回「整份 aux 稳定」的老判据。
    page_refs = bool(re.search(r'\\(?:pageref|cpageref|vref|vpageref|nameref)\s*\*?\s*\{', text))
    ref_names = None if page_refs else set(
        m.group(1).strip() for m in re.finditer(r'\\(?:ref|autoref|eqref|cref|Cref)\s*\*?\s*\{([^{}]*)\}',
                                                text))

    def label_num_sig(names):
        """aux 里这些标签的「编号」字段：`\\newlabel{名}{{编号}{页码}…}` 只取编号。"""
        if not names:
            return {}
        try:
            with open(os.path.join(outdir, 'wrapper.aux'), encoding='utf-8', errors='replace') as f:
                s = f.read()
        except OSError:
            return {}
        d = {}
        for m in re.finditer(r'\\newlabel\{([^{}]*)\}\{\{([^{}]*)\}', s):
            if m.group(1) in names:
                d[m.group(1)] = m.group(2)
        return d

    def aux_sig():
        h = []
        for n in ('wrapper.aux', 'wrapper.out', 'wrapper.toc'):
            f = os.path.join(outdir, n)
            try:
                with open(f, 'rb') as fh:
                    data = fh.read()
            except OSError:
                h.append('')
                continue
            if ignore_abspage and n == 'wrapper.aux':
                # `\gdef \@abspage@last{N}` 只记总页数（lastpage 包用），随上一遍的
                # 页数跳动却不影响任何排版结果。把它排除出稳定性签名，块就能少跑
                # 一遍（实测每遍 1.3–3.2s；书稿不用 lastpage 时才启用）。
                data = b'\n'.join(ln for ln in data.split(b'\n')
                                  if not ln.startswith(b'\\gdef \\@abspage@last'))
            h.append(hashlib.md5(data).hexdigest())
        return '|'.join(h)

    rel_out = os.path.join(F_DIR, 'out', cid).replace('\\', '/')
    rel_wrap = os.path.join(rel_out, 'wrapper.tex').replace('\\', '/')
    rel_pdf = os.path.join(rel_out, 'chapter.pdf').replace('\\', '/')
    rel_xdv = os.path.join(rel_out, 'wrapper.xdv').replace('\\', '/')

    def seed():
        if toc_seed is None:
            return
        try:
            with open(os.path.join(outdir, TOC_NAME), 'w', encoding='utf-8') as f:
                f.write(toc_seed)
        except OSError:
            pass

    # 只用 PATH 里的裸名 xelatex（绝对路径在本机实测慢得多）；PATH 里没有再退回 mk 同目录的 exe。
    # 重编轮数：默认最多 4 遍直到 aux/out/toc 稳定。
    #   · single_pass：块正文没改、只是起始页码变了（被前面章推后），它自己的版式
    #     完全不变，一遍就够（每遍 1.3–2.3s）。
    #   · refs_ok()：这一遍之后，「本块自己 \ref 出去的标签编号」一个都没变，且块里
    #     没有 \pageref 这类印页码的引用 —— 那么再跑一遍的输出与这一遍完全相同。
    #     （\ref 只印编号不印页码；跨块引用走 agg aux，本遍内根本不会变。）
    rc = 1
    for _ in range(1 if single_pass else 4):
        before = aux_sig()
        refs_before = label_num_sig(ref_names)
        seed()
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
        # aux/out/toc 三件稳定即止（2 遍）：前置页每遍都重种 wrapper.toc，
        # 输出与上一遍一致同样可以停（旧写法对前置页恒跑满 4 遍，整本目录一遍就要好几秒）。
        if rc != 0 or aux_sig() == before:
            break
        if ref_names is not None and not page_refs and label_num_sig(ref_names) == refs_before:
            break                       # 编号没变、又不印页码 → 再跑一遍是空转
    if rc == 0:
        # `-z`：xdvipdfmx 默认 zlib 级别 9，对 CJK 嵌入字体的流极慢（CHEMISTERY ch8
        # 实测 21.5s）。级别越低越快、产物越大——2026-10-28 在 54 页章（3.5MB 产物）上
        # 实测 9→21s / 6→1.33s / 3→0.95s(大 7%) / 1→0.86s(大 17%) / 0→0.63s(大 9 倍)。
        # 体积不是瓶颈、速度是，取 1：比 6 每次省约 0.45s，仅大 17%。
        try:
            rc = run(['xdvipdfmx', '-E', '-z', '1', '-o', rel_pdf, rel_xdv])
        except FileNotFoundError:
            if not xdvipdfmx_alt:
                raise
            rc = run([xdvipdfmx_alt, '-E', '-z', '1', '-o', rel_pdf, rel_xdv])
    return {'ok': rc == 0, 'secs': time.time() - t0, 'out': out,
            'pdf': _chunk_pdf(book_dir, cid), 'synctex': _chunk_sync(book_dir, cid)}


def _read_group(s, i):
    """s[i] == '{' → 返回 (花括号内文, 右括号之后的下标)；不平衡返回 (None, len(s))。"""
    depth, j = 0, i
    while j < len(s):
        c = s[j]
        if c == '\\':
            j += 2
            continue
        if c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                return s[i + 1:j], j + 1
        j += 1
    return None, len(s)


_TOC_LINE = re.compile(r'\\contentsline\s*\{')


def _tex_text(s):
    """把目录条目里的 LaTeX 片段压成纯文本（书签标题用）。"""
    s = re.sub(r'\\numberline\s*', '', s)
    s = re.sub(r'\\hspace\*?\s*\{[^{}]*\}', ' ', s)
    s = re.sub(r'\\texorpdfstring\s*\{', '', s)     # 两个参数会自然合并
    s = re.sub(r'\\[A-Za-z@]+\*?', '', s)
    s = s.replace('{', '').replace('}', '')
    s = s.replace('~', ' ').replace('\\\\', ' ')
    return re.sub(r'\s+', ' ', s).strip()


def _parse_toc(text):
    """解析 \\contentsline{level}{title}{page}{anchor} → [{level,title,page}]。"""
    out = []
    for m in _TOC_LINE.finditer(text or ''):
        i = m.end() - 1
        level, i = _read_group(text, i)
        title, i = _read_group(text, i)
        page, i = _read_group(text, i)
        if level is None or title is None or page is None:
            continue
        try:
            pg = int(page.strip())
        except ValueError:
            continue
        out.append({'level': level.strip(), 'title': _tex_text(title), 'page': pg})
    return out


def _result(status, code, message, **extra):
    """结构化退出：status / reason_code / reason 一并回给 compiler 与账本。"""
    global LAST_SKIP, LAST_REASON
    LAST_SKIP = message
    LAST_REASON = {'status': status, 'code': code, 'message': message}
    r = {'status': status, 'reason_code': code, 'reason': message}
    r.update(extra)
    return r


def _fail(code, message, **extra):
    """快速路径真实失败：compiler 直接报错，不再隐式整本重编。"""
    return _result(STATUS_FAILED, code, message, **extra)


def try_compile(book_dir, book_name, tex, env, log_cb=None):
    """快速路径（唯一编译路径）。返回结构化结果：status / reason_code / reason。

    切块 → 并行重编改动块（注入页码/章号/分卷号）→ 拼整本目录 → 单独编前置页
    （封面/目录）→ 按 openright 解页码，被推后的块重编 → 拼 PDF（前置页+各块+
    空白页）+ 按目录条目重建书签 → 合并 synctex → 原子落盘。
    """
    global LAST_SKIP, LAST_REASON, LAST_PHASES, LAST_CHUNKS
    LAST_SKIP = ''
    LAST_REASON = {}
    LAST_PHASES, LAST_CHUNKS = {}, {}
    # 把过程日志同时收进返回值：失败时 compiler 会把它写进 .build/main.log 与账本，
    # 日志卡才有真实的 `文件:行号:` 报错可点（旧写法只在外部给了 log_cb 时才留痕，
    # 而 compiler 从来不给 → 失败日志里只剩一句「块编译失败：chN」）。
    _lines = []
    _outer_cb = log_cb

    def log_cb(msg):                        # noqa: F811  内置收集 + 转交外部回调
        _lines.append(str(msg))
        if _outer_cb:
            _outer_cb(msg)

    def fail(code, message, **extra):
        if _lines:
            extra.setdefault('log', '\n'.join(_lines))
        return _fail(code, message, **extra)

    doc, shape_reason = _split_document(book_dir)
    if not doc:
        return _fail(shape_reason, {
            'has_appendix': '文档含 \\appendix（章编号改成字母并重置计数器），按章切块无法复现',
            'no_main_tex': '找不到可读的 main.tex',
            'no_begin_document': 'main.tex 里没有 \\begin{document}',
        }.get(shape_reason, '无法拆分 main.tex'))
    try:
        import pypdf  # noqa: F401
    except Exception as ex:                 # noqa: BLE001
        return _fail('missing_pypdf', '缺少 pypdf：%s' % ex)

    chunks = doc['chunks']
    n = len(chunks)
    hashes = [_chunk_hash(doc['preamble'], c) for c in chunks]
    cache = read_cache(book_dir) or {}
    cached = cache.get('chunks') if isinstance(cache.get('chunks'), list) else []

    def chunk_ok(i):
        c = cached[i] if i < len(cached) else {}
        cid = chunks[i]['id']
        return (isinstance(c.get('pages'), int) and c.get('built') == hashes[i]
                and os.path.isfile(_chunk_pdf(book_dir, cid))
                and _sync_ok(_chunk_sync(book_dir, cid))
                and os.path.isfile(_toc_path(book_dir, cid)))
    changed = [i for i in range(n) if not chunk_ok(i)]

    # 章号 / 分卷号按「前面出现过几个 \chapter / \part」推进
    chapter_no, part_no = [], []
    kc = 0
    for c in chunks:
        chapter_no.append(kc)
        part_no.append(c['parts'])
        if not c['starred']:
            kc += 1

    def layout(pages, fro):
        """前置页数与各块页数 → 每块起始页（openright：每块从奇数页起）。"""
        out = []
        pos = fro + 1
        if pos % 2 == 0:                    # 前置页结束在奇数页 → 章首补一张空白
            pos += 1
        for i in range(n):
            out.append(pos)
            pos += pages[i]
            if pos % 2 == 0:
                pos += 1
        return out

    texcmd = 'xelatex'                      # 优先 PATH 解析（绝对路径在本机实测慢得多）
    texdir = os.path.dirname(tex) if tex else ''
    xelatex_alt = os.path.join(texdir, 'xelatex.exe') if texdir else ''
    xdvipdfmx_alt = os.path.join(texdir, 'xdvipdfmx.exe') if texdir else ''
    _agg_aux(book_dir)                      # 跨块 \ref：用上一轮各块产出的标签

    front_pages = int(cache.get('front') or 0)
    starts = []
    for i in range(n):
        c = cached[i] if i < len(cached) else {}
        st = c.get('start')
        starts.append(st if isinstance(st, int) and st > 0 else 1)
    if any(s == 1 for s in starts):
        # 冷缓存但成品还在：用 main.synctex.gz（其次书签页）当初值，省掉一整轮
        # 「按第 1 页编一遍、再按真实页码全量重编」。
        seed = _seed_from_synctex(book_dir, chunks) or _seed_from_pdf(book_dir, chunks)
        if seed:
            front_pages, starts = seed

    def inject_of(i, start_page):
        # article 类（ctexart）没有 chapter/part 计数器：无条件 \setcounter 会在
        # -halt-on-error 下直接失败（MARXISM 实测）。有计数器才设，没有就跳过。
        s = ('\\ifcsname c@chapter\\endcsname\\setcounter{chapter}{%d}\\fi'
             '\\setcounter{page}{%d}') % (chapter_no[i], start_page)
        if part_no[i]:
            s += '\\ifcsname c@part\\endcsname\\setcounter{part}{%d}\\fi' % part_no[i]
        return s

    def uses_lastpage(*texts):
        """书稿是否用 lastpage（`\\pageref{LastPage}` 之类）。

        用了就不能忽略 aux 里的 `\\@abspage@last`，否则末页页码会停在上一遍。
        """
        return any(('LastPage' in (t or '')) or ('lastpage' in (t or '')) for t in texts)

    def compile_one(i, start_page):
        try:
            return _compile_piece(book_dir, chunks[i]['id'], doc['preamble'],
                                  chunks[i]['text'], inject_of(i, start_page), env,
                                  texcmd, xelatex_alt, xdvipdfmx_alt,
                                  single_pass=(i not in changed),
                                  ignore_abspage=not uses_lastpage(doc['preamble'],
                                                                   chunks[i]['text']))
        except Exception as ex:             # noqa: BLE001
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

    front_pdf_p = _chunk_pdf(book_dir, FRONT_ID)
    front_sync_p = _chunk_sync(book_dir, FRONT_ID)

    front_memo = {'toc': None, 'r': None}   # 本轮内已编过的前置页（目录没变就别重编）

    def compile_front(toc_text):
        """编前置页（封面/目录）：目录先种进 wrapper.toc，页数读不出算失败。

        `_front_hash` 相同（导言区/前置正文/整本目录都没变）且产物完好时直接复用——
        改一章正文但目录页码不变时，前置页不必重编。
        """
        if not doc['front'].strip():
            return {'ok': True, 'pdf': '', 'synctex': '', 'pages': 0}
        if front_memo['toc'] == toc_text and front_memo['r'] and front_memo['r'].get('ok'):
            # 布局迭代会再进一轮（只为重编被推后的章），目录文本没变就别重编前置页：
            # 前置页只依赖导言区 + 前置正文 + 目录 seed，与各章 PDF 无关。
            return front_memo['r']
        key = _front_hash(doc['preamble'], doc['front'], toc_text)
        if (cache.get('front_hash') == key and isinstance(cache.get('front'), int)
                and cache['front'] > 0 and os.path.isfile(front_pdf_p)
                and _sync_ok(front_sync_p)):
            return {'ok': True, 'pdf': front_pdf_p, 'synctex': front_sync_p,
                    'pages': cache['front'], 'reused': True}
        try:
            r = _compile_piece(book_dir, FRONT_ID, doc['preamble'], doc['front'],
                               '\\setcounter{page}{1}', env,
                               texcmd, xelatex_alt, xdvipdfmx_alt, toc_seed=toc_text,
                               ignore_abspage=not uses_lastpage(doc['preamble'], doc['front']))
        except Exception as ex:             # noqa: BLE001
            return {'ok': False, 'out': str(ex)}
        r['pages'] = _pages_of(r['pdf']) if r.get('ok') else None
        if r.get('ok'):
            front_memo['toc'] = toc_text
            front_memo['r'] = r
        return r

    t0 = time.time()
    todo = list(changed)
    master_toc = ''
    front_pdf = ''
    pages = []
    ok_layout = False
    for rounds in range(4):
        errors = compile_chunks(todo, starts)
        if errors and len(todo) > 1 and format_race(errors):
            # 便携 MiKTeX 在缓存/配置变动后，首个 xelatex 会重建 xelatex.fmt；
            # 多个进程并发启动时只有一个能写、其余以 Permission denied 退出。
            if log_cb:
                log_cb('[并行编译] 检测到便携 MiKTeX 格式文件争用，串行重建后重试失败块')
            rebuild_format()
            bad = [i for i in todo if chunks[i]['id'] in {c for c, _ in errors}]
            errors = compile_serially(bad, starts)
        if errors:
            for cid, out in errors:
                # 尾部留宽一点：-file-line-error 的「文件:行号: 错误」就在这一段的末尾
                log_cb('[并行编译失败] %s\n%s' % (cid, (out or '')[-4000:]))
            return fail('chunk_compile_failed',
                        '块编译失败：%s' % '; '.join(c for c, _ in errors))

        pages = []
        for i in range(n):
            pg = _pages_of(_chunk_pdf(book_dir, chunks[i]['id']))
            if not isinstance(pg, int) or pg < 1:
                return fail('chunk_pages_unreadable',
                            '块 %s 的 PDF 页数读不出' % chunks[i]['id'])
            pages.append(pg)

        bits, missing = [], []
        for i in range(n):
            t = _read_toc(book_dir, chunks[i]['id'])
            if t is None:
                missing.append(i)
            bits.append(t or '')
        if missing:
            if log_cb:
                log_cb('[目录] 缺目录条目，重编：%s'
                       % ', '.join(chunks[i]['id'] for i in missing))
            todo = missing
            continue
        master_toc = ''.join(bits)

        fr = compile_front(master_toc)
        if not fr.get('ok'):
            log_cb('[前置页编译失败]\n%s' % ((fr.get('out') or '')[-4000:]))
            return fail('front_compile_failed', '前置页（封面/目录）编译失败')
        new_front = fr.get('pages') or 0
        front_pdf = fr.get('pdf') or ''
        new_starts = layout(pages, new_front)
        shifted = [i for i in range(n) if new_starts[i] != starts[i]]
        if not shifted and new_front == front_pages:
            ok_layout = True
            break
        if log_cb and shifted:
            log_cb('[页码偏移] 重编：%s' % ', '.join(chunks[i]['id'] for i in shifted))
        starts, front_pages, todo = new_starts, new_front, shifted
    if not ok_layout:
        return fail('layout_unstable', '页码布局 4 轮迭代仍未收敛（前置页/分页不稳定）')

    offsets = [s - 1 for s in starts]
    parts = []
    if front_pages:
        parts.append(('pages', front_pdf, 0, front_pages))
    pos = front_pages
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
        return fail('read_chunk_pdf_failed', '读取块 PDF 失败：%s' % ex)

    outline = _parse_toc(master_toc)
    t_pdf0 = time.time()
    main_pdf = os.path.join(book_dir, 'main.pdf')
    merged = os.path.join(book_dir, '.build', '_floria_main.pdf')
    try:
        total_pages = _merge_pdfs(parts, merged, outline=outline)
    except Exception as ex:                 # noqa: BLE001
        return fail('merge_pdf_failed', '拼 PDF/重建书签失败：%s' % ex)
    t_pdf = time.time()

    # 合并 synctex：前置页 + 各块（页号偏移 + 行号平移回 main.tex）
    sync_tmp = os.path.join(book_dir, '.build', '_floria_main.synctex.gz')
    items = []
    if front_pages:
        items.append({'path': _chunk_sync(book_dir, FRONT_ID), 'offset': 0,
                      'chunk_name': FRONT_ID + '.tex', 'chunk_line': doc['front_line']})
    for i in range(n):
        items.append({'path': _chunk_sync(book_dir, chunks[i]['id']), 'offset': offsets[i],
                      'chunk_name': chunks[i]['id'] + '.tex', 'chunk_line': chunks[i]['line']})
    t_sync0 = time.time()
    try:
        _merge_synctex_cached(items, sync_tmp)
    except Exception as ex:                 # noqa: BLE001
        if log_cb:
            log_cb('[synctex 合并失败] %s' % ex)
        return fail('synctex_merge_failed', 'synctex 合并失败：%s' % ex)

    # PDF 与 synctex 都合并成功后才落盘：任一失败都不替换成品，
    # 避免出现「PDF 已换新、点开却全无映射」的撕裂状态。
    try:
        os.replace(merged, main_pdf)
        os.replace(sync_tmp, os.path.join(book_dir, 'main.synctex.gz'))
    except OSError as ex:
        return fail('commit_failed', '替换成品失败：%s' % ex)
    t_sync = time.time()
    if not total_pages:                     # 合并时已知页数；拿不到才回头读一次成品
        total_pages = _pages_of(main_pdf)
    rebuilt = len(set(changed) | set(todo))
    LAST_PHASES = {'chunks': round(t_pdf0 - t0, 2),
                   'pdf_merge': round(t_pdf - t_pdf0, 2),
                   'synctex': round(t_sync - t_sync0, 2)}
    LAST_CHUNKS = {'total': n, 'rebuilt': rebuilt, 'reused': n - rebuilt}

    newcache = [{'id': chunks[i]['id'], 'hash': hashes[i], 'built': hashes[i],
                 'pages': pages[i], 'start': starts[i]} for i in range(n)]
    _write_cache(book_dir, {'front': front_pages, 'chunks': newcache,
                            'front_hash': _front_hash(doc['preamble'], doc['front'], master_toc),
                            'ts': round(time.time(), 3)})
    _agg_aux(book_dir)                      # 供下一次编译的跨块 \ref
    elapsed = round(time.time() - t0, 2)
    log = '\n'.join([
        '[编译链] 自动分章并行（%d 块，重编 %d 块，前置页 %d 页）'
        % (n, rebuilt, front_pages),
        '[编译链] 未变块复用缓存：%s'
        % (', '.join(chunks[i]['id'] for i in range(n) if i not in changed) or '无'),
        '[编译链] 拼接 %d 个 PDF（pypdf）+ 按目录条目重建书签（%d 项）'
        % (n + (1 if front_pages else 0), len(outline)),
        '[编译链] 各块起始页：%s'
        % ' / '.join('%s=%d' % (chunks[i]['id'], starts[i]) for i in range(n)),
    ])
    return {'status': STATUS_OK, 'reason_code': None, 'reason': None,
            'ok': True, 'time': elapsed, 'mtime': int(os.path.getmtime(main_pdf) * 1000),
            'pages': total_pages, 'log': log, 'fast': True}
