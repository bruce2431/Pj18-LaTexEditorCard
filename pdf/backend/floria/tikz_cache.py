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
MIN_PICTURES = 4
MAX_BATCH_WORKERS = max(2, min(6, os.cpu_count() or 4))

_BEGIN_RE = re.compile(r'\\begin\s*\{tikzpicture\}')
_END_RE = re.compile(r'\\end\s*\{tikzpicture\}')
_SETCHEMFIG_RE = re.compile(r'\\setchemfig\s*\{')

_UNSAFE_TOKENS = (
    'remember picture', 'overlay', '\\ref', '\\pageref', '\\eqref',
    '\\autoref', '\\cref', '\\Cref', '\\cite', '\\label', '\\footnote',
    '\\href', '\\url', '\\thepage', '\\thechapter', '\\thesection',
    '\\thefigure', '\\thetable', '\\tikzmark', '#1', '#2',
    '\\newcommand', '\\renewcommand', '\\def\\',
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


def _extract_setchemfig_before(text, pos):
    """取 pos 之前最后一个未注释的 \\setchemfig{...}（用于保留局部键长/原子间距设置）。"""
    last = ''
    for m in _SETCHEMFIG_RE.finditer(text, 0, pos):
        if _is_commented(text, m.start()):
            continue
        grp, _ = pdf_stamp._read_group(text, m.end() - 1)
        if grp is not None:
            last = '\\setchemfig{' + grp + '}'
    return last


def find_cacheable_pictures(text):
    """扫描块文本中所有顶层可缓存的 \\begin{tikzpicture}...\\end{tikzpicture}。

    返回列表：[(start, end, code, setchemfig_cmd), ...]（按 start 升序）。
    """
    if text.count('tikzpicture') < MIN_PICTURES * 2:
        return []
    events = []
    for m in _BEGIN_RE.finditer(text):
        if not _is_commented(text, m.start()):
            events.append((m.start(), m.end(), 1))
    for m in _END_RE.finditer(text):
        if not _is_commented(text, m.start()):
            events.append((m.start(), m.end(), -1))
    events.sort(key=lambda x: x[0])

    depth = 0
    cur_start = -1
    raw_spans = []
    for st, ed, delta in events:
        if delta == 1:
            if depth == 0:
                cur_start = st
            depth += 1
        else:
            depth -= 1
            if depth == 0 and cur_start >= 0:
                raw_spans.append((cur_start, ed))
                cur_start = -1
            elif depth < 0:
                depth = 0

    if len(raw_spans) < MIN_PICTURES:
        return []

    out = []
    for st, ed in raw_spans:
        code = text[st:ed]
        # 跳过嵌套 tikzpicture（仅顶层内部不再出现 \begin{tikzpicture}）
        if _BEGIN_RE.search(code, len('\\begin{tikzpicture}')):
            continue
        if any(tok in code for tok in _UNSAFE_TOKENS):
            continue
        # 检查紧邻前文是否正在定义宏
        pre = text[max(0, st - 80):st]
        if re.search(r'\\(?:new|renew)command\b|\\def\b', pre):
            continue
        scfg = _extract_setchemfig_before(text, st) if '\\chemfig' in code or '\\schemestart' in code else ''
        out.append((st, ed, code, scfg))
    return out if len(out) >= MIN_PICTURES else []


def _cache_dir(book_dir):
    return os.path.join(book_dir, '.build', 'floria', 'tikz')


def _item_paths(book_dir, key):
    d = _cache_dir(book_dir)
    return os.path.join(d, key + '.pdf'), os.path.join(d, key + '.dp')


def _read_dp(dp_path):
    try:
        with open(dp_path, encoding='utf-8') as f:
            return float(f.read().strip())
    except (OSError, ValueError):
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
    """编译单个 TikZ 批次并把单页 PDF + 基线深度写入缓存目录。"""
    cdir = _cache_dir(book_dir)
    bdir = os.path.join(cdir, '_batch_%d_%d' % (os.getpid(), batch_id))
    os.makedirs(bdir, exist_ok=True)
    rel_bdir = os.path.relpath(bdir, book_dir).replace('\\', '/')
    tex_p = os.path.join(bdir, 'batch.tex')
    with open(tex_p, 'w', encoding='utf-8') as f:
        f.write(_batch_tex(preamble, items))

    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    e = env.copy()
    try:
        try:
            p1 = subprocess.run(
                [texcmd, '-no-pdf', '-interaction=nonstopmode', '-halt-on-error',
                 '-output-directory=' + rel_bdir, rel_bdir + '/batch.tex'],
                cwd=book_dir, timeout=120, capture_output=True, text=True,
                encoding='utf-8', errors='replace', creationflags=flags, env=e,
            )
        except FileNotFoundError:
            if not xelatex_alt:
                return 0
            p1 = subprocess.run(
                [xelatex_alt, '-no-pdf', '-interaction=nonstopmode', '-halt-on-error',
                 '-output-directory=' + rel_bdir, rel_bdir + '/batch.tex'],
                cwd=book_dir, timeout=120, capture_output=True, text=True,
                encoding='utf-8', errors='replace', creationflags=flags, env=e,
            )
        if p1.returncode != 0:
            return 0
        try:
            p2 = subprocess.run(
                ['xdvipdfmx', '-E', '-z', '1', '-o', rel_bdir + '/batch.pdf',
                 rel_bdir + '/batch.xdv'],
                cwd=book_dir, timeout=120, capture_output=True, text=True,
                encoding='utf-8', errors='replace', creationflags=flags, env=e,
            )
        except FileNotFoundError:
            if not xdvipdfmx_alt:
                return 0
            p2 = subprocess.run(
                [xdvipdfmx_alt, '-E', '-z', '1', '-o', rel_bdir + '/batch.pdf',
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
                    entries.append((ps[1], pdf_stamp._parse_pt(ps[4])))

        import pypdf
        r = pypdf.PdfReader(pdf_p)
        if len(r.pages) != len(entries):
            return 0

        saved = 0
        for idx, (key, dp_pt) in enumerate(entries):
            out_pdf, out_dp = _item_paths(book_dir, key)
            w = pypdf.PdfWriter()
            w.add_page(r.pages[idx])
            tmp_pdf = out_pdf + '.%d.tmp' % os.getpid()
            with open(tmp_pdf, 'wb') as f:
                w.write(f)
            os.replace(tmp_pdf, out_pdf)
            tmp_dp = out_dp + '.%d.tmp' % os.getpid()
            with open(tmp_dp, 'w', encoding='utf-8') as f:
                f.write('%.5f\n' % dp_pt)
            os.replace(tmp_dp, out_dp)
            saved += 1
        return saved
    except Exception:
        return 0
    finally:
        shutil.rmtree(bdir, ignore_errors=True)


def preprocess_chunk_tikz(book_dir, preamble, text, env,
                          texcmd='xelatex', xelatex_alt='', xdvipdfmx_alt=''):
    """将块内可缓存的 tikzpicture 替换为缓存 PDF（保持严格等行数）。

    返回 (new_text, stats_dict)。任何异常均安全回退为原文本。
    """
    pre_nc = pdf_stamp._strip_comments(preamble or '')
    if not re.search(r'\\usepackage(?:\[[^\]]*\])?\{[^{}]*\bgraphicx\b', pre_nc):
        return text, {'total': 0, 'hit': 0, 'built': 0}

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
        if not (os.path.isfile(pdf_p) and _read_dp(dp_p) is not None):
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

    # 逐处替换为等行数 \raisebox{-<dp>pt}{\includegraphics{...}}
    out = []
    pos = 0
    replaced = 0
    for st, ed, code, _scfg, h in items_with_key:
        pdf_p, dp_p = _item_paths(book_dir, h)
        dp = _read_dp(dp_p) if os.path.isfile(pdf_p) else None
        if dp is None:
            continue
        out.append(text[pos:st])
        rel_pdf = os.path.join('.build', 'floria', 'tikz', h + '.pdf').replace('\\', '/')
        nl = code.count('\n')
        if nl <= 0:
            repl = '\\raisebox{-%.4fpt}{\\includegraphics{%s}}' % (dp, rel_pdf)
        else:
            repl = ('\\raisebox{-%.4fpt}%%' + '\n%%' * (nl - 1)
                    + '\n{\\includegraphics{%s}}') % (dp, rel_pdf)
        out.append(repl)
        pos = ed
        replaced += 1
    out.append(text[pos:])
    return ''.join(out), {'total': len(spans), 'hit': hit_count, 'built': built_count, 'replaced': replaced}
