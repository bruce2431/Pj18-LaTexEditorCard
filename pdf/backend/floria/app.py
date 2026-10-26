# -*- coding: utf-8 -*-
"""进程装配：设书稿根 → 启后台自检 → 可选 watchdog → 起 HTTP 服务。"""
import os
import sys
import threading
import webbrowser
from http.server import ThreadingHTTPServer

from . import config, doctor, log
from .http_api import COMPILER, Handler


def _self_check_async(book=None):
    def run():
        try:
            rep = doctor.run_checks(book)
            bad = [c for c in rep['checks'] if c['status'] != 'ok']
            log.info('自检 %s（%d 项，异常 %d）：%s'
                     % (rep['level'], len(rep['checks']), len(bad),
                        '; '.join('%s=%s %s' % (c['id'], c['status'], c['detail'])
                                  for c in bad) or '全部正常'))
        except Exception as ex:                 # noqa: BLE001
            log.exception('启动自检', ex)
    threading.Thread(target=run, daemon=True, name='floria-selfcheck').start()


def run(port=8765, root=None, no_open=False, auto_restart=False):
    if root:
        config.set_books_root(root)
    if not config.BOOKS_ROOT:
        config.set_books_root(os.path.dirname(os.path.dirname(config.CARD_ROOT)))

    srv = None
    for p in range(port, port + 10):
        try:
            srv = ThreadingHTTPServer(('127.0.0.1', p), Handler)
            break
        except OSError:
            continue
    if not srv:
        print('端口 %d-%d 都被占用，无法启动。' % (port, port + 9))
        return 1

    url = 'http://127.0.0.1:%d/' % srv.server_address[1]
    print('Floria LaTeX 本地服务器已启动：' + url)
    print('书稿根目录：' + config.BOOKS_ROOT)
    print('（Ctrl+C 退出）')

    wd = None
    if auto_restart:
        from .watchdog import Watchdog
        wd = Watchdog(COMPILER.state)
        wd.start()
    _self_check_async()
    if not no_open:
        threading.Timer(0.6, webbrowser.open, [url]).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print('\n已退出。')
    finally:
        if wd:
            wd.stop()
        srv.server_close()
    return 0


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=8765)
    ap.add_argument('--root', default=None)
    ap.add_argument('--no-open', action='store_true')
    ap.add_argument('--auto-restart', action='store_true')
    ap.add_argument('--doctor', action='store_true')
    args = ap.parse_args(argv)
    if args.doctor:
        import json
        from . import config as cfg
        if args.root:
            cfg.set_books_root(args.root)
        print(json.dumps(doctor.run_checks(), ensure_ascii=False, indent=2))
        return 0
    return run(args.port, args.root, args.no_open, args.auto_restart)
