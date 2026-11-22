# -*- coding: utf-8 -*-
"""TikZ 图元透明批处理缓存模块。

针对含大量 \\begin{tikzpicture}（如 CHEMISTERY/ch3_9 的 108 个 TikZ + 173 个 chemfig）
的章节：将自包含的 tikzpicture 块批量预编译为紧裁单页 PDF 并记录基线深度（\\dp），
缓存到 `.build/floria/tikz/`；编译块正文时以等行数的
`\\raisebox{-<dp>pt}{\\includegraphics{...}}` 原位替换，严格保持 SyncTeX 行号不变。

任何单批预编译失败或含上下文依赖命令（remember picture/overlay/\\ref/\\label 等）的
图元均自动回退原样编译，绝不破坏正常编译链路。
"""
import hashlib
import os
import re
import shutil
import subprocess
import threading

from . import pdf_stamp

TIKZ_CACHE_VER = 1
MIN_PICTURES = 1
MAX_BATCH_WORKERS = max(2, min(6, os.cpu_count() or 4))

_BEGIN_RE = re.compile(r'\\begin\s*\{tikzpicture\}')
_END_RE = re.compile(r'\\end\s*\{tikzpicture\}')
_SCHEMESTART_RE = re.compile(r'\\schemestart(?![A-Za-z@])')
_SCHEMESTOP_RE = re.compile(r'\\schemestop(?![A-Za-z@])')
_CHEMFIG_RE = re.compile(r'\\chemfig(?![A-Za-z@])')
_SETCHEMFIG_RE = re.compile(r'\\setchemfig\s*\{')

_UNSAFE_RE = re.compile(
    r'remember\s+picture|\boverlay\b|#1|#2|\\def\\|@\{|'
    r'\\(?:ref|pageref|eqref|autoref|cref|Cref|cite|label|footnote|href|url|'
    r'thepage|thechapter|thesection|thefigure|thetable|tikzmark|'
    r'newcommand|renewcommand|chemmove)(?![A-Za-z@])'
)


def _is_commented(text, pos):
    ls = text.rfind('\n', 0, pos) + 1
    i = ls
    while i < pos:
        if text[i] == '%':
            bs = 0
            j = i - 1
            while j >= ls and text[j] == '\\':
                bs += 1
                j -= 1
            if bs % 2 == 0:
                return True
        i += 1
    return False


def _read_bracket_opt(s, i):
    """若 s[i] == '['，读取平衡方括号可选参数（兼容内部花括号嵌套），返回右括号后下标。"""
    if i >= len(s) or s[i] != '[':
        return i
    b_depth = 0
    c_depth = 0
    j = i
    n = len(s)
    while j < n:
        ch = s[j]
        if ch == '\\':
            j += 2
            continue
        if ch == '{':
            c_depth += 1
        elif ch == '}':
            if c_depth > 0:
                c_depth -= 1
        elif c_depth == 0:
            if ch == '[':
                b_depth += 1
            elif ch == ']':
                b_depth -= 1
                if b_depth == 0:
                    return j + 1
        j += 1
    return -1


def _pair_delimited_spans(text, begin_re, end_re):
    events = []
    for m in begin_re.finditer(text):
        if not _is_commented(text, m.start()):
            events.append((m.start(), m.end(), 1))
    for m in end_re.finditer(text):
        if not _is_commented(text, m.start()):
            events.append((m.start(), m.end(), -1))
    if not events:
        return []
    events.sort(key=lambda x: x[0])
    depth = 0
    cur_start = -1
    spans = []
    for st, ed, delta in events:
        if delta == 1:
            if depth == 0:
                cur_start = st
            depth += 1
        else:
            depth -= 1
            if depth == 0 and cur_start >= 0:
                spans.append((cur_start, ed))
                cur_start = -1
            elif depth < 0:
                depth = 0
    return spans


def _overlaps_any(st, ed, occupied):
    """occupied 已按 start 升序且互不重叠。"""
    import bisect
    idx = bisect.bisect_right(occupied, (st, 10 ** 12)) - 1
    if idx >= 0 and occupied[idx][1] > st:
        return True
    if idx + 1 < len(occupied) and occupied[idx + 1][0] < ed:
        return True
    return False


