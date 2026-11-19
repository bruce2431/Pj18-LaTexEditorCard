# -*- coding: utf-8 -*-
"""页码解耦编译与 PDF 原位盖印 / 热更新模块。

核心思路：
  1. Kit（kit.pdf）：按导言区指纹编译一次，提取正文/粗体/上标三套原生数字字体的
     CID 编码、精确字宽、以及 plain 页脚 / headings 偶奇页眉的绝对坐标与单双面信息；
  2. 页码解耦钩子：正文块编译时不绘制可见页眉/页脚，仅在 shipout/before 记录每页的
     pagestyle（plain/headings/empty）与展开后的 leftmark/rightmark 到 wrapper.fmeta，
     文末追加 Atlas 页用原生 TeX 渲染去重后的页眉文本（包裹 /FATLAS BDC..EMC）；
     同时钩住 \\pageref / \\pageref*（包裹 /FPR）与目录页码 \\FloriaTocPg（包裹 /FTOC）；
  3. 合并阶段原位盖印：剔除 Atlas 页，将每个去重页眉封装为 Form XObject，在绝对坐标
     盖印正确页码与奇偶页眉，同时原位热更新 /FTOC 与 /FPR 数字流——下游块与目录页
     在页码偏移时零重编。
"""
import hashlib
import json
import os
import re
import subprocess
import time

KIT_VER = 1

_CM_RE = re.compile(r'1\s+0\s+0\s+1\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s+cm')
_TF_RE = re.compile(r'(/F\d+)\s+(-?\d+(?:\.\d+)?)\s+Tf')
_HEX_RE = re.compile(r'<([0-9A-Fa-f]+)>')
_FTOC_RE = re.compile(r'/FTOC\s*<<\s*(.*?)\s*>>\s*BDC(.*?)EMC', re.S)
_FPR_RE = re.compile(r'/FPR\s*<<\s*(.*?)\s*>>\s*BDC(.*?)EMC', re.S)
_TOC_LINE_RE = re.compile(r'\\contentsline\s*\{')

_SUPPORTED_CLASSES = {'ctexbook', 'book', 'ctexrep', 'report', 'ctexart', 'article'}
_UNSUPPORTED_PKGS = ('fancyhdr', 'titleps', 'scrlayer-scrpage', 'scrpage2')
_UNSUPPORTED_BODY_CMDS = (
    '\\pagenumbering', '\\frontmatter', '\\mainmatter', '\\backmatter',
    '\\fancyhead', '\\fancyfoot', '\\fancyhf',
)


def _strip_comments(text):
    out = []
    for ln in (text or '').splitlines():
        i = 0
        while True:
            p = ln.find('%', i)
            if p < 0:
                out.append(ln)
                break
            bs = 0
            j = p - 1
            while j >= 0 and ln[j] == '\\':
                bs += 1
                j -= 1
            if bs % 2 == 0:
                out.append(ln[:p])
                break
            i = p + 1
    return '\n'.join(out)


def _read_group(s, i):
    """从 s[i] == '{' 起读平衡花括号组，返回 (内文, 右括号后下标)。"""
    if i >= len(s) or s[i] != '{':
        return None, i
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


def _skip_ws(s, i):
    while i < len(s) and s[i] in ' \t\r\n':
        i += 1
    return i


def _strip_cmd_with_args(text, cmd, n_opt=0, n_req=1):
    """安全剥离导言区里的单条宏调用（如 \\usetikzlibrary{...} / \\setchemfig{...}）。"""
    pat = re.compile(r'\\' + re.escape(cmd) + r'(?![A-Za-z@])')
    out = []
    pos = 0
    for m in pat.finditer(text):
        if m.start() < pos:
            continue
        # 检查是否被注释
        ls = text.rfind('\n', 0, m.start()) + 1
        if '%' in text[ls:m.start()]:
            # 粗略检查非转义 %
            seg = text[ls:m.start()]
            if re.search(r'(?<!\\)(?:\\\\)*%', seg):
                continue
        i = m.end()
        for _ in range(n_opt):
            i = _skip_ws(text, i)
            if i < len(text) and text[i] == '[':
                end = text.find(']', i + 1)
                if end < 0:
                    break
                i = end + 1
        ok = True
        for _ in range(n_req):
            i = _skip_ws(text, i)
            val, nxt = _read_group(text, i)
            if val is None:
                ok = False
                break
            i = nxt
        if ok:
            out.append(text[pos:m.start()])
            pos = i
    out.append(text[pos:])
    return ''.join(out)


def _remove_pkg_from_preamble(preamble, pkg_name):
    """从导言区移除 \\usepackage[...]{pkg}（支持逗号列表里的单项剥离）。"""
    pat = re.compile(r'\\usepackage\s*(\[[^\]]*\])?\s*\{([^{}]*)\}')

    def repl(m):
        ls = preamble.rfind('\n', 0, m.start()) + 1
        if re.search(r'(?<!\\)(?:\\\\)*%', preamble[ls:m.start()]):
            return m.group(0)
        opt = m.group(1) or ''
        pkgs = [p.strip() for p in m.group(2).split(',') if p.strip()]
        kept = [p for p in pkgs if p != pkg_name]
        if len(kept) == len(pkgs):
            return m.group(0)
        if not kept:
            return ''
        return '\\usepackage%s{%s}' % (opt, ','.join(kept))

    return pat.sub(repl, preamble)


