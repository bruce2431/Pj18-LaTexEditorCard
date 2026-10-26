# -*- coding: utf-8 -*-
"""编译服务：锁 → 快速路径 → 整本回落 → 账本 → synctex 基线。

对外只有 `CompilerService.compile(book_name, origin)` 与 `snapshot()`。
返回字段兼容旧版（ok/time/mtime/pages/log/error），并补充
`fast/mode/reason/phases/chunks/by`，供前端与日志卡显示。
"""
import os
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
                    reason_code='compiler_exception', error=msg, log='', mode='none')
            return {'ok': False, 'error': msg, 'log': '', 'time': 0,
                    'fast': False, 'mode': 'none', 'reason': msg,
                    'reason_code': 'compiler_exception'}
        finally:
            self.state.end(tok)

    # ---------- 内部 ----------
    def _run(self, b, origin, t_lock, tok=None):
        book_dir = books.book_dir(b['name'])
        build_dir = os.path.join(book_dir, '.build')
        os.makedirs(build_dir, exist_ok=True)
        mk = toolchain.find_tex_tool('latexmk')
        tex = toolchain.find_tex_tool('xelatex')
        perl = toolchain.find_perl()
        use_mk = bool(mk and perl)
        if not use_mk and not tex:
            msg = '未找到可用的 TeX 引擎（项目自带 tex/ 与系统 PATH 均无 xelatex）'
            return {'ok': False, 'error': msg, 'log': '', 'time': 0,
                    'fast': False, 'mode': 'none', 'reason': msg,
                    'reason_code': 'no_toolchain'}
        env = toolchain.sanitized_env()
        t_lock_end = time.time()

        # ---- 快速路径 ----
        self.state.set_phase('fast', token=tok)
        t0 = time.time()
        fr = None
        status = fastpath.STATUS_FAILED
        reason_code = 'fastpath_exception'
        skip = ''
        try:
            fr = fastpath.try_compile(book_dir, b['name'], mk, perl, env)
            if isinstance(fr, dict):
                status = fr.get('status') or ('ok' if fr.get('ok') else fastpath.STATUS_FAILED)
                reason_code = fr.get('reason_code')
                skip = fr.get('reason') or ''
            else:                           # 旧 shim 返回值兼容
                status = fastpath.STATUS_OK if fr else fastpath.STATUS_FAILED
                reason_code = None if fr else 'fastpath_failed'
                skip = '' if fr else (fastpath.LAST_SKIP or '快速路径未给出原因')
        except Exception as ex:                 # noqa: BLE001
            log.exception('fastpath.try_compile', ex)
            status = fastpath.STATUS_FAILED
            reason_code = 'fastpath_exception'
            skip = '快速路径异常：%s' % ex
        t_fast = time.time()
        if status == fastpath.STATUS_OK and fr:
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
            return fr

        if status == fastpath.STATUS_FAILED:
            # 快速路径真实失败：直接报错，不再隐式整本重编。
            elapsed = round(t_fast - t0, 2)
            out_log = (fr or {}).get('log') or skip
            msg = '快速编译失败[%s]：%s' % (reason_code or 'unknown', skip or '未给出原因')
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

        # unsupported：结构/环境不适用，明确选择整本模式（不是「回落」）
        log.info('整本模式（原因：%s %s）' % (reason_code or 'unsupported', skip))

        # ---- 整本回落 ----
        self.state.set_phase('full', token=tok)
        chain = ('[编译链] latexmk -xelatex 增量（perl: %s）' % perl) if use_mk else \
                '[编译链] xelatex 直驱两遍（未找到 perl，已跳过 latexmk；较慢）'
        if skip or reason_code:
            chain += '\n[编译链] 整本模式（原因：%s %s）' % (reason_code or 'unsupported', skip)
        syn_p = books.book_synctex_path(b['name'])
        front = _front_guess(book_dir)
        fh = fastpath.front_hash_of(book_dir)
        # 现有 main.synctex.gz 若含前置页就就地采纳为基线，不必为了基线整本重编；
        # 前置页正文改过（hash 不符）则旧基线作废，必须整本重编
        bl_ok = (os.path.isfile(syn_p)
                 and fastpath.ensure_front_baseline(book_dir, front, fh)[0])
        need_front = bool(front) and not bl_ok
        tex_p = os.path.join(book_dir, b['texName'])
        pdf_p = books.book_pdf_path(b['name'])
        # 源比 PDF 新 ⇒ latexmk 必然跑一遍 xelatex，synctex 会整份重写，不必 -g；
        # 只有「无事可做」的增量才直接 -g（否则留下的仍是旧的不完整 synctex）
        fresh = (os.path.isfile(pdf_p) and os.path.isfile(tex_p)
                 and os.path.getmtime(tex_p) > os.path.getmtime(pdf_p))
        if need_front and not fresh and tex:
            # PDF 已是最新，只缺一份含前置页的 synctex：单遍 xelatex 就能整份重写，
            # 比 latexmk -g 全量重编便宜得多（-g 常多花几十秒）。
            # 必须 -no-pdf：XDV 模式与各章分块/ latexmk 的 synctex 头部偏移一致
            # （PDF 模式会写成 X/Y Offset 0，合并时被判参数不一致）。
            self.state.set_phase('synctex', token=tok)
            t_rep = time.time()
            try:
                rc_r, out_r = toolchain.run_tool(
                    [tex, '-no-pdf', '-synctex=1', '-interaction=nonstopmode',
                     '-halt-on-error', '-file-line-error', '-output-directory=.build',
                     b['texName']],
                    cwd=book_dir, timeout=config.COMPILE_TIMEOUT, env=env)
                if rc_r == 0:
                    toolchain.hoist_outputs(book_dir)
                    if _syn_front_ok(syn_p, front):
                        log.info('已用单遍 xelatex 补齐前置页 synctex（%.1fs），免去 -g 整本重编'
                                 % (time.time() - t_rep))
                        need_front = False
                else:
                    log.warn('单遍 xelatex 补 synctex 失败（返回码 %s），改走 -g' % rc_r)
            except Exception as ex:             # noqa: BLE001
                log.exception('synctex 修复', ex)
            self.state.set_phase('full', token=tok)
        cmd_force = None
        if use_mk:
            base = [mk, '-xelatex', '-synctex=1']
            tail = ['-interaction=nonstopmode', '-halt-on-error', '-file-line-error',
                    '-auxdir=.build', b['texName']]
            cmd = base + (['-g'] if (need_front and not fresh) else []) + tail
            cmd_force = base + ['-g'] + tail
            env['PATH'] = os.path.dirname(perl) + os.pathsep + env.get('PATH', '')
        else:
            cmd = [tex, '-synctex=1', '-interaction=nonstopmode', '-halt-on-error',
                   '-file-line-error', '-output-directory=.build', b['texName']]
        tf0 = time.time()
        try:
            rc, out = toolchain.run_tool(cmd, cwd=book_dir,
                                         timeout=config.COMPILE_TIMEOUT, env=env)
            if rc is None and out == 'timeout':
                raise TimeoutError('timeout')
            second = None
            if not use_mk and rc == 0:
                second = toolchain.run_tool(cmd, cwd=book_dir,
                                            timeout=config.COMPILE_TIMEOUT, env=env)
                if second[0] is None and second[1] == 'timeout':
                    raise TimeoutError('timeout')
            # 增量其实没跑 xelatex（Nothing to do）→ synctex 还是旧的不完整版本，
            # 强制 -g 再走一遍，确保这次编译真正产出前置页映射
            if use_mk and rc == 0 and need_front and not _syn_front_ok(syn_p, front):
                log.warn('整本增量未重写 synctex（前置页仍缺映射），强制 -g 重编一次')
                rc, out = toolchain.run_tool(cmd_force, cwd=book_dir,
                                             timeout=config.COMPILE_TIMEOUT, env=env)
                if rc is None and out == 'timeout':
                    raise TimeoutError('timeout')
            elapsed = round(time.time() - tf0, 1)
            toolchain.hoist_outputs(book_dir)
            pdf_p = books.book_pdf_path(b['name'])
            final_rc = second[0] if second else rc
            if final_rc == 0 and os.path.isfile(pdf_p):
                stdout = (second[1] if second else out) or ''
                full_log = stdout + '\n' + chain
                pages = ledger.pages_in(full_log)
                if pages is None:
                    _ok, pages = ledger.scrape_log(os.path.join(build_dir, 'main.log'))
                if pages is None:
                    pages = toolchain._pdf_pages(pdf_p)
                # 整本编译成功 → 刷新按章缓存 + 留存前置页 synctex 基线
                try:
                    fastpath.refresh_cache(book_dir)
                    fastpath.save_front_baseline(book_dir, front_hash=fh)
                except Exception as ex:         # noqa: BLE001
                    log.exception('refresh_cache/save_front_baseline', ex)
                phases = {'lock': round(t_lock_end - t_lock, 2),
                          'plan': round(t_fast - t0, 2),
                          'full': elapsed, 'total': round(time.time() - t_lock, 2)}
                _ledger(b['name'], ok=True, elapsed=elapsed, pages=pages, by=origin,
                        mode='full', reason=skip or None, reason_code=reason_code or None,
                        phases=phases, chunks=None, log=full_log[-1200:])
                return {'ok': True, 'time': elapsed,
                        'mtime': int(os.path.getmtime(pdf_p) * 1000),
                        'pages': pages, 'log': full_log[-1200:], 'fast': False,
                        'mode': 'full', 'reason': skip or None,
                        'reason_code': reason_code or None,
                        'phases': phases, 'chunks': None, 'by': origin}
            full_log = (out or '') + '\n' + ((second or ('', ''))[1] or '')
            errs = [ln for ln in full_log.splitlines()
                    if ln.startswith('!') or ':error:' in ln or 'Emergency stop' in ln
                    or 'did not succeed' in ln or 'could not find' in ln]
            tail = ('\n'.join(errs[-12:]) or full_log[-1500:]) + '\n' + chain
            err = '编译失败（返回码 %s）' % final_rc
            phases = {'lock': round(t_lock_end - t_lock, 2),
                      'plan': round(t_fast - t0, 2),
                      'full': elapsed, 'total': round(time.time() - t_lock, 2)}
            _ledger(b['name'], ok=False, elapsed=elapsed, by=origin, mode='full',
                    reason=skip or None, reason_code=reason_code or None,
                    phases=phases, error=err, log=tail)
            return {'ok': False, 'time': elapsed, 'log': tail, 'error': err,
                    'fast': False, 'mode': 'full', 'reason': skip or None,
                    'reason_code': reason_code or None,
                    'phases': phases, 'chunks': None, 'by': origin}
        except Exception as ex:                 # noqa: BLE001
            log.exception('整本编译', ex)
            elapsed = round(time.time() - tf0, 1)
            msg = ('编译超时（%ss）——首次编译装包或图形宏包可能太慢，'
                   '可先在 VSCode 里编译一次' % config.COMPILE_TIMEOUT
                   if 'timeout' in str(ex).lower() else '编译异常：%s: %s' % (type(ex).__name__, ex))
            _ledger(b['name'], ok=False, elapsed=elapsed, by=origin, mode='full',
                    reason=skip or None, reason_code=reason_code or None,
                    error=msg, log=chain)
            return {'ok': False, 'time': elapsed, 'error': msg, 'log': chain,
                    'fast': False, 'mode': 'full', 'reason': skip or None,
                    'reason_code': reason_code or None,
                    'phases': {'lock': round(t_lock_end - t_lock, 2),
                               'plan': round(t_fast - t0, 2),
                               'full': elapsed, 'total': round(time.time() - t_lock, 2)},
                    'chunks': None, 'by': origin}


def _syn_front_ok(syn_p, front):
    """整本 synctex 是否覆盖 1..front（读不出 / 缺页都算不可用）。"""
    if not front:
        return True
    pages = fastpath.sync_pages(syn_p)
    return bool(pages) and all(p in pages for p in range(1, int(front) + 1))


def _front_guess(book_dir):
    """前置页数估计：按章缓存优先，其次整本 PDF 的编号章书签。"""
    try:
        cache = fastpath.read_cache(book_dir) or {}
        if cache.get('front') is not None:
            return int(cache['front'] or 0)
        outline = fastpath._outline_starts(os.path.join(book_dir, 'main.pdf'))
        return max(0, int(outline[0]) - 1) if outline else 0
    except Exception:                           # noqa: BLE001
        return 0


def _write_main_log(book_dir, text):
    """快速路径没有 latexmk 的 `.build/main.log`：写一份规范日志，供日志卡读取。"""
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