def _collect_scoped_setchemfig(text, query_positions):
    """按 TeX 花括号组作用域单遍追踪每个位置生效的 \\setchemfig{...}。"""
    if not query_positions:
        return {}
    targets = sorted(set(query_positions))
    t_idx = 0
    t_len = len(targets)
    max_pos = targets[-1]
    res = {}
    stack = ['']
    i = 0
    n = min(len(text), max_pos + 1)
    while i < n and t_idx < t_len:
        while t_idx < t_len and targets[t_idx] <= i:
            res[targets[t_idx]] = stack[-1]
            t_idx += 1
        if t_idx >= t_len:
            break
        ch = text[i]
        if ch == '%':
            nl = text.find('\n', i + 1)
            i = n if nl < 0 else nl + 1
            continue
        if ch == '\\':
            if text.startswith('\\setchemfig', i) and (
                i + 11 >= len(text) or not (text[i + 11].isalpha() or text[i + 11] == '@')
            ):
                k = pdf_stamp._skip_ws(text, i + 11)
                grp, nxt = pdf_stamp._read_group(text, k)
                if grp is not None:
                    cmd = '\\setchemfig{' + grp + '}'
                    # 同一作用域重复设置时：若无外层继承则保留最新一条，若在外层之上局部覆盖则串接
                    parent = stack[-2] if len(stack) >= 2 else ''
                    stack[-1] = (parent + cmd) if parent else cmd
                    i = nxt
                    continue
            i += 2
            continue
        if ch == '{':
            stack.append(stack[-1])
        elif ch == '}':
            if len(stack) > 1:
                stack.pop()
        i += 1
    while t_idx < t_len:
        res[targets[t_idx]] = stack[-1]
        t_idx += 1
    return res


def find_cacheable_pictures(text):
    """扫描块文本中所有顶层可缓存的 tikzpicture / \\schemestart...\\schemestop / 独立 \\chemfig。

    返回列表：[(start, end, code, setchemfig_cmd), ...]（按 start 升序，互不重叠）。
    """
    est = (
        text.count('tikzpicture') // 2
        + text.count('\\schemestart')
        + text.count('\\chemfig')
    )
    if est < MIN_PICTURES:
        return []

    occupied = []
    candidates = []

    # 1. 顶层 \begin{tikzpicture}...\end{tikzpicture}
    for st, ed in _pair_delimited_spans(text, _BEGIN_RE, _END_RE):
        occupied.append((st, ed))
        code = text[st:ed]
        if _BEGIN_RE.search(code, len('\\begin{tikzpicture}')):
            continue
        candidates.append((st, ed, 'tikz'))

    # 2. 顶层 \schemestart...\schemestop（不在 tikzpicture 内）
    if '\\schemestart' in text:
        occ_sorted = sorted(occupied)
        for st, ed in _pair_delimited_spans(text, _SCHEMESTART_RE, _SCHEMESTOP_RE):
            if _overlaps_any(st, ed, occ_sorted):
                continue
            occupied.append((st, ed))
            code = text[st:ed]
            if _SCHEMESTART_RE.search(code, len('\\schemestart')):
                continue
            candidates.append((st, ed, 'scheme'))

    # 3. 顶层独立 \chemfig*?[...]{...}（不在 tikzpicture / schemestart 内）
    if '\\chemfig' in text:
        occ_sorted = sorted(occupied)
        for m in _CHEMFIG_RE.finditer(text):
            st = m.start()
            if _overlaps_any(st, st + 8, occ_sorted) or _is_commented(text, st):
                continue
            j = m.end()
            if j < len(text) and text[j] == '*':
                j += 1
            j = pdf_stamp._skip_ws(text, j)
            if j < len(text) and text[j] == '[':
                j = _read_bracket_opt(text, j)
                if j < 0:
                    continue
                j = pdf_stamp._skip_ws(text, j)
            grp, ed = pdf_stamp._read_group(text, j)
            if grp is None or _overlaps_any(st, ed, occ_sorted):
                continue
            occupied.append((st, ed))
            occ_sorted = sorted(occupied)
            candidates.append((st, ed, 'chemfig'))

    if len(candidates) < MIN_PICTURES:
        return []

    candidates.sort(key=lambda x: x[0])
    filtered = []
    chem_positions = []
    for st, ed, kind in candidates:
        code = text[st:ed]
        if _UNSAFE_RE.search(code):
            continue
        pre = text[max(0, st - 120):st]
        if re.search(r'\\(?:new|renew)command\b|\\def\b|\\chemname\b|\\polymerdelim\b', pre):
            continue
        needs_scfg = (kind != 'tikz') or ('\\chemfig' in code) or ('\\schemestart' in code)
        if needs_scfg:
            chem_positions.append(st)
        filtered.append((st, ed, code, needs_scfg))

    if len(filtered) < MIN_PICTURES:
        return []

    scfg_map = _collect_scoped_setchemfig(text, chem_positions) if chem_positions else {}
    out = [
        (st, ed, code, scfg_map.get(st, '') if needs_scfg else '')
        for st, ed, code, needs_scfg in filtered
    ]
    return out if len(out) >= MIN_PICTURES else []


