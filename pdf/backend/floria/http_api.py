# -*- coding: utf-8 -*-
"""HTTP 接口与静态托管（纯标准库）。所有 endpoint 的异常都收敛成 JSON。"""
import json
import os
import posixpath
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler

from . import books, compiler, config, doctor, ledger, log, synctex

COMPILER = compiler.CompilerService()

NEW_BOOK_TEX = r"""\documentclass[UTF8]{ctexart}
\usepackage{amsmath,amssymb,graphicx}
\title{%(title)s}
\author{}
\date{\today}
\begin{document}
\maketitle

\section{第一节}
在这里开始写。
\end{document}
"""


def create_book(name):
    """在项目根下新建书稿目录 <name>/main.tex（递归发现随后即扫到它）。"""
    name = (name or '').strip()
    if not name:
        return {'ok': False, 'error': '书稿名不能为空'}
    if any(ch in name for ch in '\\/:*?"<>|') or name[0] == '.':
        return {'ok': False, 'error': r'名字里不能有 \ / : * ? " < > | 或以 . 开头'}
    if any(ord(ch) < 32 for ch in name):
        return {'ok': False, 'error': '名字里有不可见控制字符，请删掉重输'}
    if name.upper().split('.')[0] in {'CON', 'PRN', 'AUX', 'NUL'} or \
       (len(name) >= 4 and name[:3].upper() in {'COM', 'LPT'} and name[3].isdigit()):
        return {'ok': False, 'error': '这是 Windows 保留设备名，换一个名字'}
    if name[-1] in '. ':
        return {'ok': False, 'error': '名字不能以空格或点结尾'}
    if len(name) > 60:
        return {'ok': False, 'error': '名字太长（限 60 字内）'}
    d = os.path.join(config.BOOKS_ROOT, name)
    if os.path.exists(d):
        return {'ok': False, 'error': '同名书稿目录已存在'}
    try:
        os.makedirs(d)
        with open(os.path.join(d, 'main.tex'), 'w', encoding='utf-8') as f:
            f.write(NEW_BOOK_TEX % {'title': name})
    except OSError as e:
        return {'ok': False, 'error': str(e)}
    books.invalidate()
    return {'ok': True, 'name': name}


