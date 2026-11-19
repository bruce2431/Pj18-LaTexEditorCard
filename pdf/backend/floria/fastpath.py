# -*- coding: utf-8 -*-
"""整本单文件 main.tex 的自动分章并行编译（2026-10-27：只保留快速路径）。

用户要求：**源文件层次永远只有一本 main.tex**，也**不再有整本 latexmk 兜底**。
切分全部发生在 `.build/` 里：
  1. 读 main.tex，按顶层 `\\chapter` / `\\chapter*` / `\\part`（必换页）切成若干块；
     章内再按「`\\newpage` 后紧接 `\\section`/`\\subsection`」细分，改一节只重编一节；
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

from . import log, pdf_stamp, tikz_cache

CACHE_NAME = '.floria_fast.json'
# 缓存结构版本：4 = 页码解耦 + PDF 原位盖印/热更新 + 预处理精简 SyncTeX + TikZ 图元缓存。
CACHE_VER = 4
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

# 印页码/标题的引用：跨块时页码取自聚合 aux，整本页码一变就要补编。
_PAGE_REF_RE = re.compile(r'\\(?:pageref|cpageref|vref|vpageref|nameref)\s*\*?\s*\{')


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


# 章内再切分：只认「`\newpage` 之后紧接 `\section`/`\subsection`」的位置——
# 这些地方本来就要换页，切块后版面与整本一致；正文中途的 `\newpage` 不切，
# 避免浮动体跨页位置漂移。
_NEWPAGE_RE = re.compile(r'\\newpage\b')
_AFTER_NEWPAGE_RE = re.compile(r'(?:\s|%[^\n]*\n)*\\(?:section|subsection)\b')

# 分块续接的编号计数器：整本里它们跨段连续，块内从 0 起会把编号印错。
_COUNTERS = ('section', 'subsection', 'subsubsection', 'figure', 'table',
             'equation', 'footnote')
_THEOREM_COUNTERS = ('theorem', 'definition', 'example', 'lemma', 'corollary',
                     'proposition')
_ROW_ENVS = ('align', 'gather', 'multline', 'eqnarray', 'flalign', 'alignat')


def _count_cmd(text, pat):
    """数未被注释的匹配个数。"""
    return sum(1 for m in re.finditer(pat, text) if not _commented(text, m.start()))


def _numbered_rows(text, env):
    """`\\begin{align}` 这类多行公式里真正编号的行数（`\\nonumber`/`\\notag` 不编号）。"""
    total = 0
    pat = r'\\begin\{%s\}(.*?)\\end\{%s\}' % (env, env)
    for m in re.finditer(pat, text, re.S):
        if _commented(text, m.start()):
            continue
        for row in re.split(r'\\\\', m.group(1)):
            if row.strip() and not re.search(r'\\(?:nonumber|notag)\b', row):
                total += 1
    return total


def _equation_count(text):
    """equation 环境与多行公式里累计编号了几条公式。"""
    n = 0
    for m in re.finditer(r'\\begin\{equation\}(.*?)\\end\{equation\}', text, re.S):
        if _commented(text, m.start()):
            continue
        if not re.search(r'\\(?:nonumber|notag)\b', m.group(1)):
            n += 1
    return n + sum(_numbered_rows(text, e) for e in _ROW_ENVS)


def _counter_snapshot(text):
    """章内切点之前已消耗的编号计数器。

    子块独立编译时计数器从 0 起，必须在 wrapper 里注入这些值，后面紧跟的
    `\\section` / 图 / 表 / 公式 / 定理才会接着整本的编号走。
    `\\section` 会重置 subsection、定理类计数器，所以这几项只数「最后一个
    `\\section` 之后」的部分；figure/table/equation/footnote 按章重置，数全章。
    """
    def after_last(pat):
        pos = -1
        for m in re.finditer(pat, text):
            if not _commented(text, m.start()):
                pos = m.start()
        return text[pos + 1:] if pos >= 0 else ''

    sec = after_last(r'\\section\b(?!\*)')
    sub = after_last(r'\\subsection\b(?!\*)')
    snap = {
        'section': _count_cmd(text, r'\\section\b(?!\*)'),
        'subsection': _count_cmd(sec, r'\\subsection\b(?!\*)'),
        'subsubsection': _count_cmd(sub, r'\\subsubsection\b(?!\*)'),
        'figure': _count_cmd(text, r'\\begin\{figure\}'),
        'table': _count_cmd(text, r'\\begin\{table\}'),
        'equation': _equation_count(text),
        'footnote': _count_cmd(text, r'\\footnote\b'),
    }
    for name in _THEOREM_COUNTERS:
        snap[name] = _count_cmd(sec, r'\\begin\{%s\}' % name)
    return snap


def _title_arg(text, cmd, start=0):
    """取 `\\cmd[短标题]{标题}` 的标题参数（支持嵌套花括号）；找不到返回 ''。"""
    for m in re.finditer(r'\\%s\b(?!\s*\*)\s*' % cmd, text):
        if m.start() < start or _commented(text, m.start()):
            continue
        i = m.end()
        if i < len(text) and text[i] == '[':
            j = text.find(']', i)
            if j < 0:
                continue
            i = j + 1
        while i < len(text) and text[i] in ' \t\r\n':
            i += 1
        if i >= len(text) or text[i] != '{':
            continue
        depth = 0
        j = i
        while j < len(text):
            ch = text[j]
            if ch == '\\':          # 跳过 \{ \} 等转义
                j += 2
                continue
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    return text[i + 1:j]
            j += 1
    return ''


def _last_title_before(text, cmd, pos):
    """`text[:pos]` 里最后一个 `\\cmd{...}` 的标题。"""
    last = ''
    for m in re.finditer(r'\\%s\b(?!\s*\*)\s*' % cmd, text[:pos]):
        if _commented(text, m.start()):
            continue
        t = _title_arg(text, cmd, m.start())
        if t:
            last = t
    return last


def _split_at_newpage(text, line, cid, parts, chapter_title=''):
    """把一章文本按 `\\newpage` 再切开；返回 (第一块文本, 后续子块列表)。

    第一块保留切点处的 `\\newpage` 本身——去掉它会让它前面的段落重排，
    断行/字距与整本编译不一致。
    """
    cuts = []
    for m in _NEWPAGE_RE.finditer(text):
        if _commented(text, m.start()) or not _AFTER_NEWPAGE_RE.match(text, m.end()):
            continue
        cuts.append((m.start(), m.end()))
    # 章首就是 \newpage 时不能切出空块：把这次换页留在第一块里。
    while cuts and not text[:cuts[0][0]].strip():
        cuts.pop(0)
    if not cuts:
        return text, []
    subs = []
    for k, (_, a) in enumerate(cuts):
        b = cuts[k + 1][1] if k + 1 < len(cuts) else len(text)
        piece = text[a:b]
        if not piece.strip():
            continue
        # 子块首页的页眉取「本页第一个 right mark」：以 \section 开头时应该是
        # 这一节自己的标题，以 \subsection 开头时沿用上一节。
        starts_with_section = bool(re.match(
            r'\s*(?:%[^\n]*\n\s*)*\\section\b(?!\s*\*)', piece))
        if starts_with_section:
            head_section = _title_arg(piece, 'section')
        else:
            head_section = _last_title_before(text, 'section', a)
        subs.append({
            'id': '%s_%d' % (cid, k + 2),
            'text': piece,
            'line': line + text.count('\n', 0, a),
            'starred': True,          # 子块不推进章号
            'parts': parts,
            'openright': False,       # 章内子块接上一块继续排，不另起奇数页
            'counters': _counter_snapshot(text[:a]),
            # 子块里没有 \chapter，页眉的章名/节名要靠注入 marks 复刻。
            'marks': {'chapter': chapter_title, 'section': head_section,
                      'section_next': starts_with_section},
        })
    return text[:cuts[0][1]], subs


def split_document(book_dir):
    """把 main.tex 切块。返回 {preamble, front, front_line, chunks:[…]} 或 None。

    切点 = 顶层 `\\chapter` / `\\chapter*` / `\\part`（都必换页），独立编译版面与
    整本一致；章内再在「`\\newpage` 紧接 `\\section`/`\\subsection`」处细分，
    让改一节只重编一节（`\\section` 本身不换页，按它切会让断行/浮动体漂移）。
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
        text, subs = _split_at_newpage(body, line0, 'ch1', 0,
                                       _title_arg(body, 'chapter'))
        return {'preamble': preamble, 'front': '', 'front_line': line0,
                'chunks': [{'id': 'ch1', 'text': text, 'line': line0,
                            'starred': True, 'parts': 0,
                            'openright': True, 'counters': {}}] + subs}, None

    chunks = []
    parts = 0
    for i, (pos, kind) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(body)
        text = body[pos:end]
        line = line0 + body.count('\n', 0, pos)
        cid = 'ch%d' % (i + 1)
        first, subs = _split_at_newpage(text, line, cid, parts,
                                        _title_arg(text, 'chapter'))
        chunks.append({'id': cid,
                       'text': first,
                       'line': line,
                       'starred': kind != 'chapter',   # 只有 \chapter 推进章号
                       'parts': parts,
                       'openright': True,
                       'counters': {}})
        chunks.extend(subs)
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
    for c, h in zip(chunks, hits):
        s = solid(h)
        if s is not None and c.get('openright', True) and s % 2 == 0:
            # openright 下章首必为奇数页：偶数是「章题页记录太少」被阈值挡掉的假首页。
            # 章内子块接上一块连续排，可以是偶数页，不校正。
            if h.get(s - 1, 0) >= 2:
                s -= 1
            elif h.get(s + 1, 0) >= 2:
                s += 1
        starts.append(s)
    if any(s is None for s in starts):
        return None
    if any(c.get('openright', True) and s % 2 == 0
           for c, s in zip(chunks, starts)):
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
    if any(c.get('openright', True) is False for c in chunks):
        return None                         # 章内子块没有书签，没法按书签反推
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
    # 子块的编号接续前一节：前缀里增删一个 \section/图/公式，本块正文没动
    # 但印出来的编号会变，指纹里必须带上注入的计数器。
    counters = chunk.get('counters') or {}
    if counters:
        h.update(b'\0')
        h.update(repr(sorted(counters.items())).encode('utf-8', 'replace'))
    return h.hexdigest()


