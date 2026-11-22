# -*- coding: utf-8 -*-
"""编译服务：锁 → 无改动直通 → 按章并行快速路径 → 账本。

**只有一条编译路径**：`fastpath.try_compile`。整本 latexmk 兜底已删除（2026-10-27），
结构不适用（`\\appendix`）或真实故障都直接报错并把原因码写进账本。
对外只有 `CompilerService.compile(book_name, origin)` 与 `snapshot()`。
返回字段兼容旧版（ok/time/mtime/pages/log/error），并补充
`fast/mode/reason/reason_code/phases/chunks/by`，供前端与日志卡显示。
"""
import os
import shutil
import time

from . import books, config, fastpath, ledger, locks, log, toolchain


class CompilerService:
    def __init__(self):
        self.state = locks.CompileState()

    def snapshot(self):
        return self.state.snapshot()

    def compile(self, book_name, origin='page'):
        """编译一本书。永不抛异常：失败/繁忙都体现在返回 dict 里。"""
        b = books.find_book(book_name)
        if not b:
            return {'ok': False, 'error': 'book not found'}
        # 同一进程内的并发也允许并发读状态；能否真正编译交给跨进程文件锁裁决
        tok = self.state.begin(b['name'])
        t_lock = time.time()
        try:
            with locks.BuildLock(books.book_dir(b['name']), by=origin) as lk:
                self.state.set_phase('compile', lk.owner(), token=tok)
                return self._run(b, origin, t_lock, tok)
        except locks.Busy as e:
            log.warn('编译被占用：%s' % e)
            return {'ok': False, 'busy': True, 'error': str(e)}
        except Exception as ex:                 # noqa: BLE001
            log.exception('compiler.compile', ex)
            msg = '编译服务异常：%s: %s' % (type(ex).__name__, ex)
            _ledger(b['name'], ok=False, elapsed=None, by=origin, reason=msg,
                    reason_code='compiler_exception', error=msg, log='', mode='fast')
            return {'ok': False, 'error': msg, 'log': '', 'time': 0,
                    'fast': False, 'mode': 'fast', 'reason': msg,
                    'reason_code': 'compiler_exception'}
        finally:
            self.state.end(tok)

    # ---------- 内部 ----------
    def _run(self, b, origin, t_lock, tok=None):
        book_dir = books.book_dir(b['name'])
        build_dir = os.path.join(book_dir, '.build')
        os.makedirs(build_dir, exist_ok=True)
        tex = toolchain.find_tex_tool('xelatex')
        if not tex and not shutil.which('xelatex'):
            msg = '未找到可用的 xelatex（项目自带 tex/ 与系统 PATH 均无）'
            return {'ok': False, 'error': msg, 'log': '', 'time': 0,
                    'fast': False, 'mode': 'fast', 'reason': msg,
                    'reason_code': 'no_toolchain'}
        env = toolchain.sanitized_env()
        t_lock_end = time.time()

        # ---- 无改动直通 ----
        # 源码指纹与成品都和上次成功编译一致 ⇒ 没必要再编（iPad 反复点编译、
        # 打开时自动校验等场景 0 秒返回）。图片变化也计入指纹。
        src_fp = fastpath.source_fingerprint(book_dir)
        reuse_pages = fastpath.unchanged_output(book_dir, src_fp)
        if reuse_pages is not None:
            pdf_p = books.book_pdf_path(b['name'])
            pages = reuse_pages or toolchain._pdf_pages(pdf_p)
            msg = '源码未变化，复用现有成品'
            _write_main_log(book_dir, '[编译链] ' + msg)
            _ledger(b['name'], ok=True, elapsed=0.0, pages=pages, by=origin,
                    mode='fast', reason=None, reason_code=None, phases={},
                    chunks=None, log='[编译链] ' + msg)
            return {'ok': True, 'time': 0.0,
                    'mtime': int(os.path.getmtime(pdf_p) * 1000), 'pages': pages,
                    'log': '[编译链] ' + msg, 'fast': True, 'noop': True,
                    'mode': 'fast', 'reason': None, 'reason_code': None,
                    'phases': {}, 'chunks': None, 'by': origin}

        # ---- 快速路径（唯一编译路径）----
        self.state.set_phase('fast', token=tok)
        t0 = time.time()
        fr = None
        reason_code = 'fastpath_exception'
        skip = ''
        try:
            fr = fastpath.try_compile(book_dir, b['name'], tex, env)
            if isinstance(fr, dict):
                reason_code = fr.get('reason_code')
                skip = fr.get('reason') or ''
            else:
                reason_code = None if fr else 'fastpath_failed'
                skip = '' if fr else (fastpath.LAST_SKIP or '快速路径未给出原因')
        except Exception as ex:                 # noqa: BLE001
            log.exception('fastpath.try_compile', ex)
            reason_code = 'fastpath_exception'
            skip = '快速路径异常：%s' % ex
            fr = None
        t_fast = time.time()
        elapsed = round(t_fast - t0, 2)

        if fr and fr.get('status', fastpath.STATUS_OK) == fastpath.STATUS_OK:
            phases = dict(fastpath.LAST_PHASES)
            phases.update({'lock': round(t_lock_end - t_lock, 2),
                           'plan': round(t_fast - t0 - sum(phases.values()), 2),
                           'total': round(t_fast - t_lock, 2)})
            _ledger(b['name'], ok=True, elapsed=fr.get('time'), pages=fr.get('pages'),
                    by='chapter', mode='fast', reason=None, reason_code=None, phases=phases,
                    chunks=fastpath.LAST_CHUNKS or None, log=fr.get('log', ''))
            _write_main_log(book_dir, fr.get('log', ''))
            fr.update({'fast': True, 'mode': 'fast', 'reason': None, 'reason_code': None,
                       'phases': phases, 'chunks': fastpath.LAST_CHUNKS or None,
                       'by': 'chapter'})
            fastpath.write_last_ok(book_dir, src=src_fp, pages=fr.get('pages'))
            return fr

        # ---- 失败：直接报错（没有整本兜底可回落）----
        out_log = (fr or {}).get('log') or skip
        msg = '编译失败[%s]：%s' % (reason_code or 'unknown', skip or '未给出原因')
        phases = {'lock': round(t_lock_end - t_lock, 2),
                  'plan': elapsed, 'total': round(t_fast - t_lock, 2)}
        log.warn(msg)
        _write_main_log(book_dir, out_log)
        _ledger(b['name'], ok=False, elapsed=elapsed, by=origin, mode='fast',
                reason=skip, reason_code=reason_code, phases=phases,
                error=msg, log=out_log)
        return {'ok': False, 'time': elapsed, 'log': out_log, 'error': msg,
                'fast': True, 'mode': 'fast', 'reason': skip,
                'reason_code': reason_code, 'phases': phases, 'chunks': None,
                'by': origin}