class Handler(BaseHTTPRequestHandler):
    server_version = 'FloriaLaTeX/0.7.0'

    def log_message(self, fmt, *args):
        if self.path.startswith('/api') or self.path.startswith('/img'):
            sys.stderr.write('[%s] %s\n' % (self.log_date_time_string(), fmt % args))

    # ---------- helpers ----------
    def send_json(self, obj, code=200):
        try:
            data = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        except (TypeError, ValueError):
            data = b'{"error":"serialize failed"}'
            code = 500
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def send_file(self, path):
        try:
            with open(path, 'rb') as f:
                data = f.read()
        except OSError:
            self.send_json({'error': 'not found'}, 404)
            return
        ext = os.path.splitext(path)[1].lower()
        self.send_response(200)
        self.send_header('Content-Type', config.CONTENT_TYPES.get(
            ext, 'application/octet-stream'))
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def send_redirect(self, to):
        self.send_response(302)
        self.send_header('Location', to)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def query(self):
        qs = {}
        if '?' in self.path:
            for kv in self.path.split('?', 1)[1].split('&'):
                if '=' in kv:
                    k, v = kv.split('=', 1)
                    qs[k] = urllib.parse.unquote(v)
        return qs

    def read_json(self):
        try:
            n = int(self.headers.get('Content-Length', '0'))
            return json.loads(self.rfile.read(n).decode('utf-8'))
        except (ValueError, OSError):
            return None

    def _path(self):
        return posixpath.normpath(
            urllib.parse.unquote(self.path.split('?', 1)[0])).replace('\\', '/')

    # ---------- GET ----------
    def do_GET(self):
        try:
            self._get()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as ex:                 # noqa: BLE001
            log.exception('GET %s' % self.path, ex)
            try:
                self.send_json({'ok': False, 'error': '服务器内部错误：%s' % ex}, 500)
            except Exception:                   # noqa: BLE001
                pass

    def _get(self):
        path = self._path()
        if path.startswith('/api/books'):
            return self.send_json(books.books_with_pdf())
        if path.startswith('/api/health'):
            qs = self.query()
            return self.send_json(doctor.run_checks(qs.get('book') or None,
                                                    refresh=qs.get('refresh') == '1'))
        if path.startswith('/api/history'):
            qs = self.query()
            b = books.find_book(qs.get('book', ''))
            if not b:
                return self.send_json({'ok': False, 'error': 'book not found'}, 404)
            rec = ledger.ledger_read(b['name'], reconcile=qs.get('reconcile') == '1',
                                     lock_probe=COMPILER.state.busy)
            rec['compile'] = COMPILER.snapshot()
            return self.send_json(rec)
        if path.startswith('/api/log'):
            qs = self.query()
            b = books.find_book(qs.get('book', ''))
            if not b:
                return self.send_json({'ok': False, 'error': 'book not found'}, 404)
            p = os.path.join(books.book_dir(b['name']), '.build', 'main.log')
            try:
                st = os.stat(p)
                with open(p, 'r', encoding='utf-8', errors='replace') as f:
                    text = f.read()
            except OSError:
                return self.send_json({'ok': True, 'book': b['name'], 'exists': False,
                                       'text': '', 'mtime': None, 'size': 0})
            return self.send_json({'ok': True, 'book': b['name'], 'exists': True,
                                   'text': text[-2000000:],
                                   'mtime': int(st.st_mtime * 1000), 'size': st.st_size})
        if path.startswith('/api/read'):
            qs = self.query()
            b = books.find_book(qs.get('book', ''))
            if not b:
                return self.send_json({'error': 'book not found'}, 404)
            p = os.path.join(config.BOOKS_ROOT, b['name'], os.path.basename(b['texName']))
            try:
                with open(p, 'r', encoding='utf-8', errors='replace') as f:
                    text = f.read()
            except OSError as e:
                return self.send_json({'error': str(e)}, 500)
            return self.send_json({'texName': b['texName'], 'text': text, 'images': b['images'],
                                   'pdf': books.book_pdf_info(b)})
        if path.startswith('/img/'):
            rel = path[len('/img/'):]
            if '/' not in rel:
                return self.send_json({'error': 'bad path'}, 400)
            book, fname = rel.rsplit('/', 1)
            b = books.find_book(book)
            if not b:
                return self.send_json({'error': 'book not found'}, 404)
            ext = os.path.splitext(fname)[1].lower()
            if ext not in config.IMG_EXT:
                return self.send_json({'error': 'bad extension'}, 400)
            p = os.path.join(config.BOOKS_ROOT, b['name'], os.path.basename(fname))
            if not os.path.isfile(p):
                return self.send_json({'error': 'not found'}, 404)
            return self.send_file(p)
        if path.startswith('/api/sync'):
            qs = self.query()
            b = books.find_book(qs.get('book', ''))
            if not b:
                return self.send_json({'ok': False, 'error': 'book not found'}, 404)
            try:
                page = int(float(qs.get('page', '1')))
                x, y = float(qs.get('x', '0')), float(qs.get('y', '0'))
            except ValueError:
                return self.send_json({'ok': False, 'error': '坐标参数不对'}, 400)
            r = synctex.lookup(b, page, x, y)
            if 'error' in r:
                return self.send_json({'ok': False, 'error': r['error']})
            return self.send_json({'ok': True, 'line': r['line'], 'page': r['page'],
                                   'texName': r.get('file') or b['texName']})
        if path.startswith('/api/pdf'):
            qs = self.query()
            b = books.find_book(qs.get('book', ''))
            if not b:
                return self.send_json({'error': 'book not found'}, 404)
            pdf_p = books.book_pdf_path(b['name'])
            if not os.path.isfile(pdf_p):
                return self.send_json({'error': '尚未编译'}, 404)
            return self.send_file(pdf_p)
        if path in ('/', '/index.html'):
            return self.send_redirect('pdf/index.html')
        # 静态资源：只放行**卡片目录**（第一段目录下必须有 card.json）
        rel = path.lstrip('/')
        parts = rel.split('/')
        seg = parts[0]
        # 后端随 pdf 卡一起存放（pdf/backend/），绝不作为静态资源对外。
        if any(p in {'backend', '_backend', 'runtime', 'vendor', 'tex', '__pycache__'}
               for p in parts[1:]):
            return self.send_json({'error': 'not found'}, 404)
        card_dir = os.path.join(config.CARD_ROOT, seg)
        if not seg or seg.startswith(('.', '_')) or not os.path.isfile(
                os.path.join(card_dir, 'card.json')):
            return self.send_json({'error': 'not found'}, 404)
        full = os.path.normpath(os.path.join(config.CARD_ROOT, rel))
        if not full.startswith(card_dir + os.sep) or not os.path.isfile(full):
            return self.send_json({'error': 'not found'}, 404)
        return self.send_file(full)

    # ---------- POST ----------
    def do_POST(self):
        try:
            self._post()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as ex:                 # noqa: BLE001
            log.exception('POST %s' % self.path, ex)
            try:
                self.send_json({'ok': False, 'error': '服务器内部错误：%s' % ex}, 500)
            except Exception:                   # noqa: BLE001
                pass

    def _post(self):
        path = posixpath.normpath(urllib.parse.unquote(self.path.split('?', 1)[0]))
        if path == '/api/newbook':
            body = self.read_json()
            if body is None:
                return self.send_json({'error': 'bad request'}, 400)
            return self.send_json(create_book(str(body.get('name', ''))))
        if path == '/api/compile':
            body = self.read_json()
            if body is None:
                return self.send_json({'error': 'bad request'}, 400)
            b = books.find_book(str(body.get('book', '')))
            if not b:
                return self.send_json({'error': 'book not found'}, 404)
            r = COMPILER.compile(b['name'], origin='page')
            if r.get('busy'):
                return self.send_json(r, 409)
            return self.send_json(r)
        if path != '/api/write':
            return self.send_json({'error': 'unknown endpoint'}, 404)
        body = self.read_json()
        if body is None:
            return self.send_json({'error': 'bad request'}, 400)
        b = books.find_book(str(body.get('book', '')))
        if not b:
            return self.send_json({'error': 'book not found'}, 404)
        fname = os.path.basename(str(body.get('file', '')))
        if not fname.lower().endswith('.tex'):
            return self.send_json({'error': 'only .tex writable'}, 400)
        p = os.path.join(config.BOOKS_ROOT, b['name'], fname)
        if not os.path.isfile(p):
            return self.send_json({'error': 'refusing to create new file'}, 403)
        try:
            with open(p, 'w', encoding='utf-8', newline='') as f:
                f.write(str(body.get('content', '')))
        except OSError as e:
            return self.send_json({'error': str(e)}, 500)
        return self.send_json({'ok': True, 'file': fname})