# 跨块引用：\ref 家族印刷出来的页码/标题、以及链接目的地**锚点名**都由聚合 aux 决定。
_XREF_RE = re.compile(r'\\(?!href)([A-Za-z]*ref)\s*\*?\s*\{([^{}]*)\}')


def _agg_labels(book_dir):
    """聚合 aux 的标签定义：name → \\newlabel 整行。"""
    out = {}
    p = os.path.join(book_dir, F_DIR, 'agg', 'main.aux')
    try:
        with open(p, encoding='utf-8', errors='replace') as f:
            for ln in f:
                if not ln.startswith('\\newlabel'):
                    continue
                j = ln.find('}')
                if j > 10:
                    out[ln[10:j]] = ln.rstrip('\n')
    except OSError:
        pass
    return out


_PAGE_FIELD_RE = re.compile(r'^(\\newlabel\{[^{}]*\}\{\{[^{}]*\})\{[^{}]*\}')


def _xref_sig(text, labels):
    """块内引用目标在聚合 aux 里的定义指纹（无引用 → None，与旧缓存天然相等）。

    块正文没变、被引标签却改了编号或锚点名时，旧分片里的链接仍指向旧目标；
    合并时同名目标取「第一份出现的」（`_merge_pdfs` 的 setdefault），于是点到另一处。
    实测：把 `\\paragraph` 提成 `\\subsection` 后，第 110 页的 [16] 跳到了 106 页。

    **页码子字段不参与指纹**：跨块引用在 PDF 里是 GoToR + 命名锚点
    （`/D = 'subsection.4.2.8'`），合并时按名字重定向；被引标签只是页码平移
    （`{{编号}{页码}` 的第二个字段变化）时，引用块的 PDF 依然有效。把页码算进去
    会让任何一处页码偏移都把全部带引用的块判 stale，再触发整批重编——那是
    一次改稿编译从 ~30s 涨到 ~55s 的主因（同一起编辑里先按旧起始页编一遍，
    布局迭代后还要再编一遍）。
    """
    names = sorted({n.strip() for _cmd, grp in _XREF_RE.findall(text or '')
                    for n in grp.split(',') if n.strip()})
    if not names:
        return None
    h = hashlib.md5()
    for nm in names:
        h.update(nm.encode('utf-8', 'replace'))
        h.update(b'\0')
        # 页码子字段替换成固定占位；编号、标题、锚点名原样参与。
        d = _PAGE_FIELD_RE.sub(r'\1{*}', labels.get(nm) or '')
        h.update(d.encode('utf-8', 'replace'))
        h.update(b'\0')
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
    # 分片缓存的判据升级过（CACHE_VER 变了）：上次成品可能是按旧判据复用的旧分片拼的，
    # 源码虽未变也不能直通，必须重走一遍分片校验。
    if (read_cache(book_dir) or {}).get('ver') != CACHE_VER:
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


def _chunk_slim_sync(book_dir, cid):
    return os.path.join(_chunk_dir(book_dir, cid), 'wrapper.synctex.slim')


def _chunk_fmeta(book_dir, cid):
    return os.path.join(_chunk_dir(book_dir, cid), 'wrapper.fmeta')


def _slim_sync_ok(path):
    """检查预处理精简 SyncTeX 是否完整（以 END 标记收尾）。"""
    try:
        st = os.stat(path)
        if st.st_size < 16:
            return False
        with open(path, 'rb') as f:
            f.seek(max(0, st.st_size - 32))
            return b'FLORIA_SLIM_END' in f.read()
    except OSError:
        return False


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
    data['ver'] = CACHE_VER
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
    # 跨块引用经 xr-hyper 写成 GoToR，指向聚合件 .build/floria/agg/main.pdf；
    # 成品里既没有这个文件、路径也是相对当前 PDF 的，点击不会有反应。
    # 目标页已在合并件中，把这类注释改回内部 GoTo（按命名目标跳页）。
    try:
        from pypdf.generic import NameObject, TextStringObject
        for page in w.pages:
            annots = page.get('/Annots')
            if annots is None:
                continue
            for annot in annots.get_object():
                obj = annot.get_object()
                act = obj.get('/A')
                if act is None:
                    continue
                act = act.get_object()
                if act.get('/S') != '/GoToR':
                    continue
                d = act.get('/D')
                if isinstance(d, str) and d in dests:
                    act[NameObject('/S')] = NameObject('/GoTo')
                    act[NameObject('/D')] = TextStringObject(d)
                    act.pop(NameObject('/F'), None)
    except Exception:                   # noqa: BLE001
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


_SLIM_KEEP_KINDS = frozenset('([hvxr')
_SLIM_PAGE_RE = re.compile(r'(?m)^([{}])(\d+)$')
_SLIM_LINE_RE = re.compile(r'(?m)^([-\[\]()<>xkgvhr$])1,(\d+):')