def _write_main_log(book_dir, text):
    """快速路径没有整本 latexmk 的 `.build/main.log`：写一份规范日志，供日志卡读取。"""
    try:
        d = os.path.join(book_dir, '.build')
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, 'main.log')
        tmp = p + '.tmp'
        with open(tmp, 'w', encoding='utf-8', errors='replace') as f:
            f.write(text or '')
        os.replace(tmp, p)
    except OSError as ex:
        log.warn('写 .build/main.log 失败：%s' % ex)


def _ledger(name, ok, elapsed, by, mode, reason=None, reason_code=None, phases=None,
            chunks=None, error='', log='', pages=None):
    ledger.ledger_append(name, {
        'ts': round(time.time(), 3), 'ok': bool(ok), 'elapsed': elapsed,
        'pages': pages, 'by': by, 'fast': mode == 'fast',
        'mode': mode, 'reason': reason, 'reason_code': reason_code,
        'phases': phases, 'chunks': chunks,
        'error': error or '', 'log': log or '',
    })


def warmup(state=None, root=None):
    """启动预热：将便携 xelatex/xdvipdfmx/fmt/CJK 字体加载进系统页缓存，并预热各书稿图元与尺寸缓存。

    不抢占书稿 `.compile.lock`；若检测到用户正在编译则主动礼让，绝不阻塞真实请求。
    """
    import subprocess
    import tempfile
    from . import pdf_stamp, tikz_cache

    def _yield_if_busy():
        while state is not None and state.snapshot().get('state') != 'idle':
            time.sleep(0.4)

    t0 = time.time()
    tex = toolchain.find_tex_tool('xelatex') or shutil.which('xelatex')
    if not tex:
        return {'ok': False, 'reason': 'no_xelatex'}
    xdv = toolchain.find_tex_tool('xdvipdfmx') or shutil.which('xdvipdfmx') or ''
    env = toolchain.sanitized_env()
    pdf_stamp.find_native_merger()

    # 1) 极小 CJK + 数学页预热：把 xelatex.exe / MiKTeX DLL / xelatex.fmt / 字体映射预读进 OS 缓存
    _yield_if_busy()
    try:
        with tempfile.TemporaryDirectory(prefix='floria_warm_') as td:
            wtex = os.path.join(td, 'warm.tex')
            with open(wtex, 'w', encoding='utf-8') as f:
                f.write('\\documentclass[UTF8]{ctexbook}\n\\usepackage{amsmath,amssymb}\n'
                        '\\begin{document}\n预热$x_1+y_2$\n\\end{document}\n')
            cmd = [tex]
            if 'miktex' in tex.lower():
                cmd.append('--disable-installer')
            cmd += ['-interaction=nonstopmode', '-halt-on-error', '-no-pdf', 'warm.tex']
            subprocess.run(cmd, cwd=td, env=env, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=25)
            wxdv = os.path.join(td, 'warm.xdv')
            if xdv and os.path.isfile(wxdv):
                subprocess.run([xdv, '-q', '-E', '-z', '1', '-o', 'warm.pdf', 'warm.xdv'],
                               cwd=td, env=env, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=15)
    except Exception as ex:                     # noqa: BLE001
        log.warn('工具链页缓存预热跳过：%s' % ex)

    # 2) 各书稿配图尺寸、解耦套件与 TikZ/chemfig 图元缓存预热
    books_root = root or config.BOOKS_ROOT
    warmed_books = 0
    built_diagrams = 0
    if books_root and os.path.isdir(books_root):
        for entry in sorted(os.listdir(books_root)):
            if entry.startswith('.'):
                continue
            bdir = os.path.join(books_root, entry)
            if not os.path.isfile(os.path.join(bdir, 'main.tex')):
                continue
            _yield_if_busy()
            if os.path.exists(os.path.join(bdir, '.build', '.compile.lock')):
                continue
            try:
                doc, _ = fastpath._split_document(bdir)
                if not doc:
                    continue
                pdf_stamp.run_native_prep(
                    bdir,
                    chunk_ids=[c['id'] for c in doc['chunks']],
                    write_agg_aux=False,
                    scan_images=True,
                    upgrade_tikz_dp=True,
                )
                if pdf_stamp.can_decouple(doc['preamble'], doc['front'], doc['chunks']):
                    pdf_stamp.ensure_kit(bdir, doc['preamble'], env, tex, tex, xdv)
                for ch in doc['chunks']:
                    _yield_if_busy()
                    if os.path.exists(os.path.join(bdir, '.build', '.compile.lock')):
                        break
                    _, st = tikz_cache.preprocess_chunk_tikz(
                        bdir, doc['preamble'], ch['text'], env, tex, tex, xdv
                    )
                    built_diagrams += st.get('built', 0)
                warmed_books += 1
            except Exception as ex:             # noqa: BLE001
                log.warn('书稿预热跳过(%s)：%s' % (entry, ex))

    dt = round(time.time() - t0, 2)
    log.info('启动预热完成：%.2fs（书稿 %d 本，新建图元 %d 个）'
             % (dt, warmed_books, built_diagrams))
    return {'ok': True, 'elapsed': dt, 'books': warmed_books, 'built_diagrams': built_diagrams}

