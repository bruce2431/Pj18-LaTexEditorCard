# -*- coding: utf-8 -*-
"""书稿发现、路径与 PDF 状态（代码搬自原 server.py + 2s TTL 缓存）。"""
import os
import time

from . import config

# 前端每 4s 轮询 /api/books，原来每次都对整棵树 os.walk 两遍。缓存 2s 足够，
# 编译/新建后由 invalidate() 主动失效，保证按钮状态仍然即时。
_CACHE = {'ts': 0.0, 'data': None}


def invalidate():
    _CACHE['data'] = None
    _CACHE['ts'] = 0.0


def discover_books(force=False):
    """递归扫描根目录下「含 .tex 文件」的目录 → 书稿列表（一个目录 = 一篇文章）。

    跳过隐藏目录（.build/.git/…）与 node_modules/__pycache__ 等构建垃圾；
    **不跳过临时任务目录**（YYYYMMDDHHMMSS-名称）。根目录自身直接放的 .tex 不算书稿。
    书稿名 = 相对根目录的 POSIX 路径（可含 '/'）；mtime = 主 .tex 的 int 毫秒。
    """
    now = time.time()
    if not force and _CACHE['data'] is not None and now - _CACHE['ts'] < config.BOOKS_TTL:
        return [dict(b) for b in _CACHE['data']]
    books = []
    root_abs = os.path.abspath(config.BOOKS_ROOT)
    for cur, dirs, files in os.walk(root_abs):
        dirs[:] = [d for d in dirs if not d.startswith('.') and d not in config.SKIP_DIRS]
        if os.path.abspath(cur) == root_abs:
            continue  # 根目录自身不算书稿
        texs = sorted(f for f in files if f.lower().endswith('.tex') and not f.startswith('.'))
        if not texs:
            continue
        lower = [f.lower() for f in texs]
        tex_name = texs[lower.index('main.tex')] if 'main.tex' in lower else texs[0]
        images = sorted(f for f in files
                        if os.path.splitext(f)[1].lower() in config.IMG_EXT and not f.startswith('.'))
        rel = os.path.relpath(cur, root_abs).replace(os.sep, '/')
        try:
            mt = os.path.getmtime(os.path.join(cur, tex_name))
        except OSError:
            try:
                mt = os.path.getmtime(cur)
            except OSError:
                mt = 0
        books.append({'name': rel, 'texName': tex_name, 'images': images, 'mtime': int(mt * 1000)})
    books.sort(key=lambda b: b['name'])
    _CACHE['ts'], _CACHE['data'] = now, books
    return [dict(b) for b in books]


def find_book(name):
    for b in discover_books():
        if b['name'] == name:
            return b
    return None


def book_dir(name):
    return os.path.join(config.BOOKS_ROOT, name)


def book_pdf_path(name):
    """成品 PDF 的唯一落点 = 书稿根的 `main.pdf`（中间产物全在 `<书稿>/.build/`）。"""
    return os.path.join(config.BOOKS_ROOT, name, 'main.pdf')


def book_synctex_path(name):
    """「点 PDF 跳源码」用的 synctex —— 跟成品 PDF 一起输出到书稿根。"""
    return os.path.join(config.BOOKS_ROOT, name, 'main.synctex.gz')


def newest_tex_mtime(book_name):
    """书稿目录下所有 .tex 的最新 mtime（含分章子目录，跳过 .build/隐藏目录）。"""
    base = os.path.join(config.BOOKS_ROOT, book_name)
    newest = 0.0
    for cur, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if not d.startswith('.') and d not in ('node_modules', '__pycache__')]
        for f in files:
            if f.lower().endswith('.tex') and not f.startswith('.'):
                try:
                    newest = max(newest, os.path.getmtime(os.path.join(cur, f)))
                except OSError:
                    pass
    return newest


def book_pdf_info(b):
    """exists=有 main.pdf；fresh=pdf 比任何 .tex 新（无需重编）。"""
    pdf_p = book_pdf_path(b['name'])
    if not os.path.isfile(pdf_p):
        return {'exists': False, 'fresh': False, 'mtime': 0}
    try:
        tex_mtime = newest_tex_mtime(b['name']) or os.path.getmtime(
            os.path.join(config.BOOKS_ROOT, b['name'], b['texName']))
        pdf_mtime = os.path.getmtime(pdf_p)
    except OSError:
        return {'exists': True, 'fresh': False, 'mtime': 0}
    return {'exists': True, 'fresh': pdf_mtime >= tex_mtime - 1, 'mtime': int(pdf_mtime * 1000)}


def books_with_pdf():
    """/api/books：列表 + 每本 pdf 状态（目录页要显示已编译/未编译）。"""
    books = discover_books()
    for b in books:
        b['pdf'] = book_pdf_info(b)
    return books