def _build_slim_synctex(sync_gz, slim_out, chunk_name, max_pages=None):
    """编译单块后一次性预处理 SyncTeX：只留块 .tex（tag=1）与 .toc（tag=2）的盒/锚点记录，剔除 Atlas 页。

    过滤掉 synctex.py 从不使用的胶水/字距/数学散点（g/k/$），将 32 万行原始记录压到 ~5 万行纯文本，
    合并耗时从 ~1.18s 降到 ~0.06s，同时让前端点击跳转解析提速 3.5x。
    """
    with gzip.open(sync_gz, 'rt', encoding='utf-8', errors='replace') as f:
        d = _parse_synctex(f.read())
    keep_tag = {}
    for tag, p in d['inputs'].items():
        bn = os.path.basename(p.replace('\\', '/'))
        if chunk_name and bn == chunk_name:
            keep_tag[tag] = 1
        elif bn.lower().endswith('.toc'):
            keep_tag[tag] = 2

    out = []
    for k in _HDR:
        if k in d['hdr']:
            out.append('H %s:%s' % (k, d['hdr'][k]))
    npages = min(d['pages'], max_pages) if (max_pages is not None and max_pages > 0) else d['pages']
    out.append('PAGES %d' % npages)

    cur_page = 0
    skip_page = False
    for line in d['content']:
        if not line:
            continue
        c = line[0]
        if c == '{':
            try:
                cur_page = int(line[1:])
            except ValueError:
                cur_page = 0
            skip_page = bool(max_pages is not None and cur_page > max_pages)
            if not skip_page:
                out.append(line)
            continue
        if c == '}':
            if not skip_page:
                out.append(line)
            cur_page = 0
            skip_page = False
            continue
        if skip_page or c not in _SLIM_KEEP_KINDS:
            continue
        m = _REC.match(line)
        if not m:
            continue
        nt = keep_tag.get(int(m.group(2)))
        if nt is None:
            continue
        out.append('%s%d,%s:%s' % (m.group(1), nt, m.group(3), m.group(4)))

    out.append('FLORIA_SLIM_END')
    tmp = slim_out + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write('\n'.join(out) + '\n')
    os.replace(tmp, slim_out)
    return True


def _merge_key(items):
    """合并输入指纹：各块 synctex 内容 + 页偏移 + 块名 + 行平移，全一致则结果必然一致。"""
    h = hashlib.md5()
    for it in items:
        p = it.get('slim') if (it.get('slim') and os.path.isfile(it['slim'])) else it['path']
        try:
            st = os.stat(p)
            h.update(('%d:%d|' % (st.st_size, st.st_mtime_ns)).encode('ascii'))
        except OSError:
            h.update(b'?|')
        h.update(('%d|%s|%d\n' % (it.get('offset') or 0, it.get('chunk_name') or '',
                                  it.get('chunk_line') or 0)).encode('utf-8'))
    return h.hexdigest()


def _merge_synctex_cached(items, out):
    """带缓存的 SyncTeX 合并：优先走预处理 `.slim` 极速路径（~0.08s）。"""
    book_dir = os.path.dirname(os.path.dirname(out))
    cache = os.path.join(book_dir, F_DIR, 'synctex_merged.gz')
    sidecar = cache + '.key'
    key = _merge_key(items)
    try:
        with open(sidecar, 'r', encoding='utf-8') as f:
            if f.read().strip() == key and os.path.isfile(cache):
                shutil.copyfile(cache, out)
                return False
    except OSError:
        pass
    if all(it.get('slim') for it in items):
        _merge_synctex_fast(items, out)
    else:
        _merge_synctex(items, out)
    try:
        shutil.copyfile(out, cache)
        with open(sidecar, 'w', encoding='utf-8') as f:
            f.write(key)
    except OSError as ex:                   # noqa: BLE001
        log.warn('写 synctex 合并缓存失败：%s' % ex)
    return True


def _merge_synctex_fast(items, out):
    """基于每块预处理 `.slim` 的极速 SyncTeX 合并（整块 C 级正则平移 + gzip level=1）。"""
    hdr = {}
    body_chunks = []
    line_count = 0
    total_pages = 0
    has_toc = False

    for it in items:
        slim = it['slim']
        if not _slim_sync_ok(slim):
            _build_slim_synctex(it['path'], slim, it.get('chunk_name'),
                                max_pages=it.get('max_pages'))
        offset = it.get('offset') or 0
        shift = (it.get('chunk_line') or 1) - 1
        npages = 0
        with open(slim, encoding='utf-8', errors='replace') as f:
            raw = f.read()
        # 解析前置 H / PAGES 头（仅前几行）并截取正文
        pos = 0
        nraw = len(raw)
        while pos < nraw:
            nl = raw.find('\n', pos)
            if nl < 0:
                nl = nraw
            ln = raw[pos:nl]
            if ln.startswith('H '):
                k, v = ln[2:].split(':', 1)
                if k in hdr and hdr[k] != v:
                    raise ValueError('synctex 头部参数不一致（%s: %s ≠ %s）' % (k, hdr[k], v))
                hdr.setdefault(k, v)
                pos = nl + 1
            elif ln.startswith('PAGES '):
                try:
                    npages = int(ln[6:])
                except ValueError:
                    pass
                pos = nl + 1
            else:
                break

        end = raw.rfind('\nFLORIA_SLIM_END')
        if end < 0:
            end = nraw
        seg = raw[pos:end].strip('\n')
        if seg:
            if not has_toc and ('2,' in seg):
                has_toc = True
            if offset:
                seg = _SLIM_PAGE_RE.sub(lambda m, o=offset: m.group(1) + str(int(m.group(2)) + o), seg)
            if shift:
                seg = _SLIM_LINE_RE.sub(lambda m, s=shift: '%s1,%d:' % (m.group(1), int(m.group(2)) + s), seg)
            body_chunks.append(seg)
            line_count += seg.count('\n') + 1
        total_pages = max(total_pages, offset + npages)

    head = ['SyncTeX Version:1', 'Input:1:main.tex']
    if has_toc:
        head.append('Input:2:wrapper.toc')
    head.append('Output:pdf')
    for k in _HDR:
        head.append('%s:%s' % (k, hdr.get(k, '1000' if k == 'Magnification' else '1')))
    head.append('Content:')
    tail = ['Postamble:', 'Count:%d' % line_count, '!%d' % total_pages, 'Post scriptum:', '']
    payload = ('\n'.join(head) + '\n'
               + ('\n'.join(body_chunks) + '\n' if body_chunks else '')
               + '\n'.join(tail)).encode('utf-8')

    tmp = out + '.tmp'
    with gzip.open(tmp, 'wb', compresslevel=1) as f:
        f.write(payload)
    os.replace(tmp, out)


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


def _agg_aux(book_dir, chunk_ids, deltas=None, include_front=False):
    """把各块 wrapper.aux 里的 \\newlabel 抄成一份聚合 aux，供跨块 \\ref 使用。

    `deltas` = {cid: int}：页码解耦模式下每块按相对页编译，这里将各块 \\newlabel 的
    页码子字段平移成绝对页码，同时返回 `{label_name: abs_page_str}` 供 `/FPR` 热更新。
    """
    lines = []
    label_pages = {}
    cids = ([FRONT_ID] + list(chunk_ids)) if include_front else list(chunk_ids)
    for cid in cids:
        p = os.path.join(_chunk_dir(book_dir, cid), 'wrapper.aux')
        if not os.path.isfile(p):
            continue
        delta = (deltas or {}).get(cid, 0)
        try:
            with open(p, encoding='utf-8', errors='replace') as f:
                for ln in f:
                    if not ln.startswith('\\newlabel'):
                        continue
                    raw = ln.rstrip('\n')
                    name, shifted, abs_pg = pdf_stamp.shift_newlabel_line(raw, delta)
                    lines.append(shifted)
                    if name and abs_pg:
                        label_pages[name] = abs_pg
        except OSError:
            continue
    if lines:
        # 必须落在 book_dir 下：wrapper 里的 \IfFileExists 是相对 xelatex 的 cwd
        # （= book_dir）解析的，写成相对进程 cwd 会一路落到后端目录，永远找不到。
        _write_if_changed(os.path.join(book_dir, F_DIR, 'agg', 'main.aux'),
                          '\n'.join(lines) + '\n')
    return label_pages