def strip_preamble(preamble, used_text='', for_kit=False):
    """按块实际使用情况安全精简重型宏包（tikz/tikz-cd/chemfig/mhchem/pgfplots）。

    只剥离明确未使用的可选重包；若精简版编译出错，调用方自动回退完整导言区。
    """
    pre = preamble or ''
    pre_nc = _strip_comments(pre)
    txt = '' if for_kit else _strip_comments(used_text or '')

    # 若导言区自己加载了强依赖 tikz 的宏包（如 tcolorbox/mdframed/forest），不剥 tikz
    has_tikz_dep_pkg = bool(re.search(
        r'\\usepackage(?:\[[^\]]*\])?\{[^{}]*\b(?:tcolorbox|mdframed|forest|circuitikz|smartdiagram)\b',
        pre_nc))

    need_chemfig = (not for_kit) and bool(re.search(
        r'\\(?:chemfig|schemestart|chemname|lewis)\b', txt))
    need_mhchem = (not for_kit) and bool(re.search(r'\\ce\b|\\pu\b', txt))
    need_tikzcd = (not for_kit) and ('tikzcd' in txt)
    need_pgfplots = (not for_kit) and bool(re.search(r'\\begin\{axis\}|\\pgfplot|\\addplot\b', txt))
    need_tikz = (
        need_chemfig or need_tikzcd or need_pgfplots or has_tikz_dep_pkg
        or ((not for_kit) and bool(re.search(
            r'tikzpicture|\\tikz\b|\\draw\b|\\node\b|\\pgf', txt)))
    )
    need_longtable = (not for_kit) and ('longtable' in txt)
    need_tabularx = (not for_kit) and ('tabularx' in txt)
    need_subcaption = (not for_kit) and bool(re.search(r'subfigure|subtable|\\subcaption\b', txt))
    need_booktabs = (not for_kit) and bool(re.search(r'\\(?:toprule|midrule|bottomrule|cmidrule|addlinespace)\b', txt))
    need_float = (not for_kit) and bool(re.search(r'\[H\]|\\(?:floatstyle|newfloat|restylefloat|floatplacement)\b', txt))

    # 若导言区自定义命令/环境里用到了 tikz/chemfig/ce，也保守保留
    if not for_kit:
        if not need_chemfig and re.search(r'\\(?:new|renew)command[^%\n]*\\chemfig\b', pre_nc):
            need_chemfig = True
            need_tikz = True
        if not need_mhchem and re.search(r'\\(?:new|renew)command[^%\n]*\\ce\b', pre_nc):
            need_mhchem = True
        if not need_tikz and re.search(r'\\(?:new|renew)(?:command|environment)[^%\n]*(?:tikzpicture|\\tikz\b)', pre_nc):
            need_tikz = True

    out = pre
    extra_defs = []
    if not need_chemfig:
        out = _remove_pkg_from_preamble(out, 'chemfig')
        out = _strip_cmd_with_args(out, 'setchemfig', 0, 1)
        out = _strip_cmd_with_args(out, 'definesubmol', 0, 2)
        if not for_kit and ('\\setchemfig' in txt or '\\definesubmol' in txt):
            extra_defs.append('\\providecommand{\\setchemfig}[1]{}\\providecommand{\\definesubmol}[2]{}')
    if not need_mhchem:
        out = _remove_pkg_from_preamble(out, 'mhchem')
        out = _remove_pkg_from_preamble(out, 'chemformula')
    if not need_tikzcd:
        out = _remove_pkg_from_preamble(out, 'tikz-cd')
    if not need_pgfplots:
        out = _remove_pkg_from_preamble(out, 'pgfplots')
        out = _strip_cmd_with_args(out, 'pgfplotsset', 0, 1)
    if not need_tikz:
        out = _remove_pkg_from_preamble(out, 'tikz')
        out = _strip_cmd_with_args(out, 'usetikzlibrary', 0, 1)
        out = _strip_cmd_with_args(out, 'tikzset', 0, 1)
    if not need_longtable:
        out = _remove_pkg_from_preamble(out, 'longtable')
    if not need_tabularx:
        out = _remove_pkg_from_preamble(out, 'tabularx')
    if not need_subcaption:
        out = _remove_pkg_from_preamble(out, 'subcaption')
    if not need_booktabs:
        out = _remove_pkg_from_preamble(out, 'booktabs')
    if not need_float:
        out = _remove_pkg_from_preamble(out, 'float')
    if for_kit:
        out = _remove_pkg_from_preamble(out, 'hyperref')
    if extra_defs:
        out = out + '\n' + '\n'.join(extra_defs)
    return out


def can_decouple(preamble, front, chunks):
    """快速预判文档是否适用页码解耦模式（最终还由 kit.kmeta 校验对称页边距）。"""
    pre_nc = _strip_comments(preamble or '')
    m = re.search(r'\\documentclass(?:\s*\[[^\]]*\])?\s*\{([^{}]+)\}', pre_nc)
    if not m or m.group(1).strip() not in _SUPPORTED_CLASSES:
        return False
    for pkg in _UNSUPPORTED_PKGS:
        if re.search(r'\\usepackage(?:\[[^\]]*\])?\{[^{}]*\b' + re.escape(pkg) + r'\b', pre_nc):
            return False
    if '\\ps@' in pre_nc:
        return False
    all_body = (front or '') + '\n' + '\n'.join(c.get('text', '') for c in (chunks or []))
    body_nc = _strip_comments(all_body)
    for cmd in _UNSUPPORTED_BODY_CMDS:
        if re.search(re.escape(cmd) + r'(?![A-Za-z@])', body_nc):
            return False
    return True


