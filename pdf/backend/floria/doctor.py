# -*- coding: utf-8 -*-
"""自检：把「静默降级」变成可查询的健康报告（/api/health）。

检查项：依赖导入 / TeX 工具链 / MiKTeX 包库与格式 / 按章缓存 / 前置页 synctex / 源码指纹。
所有检查都短超时（<=5s），失败只记 fail/warn，绝不抛异常。
"""
import os
import time

from . import BUILD_ID, books, config, fastpath, source_fingerprint, synctex, toolchain


def _check(cid, status, detail, hint='', ms=None):
    return {'id': cid, 'status': status, 'detail': detail, 'hint': hint, 'ms': ms}


def _tool(name, kind='tex'):
    t0 = time.time()
    exe = toolchain.find_perl() if kind == 'perl' else toolchain.find_tex_tool(name)
    if not exe:
        return _check('tool.' + name, 'fail', '未找到',
                      '把便携 TeX 放回 pdf/backend/tex/'), None
    rc, out = toolchain.run_tool([exe, '--version'], timeout=config.TOOL_PROBE_TIMEOUT)
    ms = round((time.time() - t0) * 1000)
    if rc is None:
        return _check('tool.' + name, 'fail', '%s --version 超时/失败' % name, str(out)[:160], ms), exe
    if ms > 3000:
        return _check('tool.' + name, 'warn', '响应慢 %dms' % ms, '磁盘或杀软可能拖慢', ms), exe
    return _check('tool.' + name, 'ok', os.path.basename(exe), '', ms), exe


def _miktex_db():
    root = config.TEX_DIR
    files = [os.path.join(root, 'texmfs', 'data', 'miktex', 'cache', 'packages',
                          'miktex-zzdb1-2.9', 'mpm.ini'),
             os.path.join(root, 'texmfs', 'data', 'miktex', 'cache', 'packages',
                          'miktex-zzdb3-2.9', 'package-manifests.ini')]
    bad = []
    for p in files:
        try:
            if os.path.getsize(p) < 4096:
                bad.append(os.path.basename(p))
        except OSError:
            bad.append(os.path.basename(p) + '(缺失)')
    if bad:
        return _check('miktex.db', 'fail', '包数据库异常：' + ', '.join(bad),
                      '磁盘损坏迹象；需重新生成 MiKTeX 包库')
    return _check('miktex.db', 'ok', '包数据库正常')


def _miktex_fmt():
    root = config.TEX_DIR
    fmt = os.path.join(root, 'texmfs', 'data', 'miktex', 'data', 'le', 'xetex', 'xelatex.fmt')
    try:
        size = os.path.getsize(fmt)
    except OSError:
        return _check('miktex.fmt', 'fail', 'xelatex.fmt 缺失', '编译时会重建格式（每次慢 20s+）')
    if size < 1024 * 1024:
        return _check('miktex.fmt', 'fail', 'xelatex.fmt 只有 %d B' % size,
                      '格式文件损坏；删除后重新生成')
    return _check('miktex.fmt', 'ok', '%.1f MB' % (size / 1024.0 / 1024.0))


def _import_check():
    try:
        import pypdf  # noqa: F401
        return _check('import.pypdf', 'ok', 'pypdf 可用')
    except Exception as ex:                     # noqa: BLE001
        return _check('import.pypdf', 'fail', '缺少 pypdf：%s' % ex,
                      '按章并行路径不可用，只能整本编译')


def _cache_check(book):
    if not book:
        return _check('cache.book', 'warn', '未指定书稿', '传 ?book=<书稿名> 查看')
    cache = fastpath.read_cache(books.book_dir(book))
    if not cache:
        return _check('cache.book', 'warn', '还没有按章缓存', '先整本编译一次')
    chunks = cache.get('chunks') or []
    built = sum(1 for c in chunks if c.get('built'))
    return _check('cache.book', 'ok',
                  '前置页 %s · %d 块（已编 %d）' % (cache.get('front'), len(chunks), built))


def _synctex_front(book):
    if not book:
        return _check('synctex.front', 'warn', '未指定书稿')
    bdir = books.book_dir(book)
    cache = fastpath.read_cache(bdir)
    front = (cache or {}).get('front')
    if front is None:
        try:
            from .fastpath import _outline_starts
            outline = _outline_starts(os.path.join(bdir, 'main.pdf'))
            front = max(0, outline[0] - 1) if outline else None
        except Exception:                       # noqa: BLE001
            front = None
    path = books.book_synctex_path(book)
    if not os.path.isfile(path):
        return _check('synctex.front', 'fail', '没有 main.synctex.gz', '重新编译一次')
    try:
        b = books.find_book(book)
        data = synctex.load_synctex(path, os.path.basename(b['texName']), bdir)
    except Exception as ex:                     # noqa: BLE001
        return _check('synctex.front', 'fail', '解析失败：%s' % ex)
    if not front:
        return _check('synctex.front', 'warn', '前置页未知（尚未按章编译）')
    mapped = sum(1 for p in range(1, int(front) + 1) if data['pages'].get(p))
    status = 'ok' if mapped == int(front) else 'fail'
    return _check('synctex.front', status, '前置页 1..%d 有映射 %d 页' % (front, mapped),
                  '点目录页可能无映射；再整本编译一次以重建基线')


def _fingerprint():
    cur, _files = source_fingerprint()
    stale = cur[:12] != BUILD_ID
    return _check('source.fingerprint', 'warn' if stale else 'ok',
                  '%s%s' % (BUILD_ID, '（磁盘已更新，进程仍是旧代码）' if stale else ''),
                  '重启后端或等 --auto-restart 生效' if stale else '')


def run_checks(book=None, refresh=False):
    t0 = time.time()
    checks = [_import_check()]
    for name in ('xelatex', 'latexmk', 'xdvipdfmx'):
        c, _exe = _tool(name)
        checks.append(c)
    c, _perl = _tool('perl', 'perl')
    checks.append(c)
    checks.append(_miktex_db())
    checks.append(_miktex_fmt())
    checks.append(_cache_check(book))
    checks.append(_synctex_front(book))
    checks.append(_fingerprint())
    level = 'ok'
    if any(c['status'] == 'fail' for c in checks):
        level = 'broken'
    elif any(c['status'] == 'warn' for c in checks):
        level = 'warn'
    cur, files = source_fingerprint()
    return {'ok': level != 'broken', 'level': level, 'ts': round(time.time(), 3),
            'checks': checks,
            'fingerprint': {'hash': cur, 'build_id': BUILD_ID,
                            'stale': cur[:12] != BUILD_ID, 'files': len(files)},
            'elapsed': round(time.time() - t0, 3)}