_ANY_REF_RE = re.compile(
    r'\\(?:ref|pageref|autoref|eqref|cref|Cref|vref|vpageref|cpageref|nameref|hyperref)\b'
)


def _compile_piece(book_dir, cid, preamble, text, inject, env,
                   texcmd, xelatex_alt, xdvipdfmx_alt, toc_seed=None, timeout=420,
                   single_pass=False, ignore_abspage=False, decoupled=False,
                   on_xelatex_done=None):
    """编一块（章 / 前置页）：产物在 .build/floria/out/<cid>/。

    优化：
      - 重型 TikZ 块先经 `tikz_cache` 批量预编译缓存（严格等行数替换，不破坏 SyncTeX）；
      - 按块实际用到的宏包安全精简导言区（失败自动回退完整导言区），无跨块引用时跳过 xr-hyper；
      - 页码解耦模式下注入 `/FTOC`/`/FPR`/`/FATLAS` 与 `.fmeta` 钩子，严格 1 遍收敛；
      - `xelatex` 刚写完 `.toc`/`.fmeta` 立即触发 `on_xelatex_done` 回调，允许 `front` 与本块
        `xdvipdfmx` 并行重叠；随后记录 `PATCH` 页索引并产出 `.synctex.slim` 精简映射。
    """
    text_for_tex = text
    if toc_seed is None and text:
        try:
            text_for_tex, _tstats = tikz_cache.preprocess_chunk_tikz(
                book_dir, preamble, text, env, texcmd, xelatex_alt, xdvipdfmx_alt)
        except Exception:                   # noqa: BLE001
            text_for_tex = text

    _write_if_changed(os.path.join(book_dir, CHUNK_DIR, cid + '.tex'), text_for_tex)
    outdir = _chunk_dir(book_dir, cid)
    os.makedirs(outdir, exist_ok=True)
    wrapper = os.path.join(outdir, 'wrapper.tex')
    opener = '' if toc_seed is not None else \
        '\\makeatletter\\setbox\\z@\\vbox{\\@starttoc{toc}}\\makeatother\n'
    hook = pdf_stamp.decoupled_hook_code() if decoupled else ''

    # 精简未使用的重型宏包（tikz/tikz-cd/chemfig/mhchem/pgfplots/booktabs/float 等）
    used_for_strip = text_for_tex + ('\n' + toc_seed if toc_seed else '')
    pre_eff = pdf_stamp.strip_preamble(preamble, used_for_strip)
    used_stripped = (pre_eff != preamble)
    need_xr = bool(_ANY_REF_RE.search(used_for_strip))
    xr_line = (
        '\\IfFileExists{.build/floria/agg/main.aux}{\\usepackage{xr-hyper}'
        '\\externaldocument{.build/floria/agg/main}}{}\n'
    ) if need_xr else ''

    def write_wrapper(pre_src):
        wrap = (pre_src + '\n'
                + xr_line
                + hook + '\n'
                + '\\begin{document}\n'
                # 必须用 begindocument/end 钩子（\\AtBeginDocument 太早：ctexbook 会在
                # 章节前 \\cleardoublepage 多插一张空白页，实测每章各多 1 页）。
                + '\\AddToHook{begindocument/end}{%s}\n' % inject
                + opener
                + '\\input{%s}\n' % os.path.join(CHUNK_DIR, cid + '.tex').replace('\\', '/')
                + '\\end{document}\n')
        _write_if_changed(wrapper, wrap)

    write_wrapper(pre_eff)

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

    # 本块自引用：`\ref`/`\autoref`/`\eqref`/`\cref` 印的是**编号**（1.2 这种）。
    # 解耦模式下 `\pageref` 由合并阶段 `/FPR` 原位热更新，不再拖慢块收敛。
    page_refs = False if decoupled else bool(_PAGE_REF_RE.search(text))
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

    def _own_aux_labels():
        try:
            with open(os.path.join(outdir, 'wrapper.aux'), encoding='utf-8', errors='replace') as f:
                return set(re.findall(r'\\newlabel\{([^{}]*)\}', f.read()))
        except OSError:
            return set()

    def _log_needs_rerun():
        """检查 wrapper.log 是否要求重跑（longtable 宽变更 / 本块已定义却仍报 undefined 的前向引用）。"""
        try:
            with open(os.path.join(outdir, 'wrapper.log'), encoding='utf-8', errors='replace') as f:
                lg = f.read()
        except OSError:
            return False
        if 'Table widths have changed' in lg:
            return True
        undefs = set(re.findall(r"Reference `([^']*)' .*undefined", lg))
        if undefs and (undefs & _own_aux_labels()):
            return True
        return False

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

    def run_xelatex():
        nonlocal texcmd
        try:
            return run([texcmd, '-no-pdf', '-synctex=1', '-interaction=nonstopmode',
                        '-halt-on-error', '-file-line-error',
                        '-output-directory=' + rel_out, rel_wrap])
        except FileNotFoundError:
            if not xelatex_alt:
                raise
            texcmd = xelatex_alt
            return run([texcmd, '-no-pdf', '-synctex=1', '-interaction=nonstopmode',
                        '-halt-on-error', '-file-line-error',
                        '-output-directory=' + rel_out, rel_wrap])

    rc = 1
    max_passes = 1 if single_pass else 4
    for p_idx in range(max_passes):
        before = aux_sig()
        refs_before = label_num_sig(ref_names)
        seed()
        rc = run_xelatex()
        if rc != 0 and p_idx == 0 and used_stripped:
            # 精简导言区偶发宏依赖缺失 → 无感回退完整导言区重试
            out = ''
            used_stripped = False
            write_wrapper(preamble)
            seed()
            rc = run_xelatex()
        if rc != 0:
            break
        if decoupled:
            if (ref_names is not None and label_num_sig(ref_names) == refs_before
                    and not _log_needs_rerun()):
                break
        else:
            if aux_sig() == before:
                break
            if ref_names is not None and not page_refs and label_num_sig(ref_names) == refs_before:
                break

    if rc == 0 and on_xelatex_done is not None:
        try:
            on_xelatex_done()
        except Exception:                   # noqa: BLE001
            pass

    if rc == 0:
        try:
            rc = run(['xdvipdfmx', '-E', '-z', '1', '-o', rel_pdf, rel_xdv])
        except FileNotFoundError:
            if not xdvipdfmx_alt:
                raise
            rc = run([xdvipdfmx_alt, '-E', '-z', '1', '-o', rel_pdf, rel_xdv])

    if rc == 0:
        max_pg = None
        if decoupled:
            fmeta_p = _chunk_fmeta(book_dir, cid)
            fm = pdf_stamp.parse_fmeta(fmeta_p)
            if fm:
                max_pg = fm['body_pages']
                pdf_stamp.annotate_fmeta_patch_pages(_chunk_pdf(book_dir, cid), fmeta_p, max_pg)
        try:
            _build_slim_synctex(_chunk_sync(book_dir, cid), _chunk_slim_sync(book_dir, cid),
                                cid + '.tex', max_pages=max_pg)
        except Exception:                   # noqa: BLE001
            pass

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