def decoupled_hook_code():
    """注入到 wrapper 导言区末尾的页码解耦 + 标记流钩子代码。"""
    return r"""
\makeatletter
\providecommand{\texorpdfstring}[2]{#1}
\newcommand{\FloriaTocPg}[2]{%
  \texorpdfstring{\special{pdf:literal /FTOC << /I #1 >> BDC}#2\special{pdf:literal EMC}}{#2}%
}
\AddToHook{begindocument/end}{%
  \ifcsname pageref\endcsname
    \NewCommandCopy{\floria@orig@pageref}{\pageref}%
    \RenewDocumentCommand{\pageref}{sm}{%
      \texorpdfstring{%
        \special{pdf:literal /FPR << /L (#2) >> BDC}%
        \IfBooleanTF{#1}{\floria@orig@pageref*{#2}}{\floria@orig@pageref{#2}}%
        \special{pdf:literal EMC}%
      }{\IfBooleanTF{#1}{\floria@orig@pageref*{#2}}{\floria@orig@pageref{#2}}}%
    }%
  \fi
}
\newwrite\floria@meta
\immediate\openout\floria@meta=\jobname.fmeta
\gdef\floria@ps{headings}
\gdef\floria@atlas@list{}
\newcount\floria@mid
\floria@mid=0
\newcount\floria@bpages
\floria@bpages=0
\def\floria@regmark#1#2#3{%
  \ifx#2\@empty
    \def#3{-1}%
  \else
    \edef\floria@key{floria@mk@#1@\expandafter\detokenize\expandafter{#2}}%
    \@ifundefined{\floria@key}{%
      \global\advance\floria@mid by 1
      \expandafter\xdef\csname\floria@key\endcsname{\the\floria@mid}%
      \xdef\floria@atlas@list{\unexpanded\expandafter{\floria@atlas@list}\noexpand\floria@emitmark{\the\floria@mid}{#1}{\unexpanded\expandafter{#2}}}%
    }{}%
    \edef#3{\csname\floria@key\endcsname}%
  \fi
}
\def\floria@emitmark#1#2#3{%
  \noindent\special{pdf:literal /FATLAS << /Id #1 /Side (#2) >> BDC}{\slshape #3}\special{pdf:literal EMC}\par
}
\let\floria@orig@ps@headings\ps@headings
\def\ps@headings{%
  \floria@orig@ps@headings
  \let\@oddfoot\@empty\let\@evenfoot\@empty
  \def\@evenhead{\gdef\floria@ps{headings}\hfil}%
  \def\@oddhead{\gdef\floria@ps{headings}\hfil}%
}
\let\floria@orig@ps@plain\ps@plain
\def\ps@plain{%
  \floria@orig@ps@plain
  \let\@oddhead\@empty\let\@evenhead\@empty
  \def\@oddfoot{\gdef\floria@ps{plain}\hfil}%
  \let\@evenfoot\@oddfoot
}
\let\floria@orig@ps@empty\ps@empty
\def\ps@empty{%
  \floria@orig@ps@empty
  \def\@oddhead{\gdef\floria@ps{empty}\hfil}\let\@evenhead\@oddhead
  \let\@oddfoot\@empty\let\@evenfoot\@empty
}
\AddToHook{begindocument/before}{%
  \ifx\@oddhead\@empty
    \ifx\@oddfoot\@empty\pagestyle{empty}\else\pagestyle{plain}\fi
  \else
    \pagestyle{headings}%
  \fi
}
\AddToHook{shipout/before}{%
  \ifx\floria@in@atlas\@undefined
    \global\advance\floria@bpages by 1
    \protected@edef\floria@lm{\leftmark}%
    \protected@edef\floria@rm{\rightmark}%
    \floria@regmark{L}{\floria@lm}{\floria@lid}%
    \floria@regmark{R}{\floria@rm}{\floria@rid}%
    \immediate\write\floria@meta{P \the\floria@bpages\space \floria@ps\space \floria@lid\space \floria@rid}%
  \fi
}
\AtEndDocument{%
  \clearpage
  \gdef\floria@in@atlas{1}%
  \immediate\write\floria@meta{BODY_PAGES \the\floria@bpages}%
  \ifx\floria@atlas@list\@empty
    \immediate\write\floria@meta{HAS_ATLAS 0}%
  \else
    \immediate\write\floria@meta{HAS_ATLAS 1}%
    \pagestyle{empty}%
    \normalfont
    \floria@atlas@list
    \clearpage
  \fi
  \immediate\closeout\floria@meta
}
\makeatother
"""


def parse_fmeta(path):
    """解析块编译产出的 wrapper.fmeta → {pages: [(ps, lid, rid), ...], body_pages, has_atlas, ftoc_pages, fpr_pages}。"""
    pages = []
    body_pages = None
    has_atlas = 0
    ftoc_pages = None
    fpr_pages = None
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            for ln in f:
                s = ln.strip()
                if not s:
                    continue
                if s.startswith('PATCH'):
                    rest = s[5:].strip()
                    if '|' in rest:
                        p_toc, p_pr = rest.split('|', 1)
                        ftoc_pages = {int(x) for x in p_toc.split(',') if x.strip().isdigit()}
                        fpr_pages = {int(x) for x in p_pr.split(',') if x.strip().isdigit()}
                    continue
                parts = s.split()
                if parts[0] == 'P' and len(parts) >= 5:
                    try:
                        pages.append((parts[2], int(parts[3]), int(parts[4])))
                    except ValueError:
                        pages.append((parts[2], -1, -1))
                elif parts[0] == 'BODY_PAGES' and len(parts) >= 2:
                    try:
                        body_pages = int(parts[1])
                    except ValueError:
                        pass
                elif parts[0] == 'HAS_ATLAS' and len(parts) >= 2:
                    try:
                        has_atlas = int(parts[1])
                    except ValueError:
                        pass
    except OSError:
        return None
    if body_pages is None:
        body_pages = len(pages)
    if len(pages) != body_pages:
        return None
    return {
        'pages': pages,
        'body_pages': body_pages,
        'has_atlas': bool(has_atlas),
        'ftoc_pages': ftoc_pages,
        'fpr_pages': fpr_pages,
    }


def annotate_fmeta_patch_pages(pdf_path, fmeta_path, body_pages):
    """块编译后一次性扫描哪几页含 /FTOC 或 /FPR，并追加 PATCH 行到 wrapper.fmeta。

    合并阶段对不含任何热更新标记的页面（占全书 90%+）跳过内容流解压，
    直接以 ArrayObject([原压缩流, 80B 盖印流]) 零拷贝拼接，提速 ~3x。
    """
    try:
        import pypdf
        r = pypdf.PdfReader(pdf_path)
        r_pages = list(r.pages)
        end = min(body_pages, len(r_pages))
        ftoc_idx, fpr_idx = [], []
        for i in range(end):
            cs = r_pages[i].get_contents()
            if cs is None:
                continue
            data = cs.get_data()
            if b'/FTOC' in data:
                ftoc_idx.append(str(i))
            if b'/FPR' in data:
                fpr_idx.append(str(i))
        with open(fmeta_path, 'a', encoding='utf-8') as f:
            f.write('PATCH %s|%s\n' % (','.join(ftoc_idx), ','.join(fpr_idx)))
    except Exception:                       # noqa: BLE001
        pass