def _cache_dir(book_dir):
    return os.path.join(book_dir, '.build', 'floria', 'tikz')


def _item_paths(book_dir, key):
    d = _cache_dir(book_dir)
    return os.path.join(d, key + '.pdf'), os.path.join(d, key + '.dp')


def _read_dims(dp_path, pdf_path=None):
    """读取缓存图元的 (dp_pt, wd_pt, tot_pt)；兼容单浮点旧 `.dp` 并自动原地升级。"""
    try:
        with open(dp_path, encoding='utf-8') as f:
            parts = f.read().strip().split()
        if not parts:
            return None
        dp_pt = float(parts[0])
        if len(parts) >= 3:
            return dp_pt, float(parts[1]), float(parts[2])
        if pdf_path and os.path.isfile(pdf_path):
            import pypdf
            mb = pypdf.PdfReader(pdf_path).pages[0].mediabox
            wd_pt = float(mb.width) * 72.27 / 72.0
            tot_pt = float(mb.height) * 72.27 / 72.0
            tmp = dp_path + '.%d.up' % os.getpid()
            try:
                with open(tmp, 'w', encoding='utf-8') as wf:
                    wf.write('%.5f %.5f %.5f\n' % (dp_pt, wd_pt, tot_pt))
                os.replace(tmp, dp_path)
            except OSError:
                pass
            return dp_pt, wd_pt, tot_pt
    except Exception:
        return None
    return None


def _batch_tex(preamble, items):
    """把一批 (key, code, scfg) 拼成紧裁多页 TeX 源（每图一页，原生测量 \\wd/\\ht/\\dp）。"""
    clean_pre = pdf_stamp._remove_pkg_from_preamble(preamble, 'hyperref')
    parts = [
        clean_pre,
        r"""
\makeatletter
\newwrite\floria@tmeta
\immediate\openout\floria@tmeta=\jobname.tmeta
\newbox\floria@tbox
\newenvironment{FloriaTikzBox}[1]{%
  \def\floria@tkey{#1}%
  \setbox\floria@tbox=\hbox\bgroup\ignorespaces
}{%
  \unskip\egroup
  \immediate\write\floria@tmeta{T \floria@tkey\space \the\wd\floria@tbox\space \the\ht\floria@tbox\space \the\dp\floria@tbox}%
  \pdfpagewidth=\wd\floria@tbox
  \pdfpageheight=\dimexpr\ht\floria@tbox+\dp\floria@tbox\relax
  \ifdim\pdfpagewidth<1pt \pdfpagewidth=1pt\fi
  \ifdim\pdfpageheight<1pt \pdfpageheight=1pt\fi
  \shipout\vbox to\pdfpageheight{\kern0pt\box\floria@tbox\vss}%
}
\AtEndDocument{\immediate\closeout\floria@tmeta}
\makeatother
\begin{document}
\hoffset=-1in
\voffset=-1in
""",
    ]
    for key, code, scfg in items:
        if scfg:
            parts.append('{%s\n\\begin{FloriaTikzBox}{%s}\n%s\n\\end{FloriaTikzBox}\n}\n'
                         % (scfg, key, code))
        else:
            parts.append('\\begin{FloriaTikzBox}{%s}\n%s\n\\end{FloriaTikzBox}\n'
                         % (key, code))
    parts.append('\\end{document}\n')
    return '\n'.join(parts)