_LABEL_DEF_RE = re.compile(r'\\label\s*\{([^{}]*)\}')
_PAGEREF_ARG_RE = re.compile(r'\\(?:page|vpage|cpage)ref\s*\*?\s*\{([^{}]*)\}')


def _has_intra_pageref(text):
    """块内是否存在指向本块自身 \\label 的 \\pageref。"""
    if not text or 'pageref' not in text:
        return False
    defs = {m.group(1).strip() for m in _LABEL_DEF_RE.finditer(text)}
    if not defs:
        return False
    for m in _PAGEREF_ARG_RE.finditer(text):
        for nm in m.group(1).split(','):
            if nm.strip() in defs:
                return True
    return False


def _digit_bucket(page):
    """将目标页码映射到同位数奇数基准页，使块内 \\pageref 原生宽度与真实页码位数一致。"""
    if page >= 1000:
        return 1001
    if page >= 100:
        return 101
    if page >= 10:
        return 11
    return 1


def try_compile(book_dir, book_name, tex, env, log_cb=None):
    """快速路径（唯一编译路径）。返回结构化结果：status / reason_code / reason。

    默认启用页码解耦架构（对称页边距 + 标准页眉页脚风格）：
      - 每块脱离绝对页码按基准页单遍并行编译，输出 .fmeta 与末尾 Atlas 页眉图集；
      - 前置页（目录）按结构指纹缓存，仅页码变化时零重编、原位热更新 /FTOC；
      - 下游块页码偏移时零重编，合并阶段由 pdf_stamp 原位盖印页码/页眉并热更新 /FPR；
      - SyncTeX 由各块预处理的 .slim 文件与 PDF 盖印并行极速合并。
    非对称页边距或自定义 fancyhdr 等文档自动无缝回退经典迭代路径。
    """
    global LAST_SKIP, LAST_REASON, LAST_PHASES, LAST_CHUNKS
    LAST_SKIP = ''
    LAST_REASON = {}
    LAST_PHASES, LAST_CHUNKS = {}, {}
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
    labels_now = _agg_labels(book_dir)       # 上一次编译产出的跨块标签
    xrefs = [_xref_sig(c['text'], labels_now) for c in chunks]

    texcmd = 'xelatex'                      # 优先 PATH 解析（绝对路径在本机实测慢得多）
    texdir = os.path.dirname(tex) if tex else ''
    xelatex_alt = os.path.join(texdir, 'xelatex.exe') if texdir else ''
    xdvipdfmx_alt = os.path.join(texdir, 'xdvipdfmx.exe') if texdir else ''

    t0 = time.time()
    # 探测是否启用页码解耦架构（kit 仅在导言区变化时编译 1 次 ~1.3s，之后 <1ms 命中缓存）
    kit_info = None
    if pdf_stamp.can_decouple(doc['preamble'], doc['front'], chunks):
        kit_info = pdf_stamp.ensure_kit(book_dir, doc['preamble'], env,
                                        texcmd, xelatex_alt, xdvipdfmx_alt)
    decoupled = bool(kit_info)
    cache_mode_ok = (cache.get('ver') == CACHE_VER and bool(cache.get('decoupled')) == decoupled)

    def _chunk_sync_ready(cid):
        if _slim_sync_ok(_chunk_slim_sync(book_dir, cid)):
            return True
        return _sync_ok(_chunk_sync(book_dir, cid))

    def chunk_ok(i):
        if not cache_mode_ok:
            return False
        c = cached[i] if i < len(cached) else {}
        cid = chunks[i]['id']
        if not (c.get('id') == cid
                and isinstance(c.get('pages'), int) and c['pages'] >= 1
                and c.get('built') == hashes[i]
                and c.get('xref') == xrefs[i]
                and os.path.isfile(_chunk_pdf(book_dir, cid))
                and _chunk_sync_ready(cid)
                and os.path.isfile(_toc_path(book_dir, cid))):
            return False
        if decoupled and not os.path.isfile(_chunk_fmeta(book_dir, cid)):
            return False
        return True

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
        """前置页数与各块页数 → 每块起始页。

        章首 openright（奇数页起，必要时补空白）；章内子块紧接上一块继续排，
        不插空白页。
        """
        out = []
        pos = fro + 1
        for i in range(n):
            if chunks[i].get('openright', True) and pos % 2 == 0:
                pos += 1
            out.append(pos)
            pos += pages[i]
        return out

    front_pages = int(cache.get('front') or 0)
    starts = []
    for i in range(n):
        c = cached[i] if i < len(cached) else {}
        st = c.get('start')
        starts.append(st if isinstance(st, int) and st > 0 else 1)
    if any(s == 1 for s in starts):
        seed = _seed_from_synctex(book_dir, chunks) or _seed_from_pdf(book_dir, chunks)
        if seed:
            front_pages, starts = seed

    # 解耦模式下各块的相对编译基准页：
    # - 复用块沿用缓存时的 base_page（永不因页码漂移失效）；
    # - 新编块若含块内 \pageref 则用同位数奇数桶（1/11/101/1001），否则恒用 1。
    base_pages = []
    for i in range(n):
        if decoupled:
            c = cached[i] if (cache_mode_ok and i < len(cached)) else {}
            if i not in changed and isinstance(c.get('base_page'), int) and c['base_page'] >= 1:
                base_pages.append(c['base_page'])
            elif _has_intra_pageref(chunks[i]['text']):
                base_pages.append(_digit_bucket(starts[i]))
            else:
                base_pages.append(1)
        else:
            base_pages.append(starts[i])

    # 预刷新聚合 aux，供本轮编译的跨块 \ref / \pageref 读到最新标签与绝对页码
    init_deltas = ({chunks[i]['id']: starts[i] - base_pages[i] for i in range(n)}
                   if decoupled else None)
    _agg_aux(book_dir, [c['id'] for c in chunks], deltas=init_deltas,
             include_front=bool(front_pages and decoupled))

    def inject_of(i, start_page):
        # article 类（ctexart）没有 chapter/part 计数器：无条件 \setcounter 会在
        # -halt-on-error 下直接失败（MARXISM 实测）。有计数器才设，没有就跳过。
        s = ('\\ifcsname c@chapter\\endcsname\\setcounter{chapter}{%d}\\fi'
             '\\setcounter{page}{%d}') % (chapter_no[i], start_page)
        if part_no[i]:
            s += '\\ifcsname c@part\\endcsname\\setcounter{part}{%d}\\fi' % part_no[i]
        # 章内子块要接续整本的编号：section/图/表/公式/定理在整本里是连续的，
        # 子块独立编译会从 0 重新数。
        snap = chunks[i].get('counters') or {}
        for name in _COUNTERS + _THEOREM_COUNTERS:
            if snap.get(name):
                s += ('\\ifcsname c@%s\\endcsname\\setcounter{%s}{%d}\\fi'
                      % (name, name, snap[name]))
        # 页眉的章名/节名由 mark 决定；子块不含 \chapter，必须补回切点前的状态。
        mk = chunks[i].get('marks') or {}
        # ctexbook 的页眉按 \CTEX@ifname 决定是否印「第X章」前缀；子块没跑过
        # \chapter，这个开关还是初始的 false，需要手动置真。
        if mk.get('chapter') or mk.get('section'):
            s += ('\\ifcsname CTEX@ifnametrue\\endcsname'
                  '\\csname CTEX@ifnametrue\\endcsname \\fi')
        # 页眉取「本页第一个 mark」，而 \chaptermark 会把 right mark 清空，所以
        # 必须 sectionmark → chaptermark → sectionmark 这样夹着注入：
        # 首个 mark 决定首页页眉，末个 mark 决定后续页眉。
        sm = ('\\ifcsname sectionmark\\endcsname\\sectionmark{%s}\\fi'
              % mk.get('section', ''))
        if mk.get('section'):
            if mk.get('section_next'):
                # mark 在注入当时就把 \CTEXthesection 展开成节号，子块马上要进的
                # 那一节还没执行；临时把计数器 +1，让首页页眉印出正确节号。
                s += ('\\ifcsname c@section\\endcsname\\setcounter{section}{%d}\\fi'
                      % (snap.get('section', 0) + 1))
            s += sm
            if mk.get('section_next'):
                s += ('\\ifcsname c@section\\endcsname\\setcounter{section}{%d}\\fi'
                      % snap.get('section', 0))
        if mk.get('chapter'):
            s += ('\\ifcsname chaptermark\\endcsname'
                  '\\chaptermark{%s}\\fi' % mk['chapter'])
        if mk.get('section'):
            s += sm
        return s

    def uses_lastpage(*texts):
        """书稿是否用 lastpage（`\\pageref{LastPage}` 之类）。

        用了就不能忽略 aux 里的 `\\@abspage@last`，否则末页页码会停在上一遍。
        """
        return any(('LastPage' in (t or '')) or ('lastpage' in (t or '')) for t in texts)

    def compile_one(i, start_page, on_xelatex_done=None):
        try:
            return _compile_piece(book_dir, chunks[i]['id'], doc['preamble'],
                                  chunks[i]['text'], inject_of(i, start_page), env,
                                  texcmd, xelatex_alt, xdvipdfmx_alt,
                                  single_pass=(not decoupled and i not in changed),
                                  ignore_abspage=not uses_lastpage(doc['preamble'],
                                                                   chunks[i]['text']),
                                  decoupled=decoupled,
                                  on_xelatex_done=on_xelatex_done)
        except Exception as ex:             # noqa: BLE001
            return {'ok': False, 'out': str(ex), 'pdf': '', 'synctex': '', 'secs': 0}

    def compile_chunks(todo_list, page_list, on_xelatex_done=None):
        if not todo_list:
            return []
        procs = []
        sem = threading.Semaphore(MAX_PARALLEL)
        for i in todo_list:
            if log_cb:
                log_cb('[并行编译] %s（%s %d）' % (
                    chunks[i]['id'], '基准页' if decoupled else '起始页', page_list[i]))
            box = {}

            def work(ii=i, oo=page_list[i], bx=box, sm=sem):
                with sm:
                    bx['r'] = compile_one(ii, oo, on_xelatex_done=on_xelatex_done)

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

    def compile_serially(todo_list, page_list):
        """一块一块地编（并发失败后的兜底重试）。"""
        errs = []
        for i in todo_list:
            r = compile_one(i, page_list[i])
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

    def _run_batch(todo_list, page_list, on_xelatex_done=None):
        """执行一批块编译，自动处理便携 MiKTeX 格式争用。"""
        errors = compile_chunks(todo_list, page_list, on_xelatex_done=on_xelatex_done)
        if errors and len(todo_list) > 1 and format_race(errors):
            if log_cb:
                log_cb('[并行编译] 检测到便携 MiKTeX 格式文件争用，串行重建后重试失败块')
            rebuild_format()
            bad = [i for i in todo_list if chunks[i]['id'] in {c for c, _ in errors}]
            errors = compile_serially(bad, page_list)
        return errors

    front_pdf_p = _chunk_pdf(book_dir, FRONT_ID)
    front_sync_p = _chunk_sync(book_dir, FRONT_ID)
    front_has_toc = '\\tableofcontents' in (doc['front'] or '')

    front_memo = {'key': None, 'r': None}

    def compile_front(toc_text, struct_toc=None):
        """编前置页（封面/目录）。

        解耦模式下按 `struct_toc`（目录结构，不含页码）缓存：仅页码漂移时 0 次重编，
        由合并阶段原位热更新 `/FTOC`；结构变动时仅需编译 1 遍。
        """
        if not doc['front'].strip():
            return {'ok': True, 'pdf': '', 'synctex': '', 'pages': 0, 'fmeta': None}
        if decoupled:
            key = _front_hash(doc['preamble'], doc['front'],
                              struct_toc if front_has_toc else '')
        else:
            key = _front_hash(doc['preamble'], doc['front'], toc_text)
        if front_memo['key'] == key and front_memo['r'] and front_memo['r'].get('ok'):
            return front_memo['r']
        cached_key = cache.get('front_struct_hash') if decoupled else cache.get('front_hash')
        if (cache_mode_ok and cached_key == key and isinstance(cache.get('front'), int)
                and cache['front'] > 0 and os.path.isfile(front_pdf_p)
                and _chunk_sync_ready(FRONT_ID)):
            if decoupled:
                fm = pdf_stamp.parse_fmeta(_chunk_fmeta(book_dir, FRONT_ID))
                if fm and fm['body_pages'] > 0:
                    return {'ok': True, 'pdf': front_pdf_p, 'synctex': front_sync_p,
                            'pages': fm['body_pages'], 'fmeta': fm, 'reused': True}
            else:
                return {'ok': True, 'pdf': front_pdf_p, 'synctex': front_sync_p,
                        'pages': cache['front'], 'fmeta': None, 'reused': True}
        try:
            r = _compile_piece(book_dir, FRONT_ID, doc['preamble'], doc['front'],
                               '\\setcounter{page}{1}', env,
                               texcmd, xelatex_alt, xdvipdfmx_alt, toc_seed=toc_text,
                               ignore_abspage=not uses_lastpage(doc['preamble'], doc['front']),
                               decoupled=decoupled)
        except Exception as ex:             # noqa: BLE001
            return {'ok': False, 'out': str(ex)}
        if r.get('ok'):
            if decoupled:
                fm = pdf_stamp.parse_fmeta(_chunk_fmeta(book_dir, FRONT_ID))
                r['fmeta'] = fm
                r['pages'] = fm['body_pages'] if fm else None
            else:
                r['fmeta'] = None
                r['pages'] = _pages_of(r['pdf'])
            if r.get('pages'):
                front_memo['key'] = key
                front_memo['r'] = r
        return r

    def _agg_sig():
        """聚合 aux 的当前内容指纹（'' = 还没有这份文件）。"""
        try:
            with open(os.path.join(book_dir, F_DIR, 'agg', 'main.aux'), 'rb') as f:
                return hashlib.md5(f.read()).hexdigest()
        except OSError:
            return ''

    def _undefined_refs(cid):
        """该块的日志里仍为 undefined 的标签集合。"""
        try:
            with open(os.path.join(_chunk_dir(book_dir, cid), 'wrapper.log'),
                      encoding='utf-8', errors='replace') as f:
                return set(re.findall(r"Reference `([^']*)' .*undefined", f.read()))
        except OSError:
            return set()

    def _undefined_ref(cid):
        return bool(_undefined_refs(cid))

    rebuilt_set = set(changed)
    master_toc = ''
    struct_toc = ''
    toc_pages = []
    label_pages = {}
    front_pdf = ''
    front_fmeta = None
    fmetas = [None] * n
    pages = []

    if decoupled:
        # ---------- 页码解耦单轮快速路径（页码漂移 0 重编） ----------
        def _collect_meta_and_tocs():
            cur_pages = []
            cur_fmetas = []
            cur_raw_tocs = []
            missing = []
            for i in range(n):
                cid = chunks[i]['id']
                fm = pdf_stamp.parse_fmeta(_chunk_fmeta(book_dir, cid))
                t = _read_toc(book_dir, cid)
                if not fm or fm['body_pages'] < 1 or t is None:
                    missing.append(i)
                    cur_pages.append(1)
                    cur_fmetas.append(fm)
                    cur_raw_tocs.append(t or '')
                else:
                    cur_pages.append(fm['body_pages'])
                    cur_fmetas.append(fm)
                    cur_raw_tocs.append(t)
            return cur_pages, cur_fmetas, cur_raw_tocs, missing

        # 当所有 changed 块的 xelatex 刚写完 .toc/.fmeta 时，若目录结构已变，
        # 立刻并行起跑 front 编译，与最后一个块的 xdvipdfmx + slim 重叠（省 ~0.6s）。
        early_front = {'th': None, 'struct': None, 'res': None}
        if changed and doc['front'].strip():
            xe_lock = threading.Lock()
            xe_rem = [len(changed)]

            def _on_xe_done():
                with xe_lock:
                    xe_rem[0] -= 1
                    if xe_rem[0] != 0:
                        return
                c_pages, _c_fm, c_tocs, c_miss = _collect_meta_and_tocs()
                if c_miss:
                    return
                e_starts = layout(c_pages, front_pages)
                e_toc = ''.join(
                    pdf_stamp.shift_toc_pages(c_tocs[i], e_starts[i] - base_pages[i])
                    for i in range(n)
                )
                s_toc, w_toc, _ = pdf_stamp.prepare_toc_for_front(e_toc)
                f_key = _front_hash(doc['preamble'], doc['front'],
                                    s_toc if front_has_toc else '')
                if (cache_mode_ok and cache.get('front_struct_hash') == f_key
                        and isinstance(cache.get('front'), int) and cache['front'] > 0
                        and os.path.isfile(front_pdf_p) and _chunk_sync_ready(FRONT_ID)):
                    return
                early_front['struct'] = s_toc

                def _run_front():
                    early_front['res'] = compile_front(w_toc, struct_toc=s_toc)

                th = threading.Thread(target=_run_front)
                early_front['th'] = th
                th.start()
        else:
            _on_xe_done = None

        if changed:
            errors = _run_batch(changed, base_pages, on_xelatex_done=_on_xe_done)
            if errors:
                if early_front['th'] is not None:
                    early_front['th'].join()
                for cid, out in errors:
                    log_cb('[并行编译失败] %s\n%s' % (cid, (out or '')[-4000:]))
                return fail('chunk_compile_failed',
                            '块编译失败：%s' % '; '.join(c for c, _ in errors))

        pages, fmetas, raw_tocs, missing = _collect_meta_and_tocs()
        if missing:
            if early_front['th'] is not None:
                early_front['th'].join()
                early_front['th'] = None
                early_front['res'] = None
            if log_cb:
                log_cb('[解耦补编] 缺元数据/目录条目：%s'
                       % ', '.join(chunks[i]['id'] for i in missing))
            rebuilt_set.update(missing)
            errors = _run_batch(missing, base_pages)
            if errors:
                for cid, out in errors:
                    log_cb('[并行编译失败] %s\n%s' % (cid, (out or '')[-4000:]))
                return fail('chunk_compile_failed',
                            '块编译失败：%s' % '; '.join(c for c, _ in errors))
            pages, fmetas, raw_tocs, missing = _collect_meta_and_tocs()
            if missing:
                return fail('chunk_pages_unreadable',
                            '块 %s 的元数据/目录读不出' % chunks[missing[0]]['id'])

        # 组装预估目录并决定是否复用前置页（结构不变则 0 编译；已抢跑则直接回收结果）
        est_starts = layout(pages, front_pages)
        est_toc = ''.join(
            pdf_stamp.shift_toc_pages(raw_tocs[i], est_starts[i] - base_pages[i])
            for i in range(n)
        )
        struct_toc, wrapped_toc, _ = pdf_stamp.prepare_toc_for_front(est_toc)

        if early_front['th'] is not None:
            early_front['th'].join()
        if early_front['res'] is not None and early_front['struct'] == struct_toc:
            fr = early_front['res']
        else:
            fr = compile_front(wrapped_toc, struct_toc=struct_toc)
        if not fr.get('ok') or (doc['front'].strip() and not fr.get('pages')):
            log_cb('[前置页编译失败]\n%s' % ((fr.get('out') or '')[-4000:]))
            return fail('front_compile_failed', '前置页（封面/目录）编译失败')
        front_pages = fr.get('pages') or 0
        front_pdf = fr.get('pdf') or ''
        front_fmeta = fr.get('fmeta')

        # 精确布局 + 精确平移目录页码与全局 \newlabel 绝对页码
        starts = layout(pages, front_pages)
        deltas = {chunks[i]['id']: starts[i] - base_pages[i] for i in range(n)}
        label_pages = _agg_aux(book_dir, [c['id'] for c in chunks],
                               deltas=deltas, include_front=bool(front_pages))
        labels_new = _agg_labels(book_dir)
        label_names_new = set(labels_new)

        # 仅当「非页码」引用定义变更（编号/锚点变更或新标签就绪）时才补编受影响块
        stale = [
            i for i in range(n)
            if _xref_sig(chunks[i]['text'], labels_new) != xrefs[i]
            or bool(_undefined_refs(chunks[i]['id']) & label_names_new)
        ]
        if stale:
            if log_cb:
                log_cb('[跨块引用] 标签编号/锚点变更，补编：%s'
                       % ', '.join(chunks[i]['id'] for i in stale))
            rebuilt_set.update(stale)
            errors = _run_batch(stale, base_pages)
            if errors:
                for cid, out in errors:
                    log_cb('[并行编译失败] %s\n%s' % (cid, (out or '')[-4000:]))
                return fail('chunk_compile_failed',
                            '块编译失败：%s' % '; '.join(c for c, _ in errors))
            pages, fmetas, raw_tocs, missing = _collect_meta_and_tocs()
            if missing:
                return fail('chunk_pages_unreadable',
                            '块 %s 的元数据/目录读不出' % chunks[missing[0]]['id'])
            est_starts = layout(pages, front_pages)
            est_toc = ''.join(
                pdf_stamp.shift_toc_pages(raw_tocs[i], est_starts[i] - base_pages[i])
                for i in range(n)
            )
            new_struct_toc, wrapped_toc, _ = pdf_stamp.prepare_toc_for_front(est_toc)
            if new_struct_toc != struct_toc:
                struct_toc = new_struct_toc
                fr = compile_front(wrapped_toc, struct_toc=struct_toc)
                if not fr.get('ok') or (doc['front'].strip() and not fr.get('pages')):
                    log_cb('[前置页编译失败]\n%s' % ((fr.get('out') or '')[-4000:]))
                    return fail('front_compile_failed', '前置页（封面/目录）编译失败')
                front_pages = fr.get('pages') or 0
                front_pdf = fr.get('pdf') or ''
                front_fmeta = fr.get('fmeta')
            starts = layout(pages, front_pages)
            deltas = {chunks[i]['id']: starts[i] - base_pages[i] for i in range(n)}
            label_pages = _agg_aux(book_dir, [c['id'] for c in chunks],
                                   deltas=deltas, include_front=bool(front_pages))
            labels_new = _agg_labels(book_dir)

        xrefs = [_xref_sig(c['text'], labels_new) for c in chunks]
        master_toc = ''.join(
            pdf_stamp.shift_toc_pages(raw_tocs[i], starts[i] - base_pages[i])
            for i in range(n)
        )
        struct_toc, _, toc_pages = pdf_stamp.prepare_toc_for_front(master_toc)
    else:
        # ---------- 经典回退路径（非对称页边距/自定义 fancyhdr 等） ----------
        todo = list(changed)
        ok_layout = False
        for _rounds in range(4):
            agg_before = _agg_sig()
            errors = _run_batch(todo, starts)
            if errors:
                for cid, out in errors:
                    log_cb('[并行编译失败] %s\n%s' % (cid, (out or '')[-4000:]))
                return fail('chunk_compile_failed',
                            '块编译失败：%s' % '; '.join(c for c, _ in errors))

            _agg_aux(book_dir, [c['id'] for c in chunks])
            if _agg_sig() != agg_before:
                labels_new = _agg_labels(book_dir)
                stale = [i for i in range(n)
                         if _xref_sig(chunks[i]['text'], labels_new) != xrefs[i]
                         or _undefined_ref(chunks[i]['id'])]
                if stale:
                    if log_cb:
                        log_cb('[跨块引用] 聚合 aux 已更新，补编：%s'
                               % ', '.join(chunks[i]['id'] for i in stale))
                    rebuilt_set.update(stale)
                    errors = _run_batch(stale, starts)
                    if errors:
                        for cid, out in errors:
                            log_cb('[并行编译失败] %s\n%s' % (cid, (out or '')[-4000:]))
                        return fail('chunk_compile_failed',
                                    '块编译失败：%s' % '; '.join(c for c, _ in errors))
                    _agg_aux(book_dir, [c['id'] for c in chunks])
                    labels_new = _agg_labels(book_dir)
                xrefs = [_xref_sig(c['text'], labels_new) for c in chunks]

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
                rebuilt_set.update(missing)
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
            rebuilt_set.update(shifted)
            starts, front_pages, todo = new_starts, new_front, shifted
        if not ok_layout:
            return fail('layout_unstable', '页码布局 4 轮迭代仍未收敛（前置页/分页不稳定）')

    offsets = [s - 1 for s in starts]
    parts = []
    pos = front_pages
    if decoupled:
        if front_pages:
            parts.append(('pages', front_pdf, 0, front_pages, 1, front_fmeta))
        blank_sz = tuple(kit_info['page_size'])
        for i in range(n):
            pdf = _chunk_pdf(book_dir, chunks[i]['id'])
            if starts[i] - 1 > pos:
                parts.append(('blanks', starts[i] - 1 - pos, blank_sz))
                pos = starts[i] - 1
            parts.append(('pages', pdf, 0, pages[i], starts[i], fmetas[i]))
            pos += pages[i]
    else:
        if front_pages:
            parts.append(('pages', front_pdf, 0, front_pages))
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
        except Exception as ex:             # noqa: BLE001
            return fail('read_chunk_pdf_failed', '读取块 PDF 失败：%s' % ex)

    outline = _parse_toc(master_toc)
    main_pdf = os.path.join(book_dir, 'main.pdf')
    merged = os.path.join(book_dir, '.build', '_floria_main.pdf')
    sync_tmp = os.path.join(book_dir, '.build', '_floria_main.synctex.gz')

    items = []
    if front_pages:
        items.append({'path': _chunk_sync(book_dir, FRONT_ID),
                      'slim': _chunk_slim_sync(book_dir, FRONT_ID),
                      'max_pages': front_pages if decoupled else None,
                      'offset': 0,
                      'chunk_name': FRONT_ID + '.tex', 'chunk_line': doc['front_line']})
    for i in range(n):
        cid = chunks[i]['id']
        items.append({'path': _chunk_sync(book_dir, cid),
                      'slim': _chunk_slim_sync(book_dir, cid),
                      'max_pages': pages[i] if decoupled else None,
                      'offset': offsets[i],
                      'chunk_name': cid + '.tex', 'chunk_line': chunks[i]['line']})

    # 顺序执行 PDF 盖印合并（~0.5s）与 SyncTeX 极速合并（~0.15s）：
    # 两者均为纯 Python CPU 密集任务，实测顺序执行比双线程（受 GIL 争用约 1.26s）快一倍。
    t_pdf0 = time.time()
    try:
        if decoupled:
            total_pages = pdf_stamp.stamp_and_merge_pdfs(
                parts, merged, kit_info, outline=outline,
                toc_pages=toc_pages, label_pages=label_pages)
        else:
            total_pages = _merge_pdfs(parts, merged, outline=outline)
    except Exception as ex:                 # noqa: BLE001
        return fail('merge_pdf_failed', '拼 PDF/重建书签失败：%s' % ex)
    t_pdf = time.time()

    t_sync0 = time.time()
    try:
        _merge_synctex_cached(items, sync_tmp)
    except Exception as ex:                 # noqa: BLE001
        if log_cb:
            log_cb('[synctex 合并失败] %s' % ex)
        return fail('synctex_merge_failed', 'synctex 合并失败：%s' % ex)
    t_sync = time.time()

    # PDF 与 synctex 都合并成功后才落盘：任一失败都不替换成品，
    # 避免出现「PDF 已换新、点开却全无映射」的撕裂状态。
    try:
        os.replace(merged, main_pdf)
        os.replace(sync_tmp, os.path.join(book_dir, 'main.synctex.gz'))
    except OSError as ex:
        return fail('commit_failed', '替换成品失败：%s' % ex)
    t_done = time.time()
    if not total_pages:
        total_pages = _pages_of(main_pdf)
    rebuilt = len(rebuilt_set)
    LAST_PHASES = {'chunks': round(t_pdf0 - t0, 2),
                   'pdf_merge': round(t_pdf - t_pdf0, 2),
                   'synctex': round(t_sync - t_sync0, 2),
                   'commit': round(t_done - t_sync, 2)}
    LAST_CHUNKS = {'total': n, 'rebuilt': rebuilt, 'reused': n - rebuilt}

    newcache = [{'id': chunks[i]['id'], 'hash': hashes[i], 'built': hashes[i],
                 'xref': xrefs[i], 'pages': pages[i], 'start': starts[i],
                 'base_page': base_pages[i]} for i in range(n)]
    cache_rec = {
        'front': front_pages,
        'chunks': newcache,
        'decoupled': decoupled,
        'front_hash': _front_hash(doc['preamble'], doc['front'], master_toc),
        'ts': round(time.time(), 3),
    }
    if decoupled:
        cache_rec['front_struct_hash'] = _front_hash(
            doc['preamble'], doc['front'], struct_toc if front_has_toc else '')
    _write_cache(book_dir, cache_rec)
    if not decoupled:
        _agg_aux(book_dir, [c['id'] for c in chunks])
    elapsed = round(time.time() - t0, 2)
    mode_tag = '页码解耦并行' if decoupled else '自动分章并行'
    log = '\n'.join([
        '[编译链] %s（%d 块，重编 %d 块，前置页 %d 页）'
        % (mode_tag, n, rebuilt, front_pages),
        '[编译链] 未变块复用缓存：%s'
        % (', '.join(chunks[i]['id'] for i in range(n) if i not in rebuilt_set) or '无'),
        '[编译链] 拼接 %d 个 PDF（%s）+ 按目录条目重建书签（%d 项）'
        % (n + (1 if front_pages else 0),
           '原位盖印+热更新' if decoupled else 'pypdf', len(outline)),
        '[编译链] 各块起始页：%s'
        % ' / '.join('%s=%d' % (chunks[i]['id'], starts[i]) for i in range(n)),
    ])
    return {'status': STATUS_OK, 'reason_code': None, 'reason': None,
            'ok': True, 'time': elapsed, 'mtime': int(os.path.getmtime(main_pdf) * 1000),
            'pages': total_pages, 'log': log, 'fast': True}