def _extract_bdc_boxes(stream_str, tag):
    """提取流里所有 /<tag> << ... >> BDC ... EMC 块及其精确坐标。"""
    pat = re.compile(r'/' + tag + r'\s*<<\s*(.*?)\s*>>\s*BDC(.*?)EMC', re.S)
    init_m = _CM_RE.search(stream_str[:250])
    ox, oy = (float(init_m.group(1)), float(init_m.group(2))) if init_m else (0.0, 0.0)
    results = []
    for m in pat.finditer(stream_str):
        props = m.group(1)
        body = m.group(2)
        cms = list(_CM_RE.finditer(body))
        if len(cms) >= 2:
            x0 = -float(cms[0].group(1))
            y0 = -float(cms[0].group(2))
            x1 = float(cms[-1].group(1))
            y1 = float(cms[-1].group(2))
            inner = body[cms[0].end():cms[-1].start()].strip()
        else:
            x0 = y0 = x1 = y1 = 0.0
            inner = body.strip()
        results.append({
            'props': props,
            'ox': ox, 'oy': oy,
            'x0': x0, 'y0': y0, 'x1': x1, 'y1': y1,
            'abs_x0': ox + x0, 'abs_y0': oy + y0,
            'abs_x1': ox + x1, 'abs_y1': oy + y1,
            'width': x1 - x0,
            'inner': inner,
        })
    return ox, oy, results


def _parse_pt(val):
    m = re.search(r'-?\d+(?:\.\d+)?', val or '')
    return float(m.group(0)) if m else 0.0


def _kit_tex_source(preamble):
    return (
        preamble + '\n'
        + r"""\makeatletter
\newwrite\floria@kmeta
\immediate\openout\floria@kmeta=\jobname.kmeta
\def\FloriaKit#1#2{\special{pdf:literal /FKIT << /K (#1) >> BDC}{#2}\special{pdf:literal EMC}}
\def\ps@fkitplain{%
  \let\@mkboth\@gobbletwo
  \let\@oddhead\@empty\let\@evenhead\@empty
  \def\@oddfoot{\reset@font\hfil\FloriaKit{PFOOT}{\thepage}\hfil}%
  \let\@evenfoot\@oddfoot
}
\def\ps@fkithead{%
  \let\@oddfoot\@empty\let\@evenfoot\@empty
  \def\@evenhead{\reset@font\FloriaKit{EPG}{\thepage}\hfil{\slshape\FloriaKit{EMK}{\leftmark}}}%
  \def\@oddhead{\reset@font{\slshape\FloriaKit{OMK}{\rightmark}}\hfil\FloriaKit{OPG}{\thepage}}%
}
\AddToHook{begindocument/end}{%
  \immediate\write\floria@kmeta{TWOSIDE \if@twoside 1\else 0\fi}%
  \immediate\write\floria@kmeta{ODDSIDE \the\oddsidemargin}%
  \immediate\write\floria@kmeta{EVENSIDE \the\evensidemargin}%
}
\AtEndDocument{\immediate\closeout\floria@kmeta}
\makeatother
\begin{document}
\thispagestyle{fkitplain}
\setcounter{page}{8}
\noindent\normalfont\FloriaKit{DIGS}{0123456789}\par
\noindent\normalfont\FloriaKit{BDIGS}{\bfseries 0123456789}\par
\noindent\normalfont\FloriaKit{SDIGS}{\scriptsize 0123456789}\par
\newpage
\pagestyle{fkithead}
\markboth{LEFTMARK}{RIGHTMARK}
\setcounter{page}{6}
Even page
\newpage
\setcounter{page}{7}
Odd page
\end{document}
"""
    )