def _compile_batch(book_dir, preamble, items, batch_id, env,
                   texcmd, xelatex_alt, xdvipdfmx_alt):
    """编译单个 TikZ 批次并把单页 PDF + (dp, wd, tot) 写入缓存目录。"""
    cdir = _cache_dir(book_dir)
    bdir = os.path.join(cdir, '_batch_%d_%d' % (os.getpid(), batch_id))
    os.makedirs(bdir, exist_ok=True)
    rel_bdir = os.path.relpath(bdir, book_dir).replace('\\', '/')
    tex_p = os.path.join(bdir, 'batch.tex')
    with open(tex_p, 'w', encoding='utf-8') as f:
        f.write(_batch_tex(preamble, items))

    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    e = env.copy()
    xe_bin = xelatex_alt if (xelatex_alt and os.path.isfile(xelatex_alt)) else texcmd
    xdv_bin = xdvipdfmx_alt if (xdvipdfmx_alt and os.path.isfile(xdvipdfmx_alt)) else 'xdvipdfmx'
    try:
        def _xe_cmd(cmd_bin):
            args = [cmd_bin]
            if 'miktex' in cmd_bin.lower():
                args.append('--disable-installer')
            args.extend(['-no-pdf', '-interaction=nonstopmode', '-halt-on-error',
                         '-output-directory=' + rel_bdir, rel_bdir + '/batch.tex'])
            return args

        try:
            p1 = subprocess.run(
                _xe_cmd(xe_bin),
                cwd=book_dir, timeout=120, capture_output=True, text=True,
                encoding='utf-8', errors='replace', creationflags=flags, env=e,
            )
        except FileNotFoundError:
            fallback = xelatex_alt if xe_bin != xelatex_alt else texcmd
            if not fallback:
                return 0
            p1 = subprocess.run(
                _xe_cmd(fallback),
                cwd=book_dir, timeout=120, capture_output=True, text=True,
                encoding='utf-8', errors='replace', creationflags=flags, env=e,
            )
        if p1.returncode != 0:
            return 0
        try:
            p2 = subprocess.run(
                [xdv_bin, '-E', '-z', '1', '-o', rel_bdir + '/batch.pdf',
                 rel_bdir + '/batch.xdv'],
                cwd=book_dir, timeout=120, capture_output=True, text=True,
                encoding='utf-8', errors='replace', creationflags=flags, env=e,
            )
        except FileNotFoundError:
            fallback_xdv = xdvipdfmx_alt if xdv_bin != xdvipdfmx_alt else 'xdvipdfmx'
            if not fallback_xdv:
                return 0
            p2 = subprocess.run(
                [fallback_xdv, '-E', '-z', '1', '-o', rel_bdir + '/batch.pdf',
                 rel_bdir + '/batch.xdv'],
                cwd=book_dir, timeout=120, capture_output=True, text=True,
                encoding='utf-8', errors='replace', creationflags=flags, env=e,
            )
        if p2.returncode != 0:
            return 0

        meta_p = os.path.join(bdir, 'batch.tmeta')
        pdf_p = os.path.join(bdir, 'batch.pdf')
        if not (os.path.isfile(meta_p) and os.path.isfile(pdf_p)):
            return 0

        entries = []
        with open(meta_p, encoding='utf-8') as f:
            for ln in f:
                ps = ln.strip().split()
                if len(ps) >= 5 and ps[0] == 'T':
                    wd_pt = pdf_stamp._parse_pt(ps[2])
                    ht_pt = pdf_stamp._parse_pt(ps[3])
                    dp_pt = pdf_stamp._parse_pt(ps[4])
                    entries.append((ps[1], dp_pt, wd_pt, ht_pt + dp_pt))

        import pypdf
        r = pypdf.PdfReader(pdf_p)
        if len(r.pages) != len(entries):
            return 0

        saved = 0
        for idx, (key, dp_pt, wd_pt, tot_pt) in enumerate(entries):
            out_pdf, out_dp = _item_paths(book_dir, key)
            w = pypdf.PdfWriter()
            w.add_page(r.pages[idx])
            tmp_pdf = out_pdf + '.%d.tmp' % os.getpid()
            with open(tmp_pdf, 'wb') as f:
                w.write(f)
            os.replace(tmp_pdf, out_pdf)
            tmp_dp = out_dp + '.%d.tmp' % os.getpid()
            with open(tmp_dp, 'w', encoding='utf-8') as f:
                f.write('%.5f %.5f %.5f\n' % (dp_pt, wd_pt, tot_pt))
            os.replace(tmp_dp, out_dp)
            saved += 1
        return saved
    except Exception:
        return 0
    finally:
        shutil.rmtree(bdir, ignore_errors=True)


