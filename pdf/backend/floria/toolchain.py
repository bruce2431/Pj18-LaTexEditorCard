# -*- coding: utf-8 -*-
"""TeX / perl 工具定位与子进程环境（代码搬自原 server.py，逻辑不变）。"""
import os
import shutil
import subprocess

from . import config
from .log import warn

# 编译工具优先用**项目自带**的（APP_DIR/tex/ 下任意深度的同名可执行文件），
# 找不到才回落系统 PATH —— 使本应用自包含：整体拷到别的机器无需另装 TeX。
_TEX_TOOL_CACHE = {}
_PERL_CACHE = {}


def find_tex_tool(name):
    """定位 TeX 工具：先项目自带 tex/<任意深度>/<name>(.exe)，再系统 PATH。结果缓存。"""
    if name in _TEX_TOOL_CACHE:
        return _TEX_TOOL_CACHE[name]
    exe = (name + '.exe') if os.name == 'nt' else name
    found = None
    root = config.TEX_DIR
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


def find_perl():
    """定位 perl（latexmk 的脚本引擎）；找不到返回 None（调用方回落直驱 xelatex）。结果缓存。"""
    if 'perl' in _PERL_CACHE:
        return _PERL_CACHE['perl']
    exe = 'perl.exe' if os.name == 'nt' else 'perl'
    cands = [os.path.join(config.APP_DIR, 'tex', 'perl', 'bin', exe),   # 与 MiKTeX 同处一室
             os.path.join(config.APP_DIR, 'tex', 'perl', exe)]
    d = config.APP_DIR                                          # 工作区便携约定：逐级向上找 .tools/perl
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


def sanitized_env():
    """编译子进程环境：剥掉 git-bash 导出的 LANG/LC_ALL/LC_CTYPE。

    MiKTeX 自带 perl 在 zh_CN.UTF-8 下把带中文的脚本路径转码失败
    （"Can't open perl script"，0.7s 即退；2026-10-14 实测踩过）。
    """
    env = os.environ.copy()
    for k in ('LANG', 'LC_ALL', 'LC_CTYPE'):
        env.pop(k, None)
    return env


def creation_flags():
    return subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0


def run_tool(cmd, cwd=None, timeout=60, env=None):
    """跑一个外部命令，永不抛异常：返回 (returncode, stdout+stderr)。"""
    try:
        p = subprocess.run(cmd, cwd=cwd, timeout=timeout, capture_output=True, text=True,
                           encoding='utf-8', errors='replace',
                           creationflags=creation_flags(), env=env)
        return p.returncode, (p.stdout or '') + (p.stderr or '')
    except subprocess.TimeoutExpired:
        return None, 'timeout'
    except OSError as e:
        return None, str(e)


def _pdf_pages(pdf_p):
    """pdfinfo 数成品页数（便携链自带）；拿不到给 None（前端显示「—」）。"""
    exe = find_tex_tool('pdfinfo')
    if not exe:
        return None
    rc, out = run_tool([exe, pdf_p], timeout=60)
    if rc is None:
        return None
    for ln in out.splitlines():
        if ln.lower().startswith('pages:'):
            try:
                return int(ln.split(':', 1)[1].strip())
            except ValueError:
                return None
    return None


def hoist_outputs(book_dir):
    """把误落在 .build/ 里的成品搬回书稿根（latexmk -auxdir 已自己搬，此处兜直驱回落路径）。"""
    for f in ('main.pdf', 'main.synctex.gz'):
        src = os.path.join(book_dir, '.build', f)
        dst = os.path.join(book_dir, f)
        try:
            if os.path.isfile(src) and (not os.path.isfile(dst)
                                        or os.path.getmtime(src) > os.path.getmtime(dst)):
                shutil.move(src, dst)
        except OSError as e:
            warn('搬移成品失败 %s: %s' % (f, e))