def ensure_kit(book_dir, preamble, env, texcmd='xelatex', xelatex_alt='', xdvipdfmx_alt=''):
    """确保 kit.pdf 与解析结果 kit_info.json 就绪（按导言区指纹缓存）。

    返回 dict（含 pdf 路径、字体映射、页眉页脚坐标、是否双面）或 None（不对称页边距/失败）。
    """
    kit_pre = strip_preamble(preamble, for_kit=True)
    kit_hash = hashlib.md5((str(KIT_VER) + '\0' + kit_pre).encode('utf-8', 'replace')).hexdigest()
    kit_dir = os.path.join(book_dir, '.build', 'floria', 'kit')
    kit_pdf = os.path.join(kit_dir, 'kit.pdf')
    kit_json = os.path.join(kit_dir, 'kit_info.json')

    # 缓存命中
    if os.path.isfile(kit_pdf) and os.path.isfile(kit_json):
        try:
            with open(kit_json, encoding='utf-8') as f:
                info = json.load(f)
            if info.get('hash') == kit_hash and info.get('ver') == KIT_VER:
                if not info.get('symmetric', False):
                    return None
                info['pdf'] = kit_pdf
                return info
        except (OSError, ValueError):
            pass

    os.makedirs(kit_dir, exist_ok=True)
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    e = env.copy()

    def _compile_kit(pre_text):
        tex_p = os.path.join(kit_dir, 'kit.tex')
        with open(tex_p, 'w', encoding='utf-8') as f:
            f.write(_kit_tex_source(pre_text))
        rel_dir = os.path.join('.build', 'floria', 'kit').replace('\\', '/')
        rel_tex = rel_dir + '/kit.tex'
        rel_xdv = rel_dir + '/kit.xdv'
        rel_pdf = rel_dir + '/kit.pdf'
        try:
            p1 = subprocess.run(
                [texcmd, '-no-pdf', '-interaction=nonstopmode', '-halt-on-error',
                 '-output-directory=' + rel_dir, rel_tex],
                cwd=book_dir, timeout=60, capture_output=True, text=True,
                encoding='utf-8', errors='replace', creationflags=flags, env=e,
            )
        except FileNotFoundError:
            if not xelatex_alt:
                return False
            p1 = subprocess.run(
                [xelatex_alt, '-no-pdf', '-interaction=nonstopmode', '-halt-on-error',
                 '-output-directory=' + rel_dir, rel_tex],
                cwd=book_dir, timeout=60, capture_output=True, text=True,
                encoding='utf-8', errors='replace', creationflags=flags, env=e,
            )
        if p1.returncode != 0:
            return False
        try:
            p2 = subprocess.run(
                ['xdvipdfmx', '-E', '-z', '1', '-o', rel_pdf, rel_xdv],
                cwd=book_dir, timeout=60, capture_output=True, text=True,
                encoding='utf-8', errors='replace', creationflags=flags, env=e,
            )
        except FileNotFoundError:
            if not xdvipdfmx_alt:
                return False
            p2 = subprocess.run(
                [xdvipdfmx_alt, '-E', '-z', '1', '-o', rel_pdf, rel_xdv],
                cwd=book_dir, timeout=60, capture_output=True, text=True,
                encoding='utf-8', errors='replace', creationflags=flags, env=e,
            )
        return p2.returncode == 0 and os.path.isfile(kit_pdf)

    ok = _compile_kit(kit_pre)
    if not ok and kit_pre != preamble:
        # 精简导言区偶发依赖缺失 → 回退完整导言区（仅剥 hyperref）
        ok = _compile_kit(_remove_pkg_from_preamble(preamble, 'hyperref'))
    if not ok:
        return None

    # 解析 kit.kmeta
    twoside = True
    oddside = 0.0
    evenside = 0.0
    try:
        with open(os.path.join(kit_dir, 'kit.kmeta'), encoding='utf-8') as f:
            for ln in f:
                parts = ln.strip().split()
                if len(parts) < 2:
                    continue
                if parts[0] == 'TWOSIDE':
                    twoside = parts[1] == '1'
                elif parts[0] == 'ODDSIDE':
                    oddside = _parse_pt(parts[1])
                elif parts[0] == 'EVENSIDE':
                    evenside = _parse_pt(parts[1])
    except OSError:
        return None

    symmetric = (not twoside) or (abs(oddside - evenside) <= 0.5)
    if not symmetric:
        info = {'ver': KIT_VER, 'hash': kit_hash, 'symmetric': False}
        try:
            with open(kit_json, 'w', encoding='utf-8') as f:
                json.dump(info, f)
        except OSError:
            pass
        return None

    # 解析 kit.pdf
    import pypdf
    kr = pypdf.PdfReader(kit_pdf)
    items = {}
    for pi, pg in enumerate(kr.pages):
        cs = pg.get_contents()
        if cs is None:
            continue
        s = cs.get_data().decode('latin1', errors='replace')
        _, _, boxes = _extract_bdc_boxes(s, 'FKIT')
        for b in boxes:
            km = re.search(r'/K\s*\(([^)]+)\)', b['props'])
            if not km:
                continue
            k = km.group(1)
            tf = _TF_RE.search(b['inner'])
            hexes = ''.join(_HEX_RE.findall(b['inner']))
            items[k] = {
                'page_idx': pi,
                'font_key': tf.group(1) if tf else None,
                'font_size': float(tf.group(2)) if tf else 10.5,
                'hex': hexes,
                'abs_x0': b['abs_x0'], 'abs_y0': b['abs_y0'],
                'abs_x1': b['abs_x1'], 'abs_y1': b['abs_y1'],
                'width': b['width'],
            }

    # 校验三套数字字体（各 10 位 0-9）与页脚/页眉坐标
    fonts = {}
    for dk in ('DIGS', 'BDIGS', 'SDIGS'):
        it = items.get(dk)
        if not it or not it['font_key'] or len(it['hex']) not in (20, 40):
            return None
        step = len(it['hex']) // 10
        cids = [it['hex'][i:i + step] for i in range(0, len(it['hex']), step)]
        fonts[dk] = {
            'page_idx': it['page_idx'],
            'font_key': it['font_key'],
            'base_size': it['font_size'],
            'digit_w': it['width'] / 10.0,
            'cids': cids,
        }

    pfoot = items.get('PFOOT')
    opg = items.get('OPG')
    omk = items.get('OMK')
    if not pfoot or not opg or not omk:
        return None
    epg = items.get('EPG') or opg
    emk = items.get('EMK') or omk

    mb = kr.pages[0].mediabox
    info = {
        'ver': KIT_VER,
        'hash': kit_hash,
        'symmetric': True,
        'twoside': twoside,
        'page_size': [float(mb.width), float(mb.height)],
        'fonts': fonts,
        'plain_cx': (pfoot['abs_x0'] + pfoot['abs_x1']) / 2.0,
        'plain_y': pfoot['abs_y0'],
        'even_pg_x': epg['abs_x0'],
        'even_mk_rx': emk['abs_x1'],
        'odd_mk_lx': omk['abs_x0'],
        'odd_pg_rx': opg['abs_x1'],
        'head_y': opg['abs_y0'],
    }
    tmp = kit_json + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(info, f, ensure_ascii=False, indent=1)
        os.replace(tmp, kit_json)
    except OSError:
        pass
    info['pdf'] = kit_pdf
    return info


# ---------- TOC 与 aux 页码平移工具 ----------
def shift_toc_pages(toc_text, delta):
    """把块内 wrapper.toc 中所有 \\contentsline 的页码加上 delta。"""
    if not toc_text or delta == 0:
        return toc_text or ''
    out = []
    pos = 0
    for m in _TOC_LINE_RE.finditer(toc_text):
        i = m.end() - 1
        g1, i1 = _read_group(toc_text, i)
        g2, i2 = _read_group(toc_text, i1)
        g3, i3 = _read_group(toc_text, i2)
        if g1 is None or g2 is None or g3 is None:
            continue
        try:
            new_pg = str(int(g3.strip()) + delta)
        except ValueError:
            continue
        out.append(toc_text[pos:i2])
        out.append('{' + new_pg + '}')
        pos = i3
    out.append(toc_text[pos:])
    return ''.join(out)


def prepare_toc_for_front(master_toc):
    """从整本 master_toc 提取：
      - struct_text：页码置为 * 的结构文本（判断 front 是否需要重编）；
      - wrapped_text：页码包上 \\FloriaTocPg{k}{page} 的 seed 文本（编 front 时用）；
      - page_list：按顺序对应的整数页码列表 [pg0, pg1, ...]（热更新 /FTOC 用）。
    """
    struct_out = []
    wrap_out = []
    pages = []
    pos = 0
    idx = 0
    for m in _TOC_LINE_RE.finditer(master_toc or ''):
        i = m.end() - 1
        g1, i1 = _read_group(master_toc, i)
        g2, i2 = _read_group(master_toc, i1)
        g3, i3 = _read_group(master_toc, i2)
        if g1 is None or g2 is None or g3 is None:
            continue
        pg_s = g3.strip()
        struct_out.append(master_toc[pos:i2] + '{*}')
        wrap_out.append(master_toc[pos:i2] + '{\\FloriaTocPg{%d}{%s}}' % (idx, pg_s))
        pages.append(pg_s)
        idx += 1
        pos = i3
    struct_out.append((master_toc or '')[pos:])
    wrap_out.append((master_toc or '')[pos:])
    return ''.join(struct_out), ''.join(wrap_out), pages