def preprocess_chunk_tikz(book_dir, preamble, text, env,
                          texcmd='xelatex', xelatex_alt='', xdvipdfmx_alt=''):
    """将块内可缓存的 tikzpicture / schemestart / chemfig 替换为零 Poppler 开销的 \\FloriaPdf（严格等行数）。

    返回 (new_text, stats_dict)。任何异常均安全回退为原文本。
    """
    pre_nc = pdf_stamp._strip_comments(preamble or '')
    spans = find_cacheable_pictures(text)
    if not spans:
        return text, {'total': 0, 'hit': 0, 'built': 0}

    os.makedirs(_cache_dir(book_dir), exist_ok=True)
    pre_sig = hashlib.md5((str(TIKZ_CACHE_VER) + '\0' + pre_nc).encode('utf-8', 'replace')).hexdigest()[:12]

    items_with_key = []
    misses = {}
    for st, ed, code, scfg in spans:
        h = hashlib.md5((pre_sig + '\0' + scfg + '\0' + code).encode('utf-8', 'replace')).hexdigest()[:20]
        items_with_key.append((st, ed, code, scfg, h))
        pdf_p, dp_p = _item_paths(book_dir, h)
        if not (os.path.isfile(pdf_p) and _read_dims(dp_p, pdf_p) is not None):
            misses.setdefault(h, (h, code, scfg))

    hit_count = len(spans) - len([1 for _, _, _, _, h in items_with_key if h in misses])
    built_count = 0

    if misses:
        miss_list = list(misses.values())
        n_batches = min(MAX_BATCH_WORKERS, max(1, (len(miss_list) + 14) // 15))
        if n_batches == 1:
            built_count = _compile_batch(book_dir, preamble, miss_list, 0, env,
                                         texcmd, xelatex_alt, xdvipdfmx_alt)
        else:
            # 轮询分配，保证各批次大小均衡
            buckets = [[] for _ in range(n_batches)]
            for idx, it in enumerate(miss_list):
                buckets[idx % n_batches].append(it)
            results = [0] * n_batches
            threads = []
            for bi, bucket in enumerate(buckets):
                def _work(idx=bi, bkt=bucket):
                    results[idx] = _compile_batch(book_dir, preamble, bkt, idx, env,
                                                  texcmd, xelatex_alt, xdvipdfmx_alt)
                th = threading.Thread(target=_work)
                th.start()
                threads.append(th)
            for th in threads:
                th.join()
            built_count = sum(results)

    # 逐处替换为等行数 \FloriaPdf{<dp>pt}{<wd>pt}{<tot>pt}{<rel_pdf>}
    out = []
    pos = 0
    replaced = 0
    for st, ed, code, _scfg, h in items_with_key:
        pdf_p, dp_p = _item_paths(book_dir, h)
        dims = _read_dims(dp_p, pdf_p) if os.path.isfile(pdf_p) else None
        if dims is None:
            continue
        dp_pt, wd_pt, tot_pt = dims
        out.append(text[pos:st])
        rel_pdf = os.path.join('.build', 'floria', 'tikz', h + '.pdf').replace('\\', '/')
        nl = code.count('\n')
        if nl <= 0:
            repl = '\\FloriaPdf{%.4fpt}{%.4fpt}{%.4fpt}{%s}' % (dp_pt, wd_pt, tot_pt, rel_pdf)
        else:
            repl = ('\\FloriaPdf{%.4fpt}{%.4fpt}{%.4fpt}%%' + '\n%%' * (nl - 1)
                    + '\n{%s}') % (dp_pt, wd_pt, tot_pt, rel_pdf)
        out.append(repl)
        pos = ed
        replaced += 1
    out.append(text[pos:])
    return ''.join(out), {'total': len(spans), 'hit': hit_count, 'built': built_count, 'replaced': replaced}