def shift_newlabel_line(line, delta):
    """解析 \\newlabel{name}{{num}{page}...}，返回 (name, shifted_line, abs_page_str|None)。"""
    if not line.startswith('\\newlabel{'):
        return None, line, None
    name, i1 = _read_group(line, len('\\newlabel'))
    if name is None:
        return None, line, None
    body, i2 = _read_group(line, i1)
    if body is None:
        return name, line, None
    # body 内部为 {num}{page}{title}{anchor}{...}
    num, j1 = _read_group(body, 0)
    if num is None:
        return name, line, None
    pg, j2 = _read_group(body, j1)
    if pg is None:
        return name, line, None
    pg_s = pg.strip()
    try:
        abs_pg = str(int(pg_s) + delta)
    except ValueError:
        return name, line, pg_s
    if delta == 0:
        return name, line, abs_pg
    new_body = body[:j1] + '{' + abs_pg + '}' + body[j2:]
    new_line = line[:i1] + '{' + new_body + '}' + line[i2:]
    return name, new_line, abs_pg


# ---------- 核心：PDF 合并 + 原位盖印 + /FTOC & /FPR 热更新 ----------
def _encode_digits(s, fspec):
    cids = fspec['cids']
    parts = []
    for ch in str(s):
        if '0' <= ch <= '9':
            parts.append(cids[ord(ch) - 48])
    return '<' + ''.join(parts) + '>'


def stamp_and_merge_pdfs(parts, out, kit_info, outline=None,
                         toc_pages=None, label_pages=None):
    """合并前置页与各块 PDF，同时完成：
      1. 剔除每块末尾 Atlas 页并提取去重页眉为共享 Form XObject；
      2. 在每页按绝对页码与 fmeta 盖印页脚或奇偶页眉；
      3. 原位热更新 /FTOC（目录页码）与 /FPR（\\pageref 跨页引用）；
      4. 重建书签树、命名目标、并将 xr-hyper 的 GoToR 改回内部 GoTo。

    parts 元素格式：
      - ('blanks', n, (w, h))
      - ('pages', path, start_idx, end_idx, abs_start_page, fmeta_dict_or_None)
    """
    import pypdf
    from pypdf.generic import (
        ArrayObject, DecodedStreamObject, DictionaryObject, FloatObject,
        NameObject, TextStringObject,
    )

    w = pypdf.PdfWriter()

    # 1. 从 kit.pdf 克隆三套数字字体对象（整本书共享，几乎零体积增量）
    kr = pypdf.PdfReader(kit_info['pdf'])
    kit_fonts = {}
    for dk, fname in (('DIGS', '/FKD0'), ('BDIGS', '/FKDB'), ('SDIGS', '/FKDS')):
        fi = kit_info['fonts'][dk]
        fobj = kr.pages[fi['page_idx']]['/Resources']['/Font'][fi['font_key']].get_object()
        fref = w._add_object(fobj.clone(w))
        kit_fonts[dk] = {
            'name': fname,
            'ref': fref,
            'cids': fi['cids'],
            'base_size': fi['base_size'],
            'digit_w': fi['digit_w'],
        }

    twoside = kit_info['twoside']
    plain_cx = kit_info['plain_cx']
    plain_y = kit_info['plain_y']
    even_pg_x = kit_info['even_pg_x']
    even_mk_rx = kit_info['even_mk_rx']
    odd_mk_lx = kit_info['odd_mk_lx']
    odd_pg_rx = kit_info['odd_pg_rx']
    head_y = kit_info['head_y']
    dig_spec = kit_fonts['DIGS']

    dests = {}
    toc_map = {i: str(p) for i, p in enumerate(toc_pages or [])}
    lbl_map = {str(k): str(v) for k, v in (label_pages or {}).items()}

    def _patch_ftoc(m, fonts_dict):
        props, body = m.group(1), m.group(2)
        im = re.search(r'/I\s+(\d+)', props)
        if not im:
            return m.group(0)
        idx = int(im.group(1))
        new_s = toc_map.get(idx)
        if not new_s or not new_s.isdigit():
            return m.group(0)
        cms = list(_CM_RE.finditer(body))
        tf = _TF_RE.search(body)
        if len(cms) < 2 or not tf:
            return m.group(0)
        x0 = -float(cms[0].group(1))
        y0 = -float(cms[0].group(2))
        x1 = float(cms[-1].group(1))
        y1 = float(cms[-1].group(2))
        sz = float(tf.group(2))
        fk = tf.group(1)
        fbase = ''
        try:
            fbase = str(fonts_dict[fk].get_object().get('/BaseFont', ''))
        except Exception:
            pass
        fspec = kit_fonts['BDIGS'] if ('BX' in fbase or 'Bold' in fbase) else kit_fonts['DIGS']
        dw = fspec['digit_w'] * (sz / fspec['base_size'])
        nx0 = x1 - len(new_s) * dw
        hx = _encode_digits(new_s, fspec)
        return (
            '/FTOC << %s >> BDC '
            '1 0 0 1 %.3f %.3f cm '
            'BT %s %.4g Tf %.3f %.3f Td[%s]TJ ET '
            '1 0 0 1 %.3f %.3f cm EMC'
            % (props, -x0, -y0, fspec['name'], sz, nx0, y0, hx, x1, y1)
        )

    def _patch_fpr(m, fonts_dict):
        props, body = m.group(1), m.group(2)
        lm = re.search(r'/L\s*\(([^)]+)\)', props)
        if not lm:
            return m.group(0)
        lbl = lm.group(1)
        new_s = lbl_map.get(lbl)
        if not new_s:
            try:
                lbl_u = lbl.encode('latin1').decode('utf-8')
                new_s = lbl_map.get(lbl_u)
            except (UnicodeEncodeError, UnicodeDecodeError):
                pass
        if not new_s or not new_s.isdigit():
            return m.group(0)
        cms = list(_CM_RE.finditer(body))
        tf = _TF_RE.search(body)
        if len(cms) < 2 or not tf:
            return m.group(0)
        x0 = -float(cms[0].group(1))
        y0 = -float(cms[0].group(2))
        x1 = float(cms[-1].group(1))
        y1 = float(cms[-1].group(2))
        sz = float(tf.group(2))
        if sz < dig_spec['base_size'] * 0.85:
            fspec = kit_fonts['SDIGS']
        else:
            fbase = ''
            try:
                fbase = str(fonts_dict[tf.group(1)].get_object().get('/BaseFont', ''))
            except Exception:
                pass
            fspec = kit_fonts['BDIGS'] if ('BX' in fbase or 'Bold' in fbase) else kit_fonts['DIGS']
        dw = fspec['digit_w'] * (sz / fspec['base_size'])
        w_box = max(0.5, x1 - x0)
        w_new = len(new_s) * dw
        tz_cmd = ''
        if w_new <= w_box + 0.08:
            nx0 = x0 + (w_box - w_new) / 2.0
        else:
            tz = max(50.0, 100.0 * w_box / w_new)
            tz_cmd = '%.2f Tz ' % tz
            nx0 = x0
        hx = _encode_digits(new_s, fspec)
        return (
            '/FPR << %s >> BDC '
            '1 0 0 1 %.3f %.3f cm '
            'BT %s %.4g Tf %s%.3f %.3f Td[%s]TJ ET '
            '1 0 0 1 %.3f %.3f cm EMC'
            % (props, -x0, -y0, fspec['name'], sz, tz_cmd, nx0, y0, hx, x1, y1)
        )

    total_pages = 0
    gotor_acts = []
    fkd0_name = NameObject('/FKD0')
    fkdb_name = NameObject('/FKDB')
    fkds_name = NameObject('/FKDS')
    contents_name = NameObject('/Contents')
    res_name = NameObject('/Resources')
    font_name = NameObject('/Font')
    xobj_name = NameObject('/XObject')
    dig_ref = dig_spec['ref']
    dig_size = dig_spec['base_size']
    dig_w = dig_spec['digit_w']

    def _build_stamp_ops(ps, abs_pg, lid, rid, res, atlas_forms):
        pg_str = str(abs_pg)
        pg_w = len(pg_str) * dig_w
        pg_hx = _encode_digits(pg_str, dig_spec)
        ops = []
        if ps == 'plain':
            px = plain_cx - pg_w / 2.0
            ops.append(
                'q 0 G 0 g BT /FKD0 %.4g Tf %.3f %.3f Td[%s]TJ ET Q'
                % (dig_size, px, plain_y, pg_hx)
            )
        elif ps == 'headings':
            if twoside and (abs_pg % 2 == 0):
                ops.append(
                    'q 0 G 0 g BT /FKD0 %.4g Tf %.3f %.3f Td[%s]TJ ET Q'
                    % (dig_size, even_pg_x, head_y, pg_hx)
                )
                af = atlas_forms.get(lid)
                if af:
                    if '/XObject' not in res:
                        res[xobj_name] = DictionaryObject()
                    res['/XObject'].get_object()[NameObject(af['name'])] = af['ref']
                    mx = even_mk_rx - af['width']
                    ops.append('q 0 G 0 g 1 0 0 1 %.3f %.3f cm %s Do Q'
                               % (mx, head_y, af['name']))
            else:
                af = atlas_forms.get(rid)
                if af:
                    if '/XObject' not in res:
                        res[xobj_name] = DictionaryObject()
                    res['/XObject'].get_object()[NameObject(af['name'])] = af['ref']
                    ops.append('q 0 G 0 g 1 0 0 1 %.3f %.3f cm %s Do Q'
                               % (odd_mk_lx, head_y, af['name']))
                px = odd_pg_rx - pg_w
                ops.append(
                    'q 0 G 0 g BT /FKD0 %.4g Tf %.3f %.3f Td[%s]TJ ET Q'
                    % (dig_size, px, head_y, pg_hx)
                )
        return ops

    def _append_stamp_stream(pg, stamp_ops):
        st = DecodedStreamObject()
        st.set_data(('\n'.join(stamp_ops) + '\n').encode('latin1'))
        sref = w._add_object(st)
        orig_c = pg.get('/Contents')
        if orig_c is None:
            pg[contents_name] = sref
        elif isinstance(orig_c, ArrayObject):
            pg[contents_name] = ArrayObject(list(orig_c) + [sref])
        else:
            orig_obj = orig_c.get_object()
            if isinstance(orig_obj, ArrayObject):
                pg[contents_name] = ArrayObject(list(orig_obj) + [sref])
            else:
                pg[contents_name] = ArrayObject([orig_c, sref])

    for part in parts:
        if part[0] == 'blanks':
            n, (bw, bh) = part[1], part[2]
            for _ in range(n):
                w.add_blank_page(width=bw, height=bh)
                total_pages += 1
            continue

        _, path, a, b, abs_start, fmeta = part
        off = total_pages
        r = pypdf.PdfReader(path)
        r_pages = list(r.pages)
        n_total = len(r_pages)
        body_end = min(b, n_total)

        # 提取本块 Atlas 页（位于 body_end .. n_total-1）中的 /FATLAS 块为 Form XObject
        atlas_forms = {}
        if fmeta and fmeta.get('has_atlas') and body_end < n_total:
            for api in range(body_end, n_total):
                apg = r_pages[api]
                acs = apg.get_contents()
                if acs is None:
                    continue
                astr = acs.get_data().decode('latin1', errors='replace')
                if '/FATLAS' not in astr:
                    continue
                _, _, aboxes = _extract_bdc_boxes(astr, 'FATLAS')
                if not aboxes:
                    continue
                ares = apg.get('/Resources')
                ares_cloned = ares.clone(w) if ares is not None else DictionaryObject()
                for ab in aboxes:
                    im = re.search(r'/Id\s+(\d+)', ab['props'])
                    if not im:
                        continue
                    mid = int(im.group(1))
                    fx_stream = ('1 0 0 1 %.4f %.4f cm\n%s\n'
                                 % (-ab['x0'], -ab['y0'], ab['inner'])).encode('latin1')
                    form = DecodedStreamObject()
                    form.set_data(fx_stream)
                    form[NameObject('/Type')] = NameObject('/XObject')
                    form[NameObject('/Subtype')] = NameObject('/Form')
                    form[NameObject('/BBox')] = ArrayObject([
                        FloatObject(-5.0), FloatObject(-10.0),
                        FloatObject(max(1.0, ab['width']) + 5.0), FloatObject(28.0),
                    ])
                    form[NameObject('/Resources')] = ares_cloned
                    fref = w._add_object(form)
                    atlas_forms[mid] = {
                        'ref': fref,
                        'width': ab['width'],
                        'name': '/FAM%d' % mid,
                    }

        fmeta_pages = (fmeta or {}).get('pages') or []
        ftoc_set = (fmeta or {}).get('ftoc_pages')
        fpr_set = (fmeta or {}).get('fpr_pages')
        has_patch_index = (ftoc_set is not None and fpr_set is not None)

        for i in range(a, body_end):
            pg = w.add_page(r_pages[i])
            total_pages += 1
            local_idx = i - a
            abs_pg = abs_start + local_idx

            # 顺手收集本页的 GoToR 注释（避免结尾再次全书遍历 w.pages）
            annots = pg.get('/Annots')
            if annots is not None:
                try:
                    for annot in annots.get_object():
                        obj = annot.get_object()
                        act = obj.get('/A')
                        if act is None:
                            continue
                        act = act.get_object()
                        if act.get('/S') == '/GoToR':
                            gotor_acts.append(act)
                except Exception:
                    pass

            ps, lid, rid = (fmeta_pages[i] if i < len(fmeta_pages) else ('empty', -1, -1))
            need_stamp = ps in ('plain', 'headings')

            if has_patch_index:
                may_ftoc = bool(toc_map) and (i in ftoc_set)
                may_fpr = bool(lbl_map) and (i in fpr_set)
                need_decode = may_ftoc or may_fpr
            else:
                need_decode = bool(toc_map) or bool(lbl_map)

            if not need_decode:
                # 极速零解压路径（占全书 90%+）：原压缩内容流丝毫不动，只挂 80B 盖印流
                if not need_stamp:
                    continue
                res = pg.get('/Resources')
                if res is None:
                    res = DictionaryObject()
                    pg[res_name] = res
                else:
                    res = res.get_object()
                if '/Font' not in res:
                    res[font_name] = DictionaryObject()
                res['/Font'].get_object()[fkd0_name] = dig_ref
                stamp_ops = _build_stamp_ops(ps, abs_pg, lid, rid, res, atlas_forms)
                if stamp_ops:
                    _append_stamp_stream(pg, stamp_ops)
                continue

            cs = pg.get_contents()
            raw = cs.get_data().decode('latin1', errors='replace') if cs is not None else ''
            has_ftoc = bool(toc_map) and ('/FTOC' in raw)
            has_fpr = bool(lbl_map) and ('/FPR' in raw)

            if not (has_ftoc or has_fpr):
                if not need_stamp:
                    continue
                res = pg.get('/Resources')
                if res is None:
                    res = DictionaryObject()
                    pg[res_name] = res
                else:
                    res = res.get_object()
                if '/Font' not in res:
                    res[font_name] = DictionaryObject()
                res['/Font'].get_object()[fkd0_name] = dig_ref
                stamp_ops = _build_stamp_ops(ps, abs_pg, lid, rid, res, atlas_forms)
                if stamp_ops:
                    _append_stamp_stream(pg, stamp_ops)
                continue

            res = pg.get('/Resources')
            if res is None:
                res = DictionaryObject()
                pg[res_name] = res
            else:
                res = res.get_object()
            if '/Font' not in res:
                res[font_name] = DictionaryObject()
            fonts_dict = res['/Font'].get_object()

            if has_ftoc:
                fonts_dict[fkd0_name] = kit_fonts['DIGS']['ref']
                fonts_dict[fkdb_name] = kit_fonts['BDIGS']['ref']
                raw = _FTOC_RE.sub(lambda mm: _patch_ftoc(mm, fonts_dict), raw)

            if has_fpr:
                fonts_dict[fkd0_name] = kit_fonts['DIGS']['ref']
                fonts_dict[fkdb_name] = kit_fonts['BDIGS']['ref']
                fonts_dict[fkds_name] = kit_fonts['SDIGS']['ref']
                raw = _FPR_RE.sub(lambda mm: _patch_fpr(mm, fonts_dict), raw)

            if need_stamp:
                fonts_dict[fkd0_name] = dig_ref
                stamp_ops = _build_stamp_ops(ps, abs_pg, lid, rid, res, atlas_forms)
                if stamp_ops:
                    raw = raw + '\n' + '\n'.join(stamp_ops) + '\n'

            ns = DecodedStreamObject()
            ns.set_data(raw.encode('latin1'))
            pg[contents_name] = w._add_object(ns)

        # 搬运命名目标（O(1) 页对象 ID 查表，忽略落到 Atlas 页的目标）
        try:
            page_id_to_idx = {}
            for pidx, rpg in enumerate(r_pages):
                iref = getattr(rpg, 'indirect_reference', None)
                if iref is not None:
                    page_id_to_idx[iref.idnum] = pidx
            for name, dest in r.named_destinations.items():
                pi = None
                dpg = getattr(dest, 'page', None)
                if dpg is not None and hasattr(dpg, 'idnum'):
                    pi = page_id_to_idx.get(dpg.idnum)
                if pi is None:
                    try:
                        pi = r.get_destination_page_number(dest)
                    except Exception:
                        continue
                if pi is None or pi < a or pi >= body_end:
                    continue
                idx = off + (pi - a)
                if idx < total_pages:
                    dests.setdefault(str(name), idx)
        except Exception:
            pass

    # 重建书签树
    ranks = {'part': 0, 'chapter': 1, 'section': 2, 'subsection': 3}
    parents = {}
    for e in outline or []:
        rk = ranks.get(e.get('level'))
        if rk is None:
            continue
        idx = int(e.get('page') or 0) - 1
        if idx < 0 or idx >= total_pages:
            continue
        for k in list(parents):
            if k >= rk:
                del parents[k]
        parent = parents.get(rk - 1) if rk else None
        try:
            parents[rk] = w.add_outline_item(e.get('title') or '', idx, parent=parent)
        except Exception:
            pass

    for name, idx in dests.items():
        try:
            w.add_named_destination(name, idx)
        except Exception:
            pass

    # 将 xr-hyper 写出的 GoToR 改回内部 GoTo
    goto_name = NameObject('/GoTo')
    s_name = NameObject('/S')
    d_name = NameObject('/D')
    for act in gotor_acts:
        try:
            d = act.get('/D')
            if isinstance(d, str) and d in dests:
                act[s_name] = goto_name
                act[d_name] = TextStringObject(d)
                act.pop(NameObject('/F'), None)
        except Exception:
            pass

    tmp = out + '.tmp'
    with open(tmp, 'wb') as f:
        w.write(f)
    os.replace(tmp, out)
    return total_pages
