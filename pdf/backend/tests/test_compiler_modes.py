# -*- coding: utf-8 -*-
"""compiler 的唯一编译路径契约（2026-10-27 起没有整本 latexmk 兜底）：

- fastpath 成功      → 账本 ok / mode=fast，写 last_ok
- fastpath failed    → 直接报错，原因码原样写账本，不再调用任何其它工具
- fastpath 抛异常    → 记为 fastpath_exception
- 没有 xelatex       → no_toolchain，不进入编译

不依赖 TeX（toolchain 全部 mock）。
运行：cd pdf/backend && python -m unittest discover -s tests -t . -v
"""
import os
import tempfile
import unittest
from contextlib import contextmanager
from unittest import mock

from floria import books, compiler, config, fastpath, toolchain

TEX = r"""\documentclass[UTF8]{ctexbook}
\begin{document}
\chapter{甲}
甲
\chapter{乙}
乙
\end{document}
"""


@contextmanager
def fake_toolchain(tex='/fake/xelatex'):
    with mock.patch.object(toolchain, 'find_tex_tool',
                           side_effect=lambda name: '/fake/' + name if tex else None), \
         mock.patch.object(toolchain, 'sanitized_env', return_value={}), \
         mock.patch('shutil.which', return_value=None):
        yield


class CompilerModeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self._old_root = config.BOOKS_ROOT
        d = os.path.join(self.root, 'b')
        os.makedirs(d)
        with open(os.path.join(d, 'main.tex'), 'w', encoding='utf-8') as f:
            f.write(TEX)
        config.set_books_root(self.root)
        books.invalidate()
        self.svc = compiler.CompilerService()

    def tearDown(self):
        books.invalidate()
        config.set_books_root(self._old_root or self.root)
        self._tmp.cleanup()

    def test_failed_reports_and_never_retries(self):
        failed = {'status': fastpath.STATUS_FAILED, 'reason_code': 'synctex_merge_failed',
                  'reason': 'synctex 合并失败：boom', 'log': '[synctex 合并失败] boom'}
        with fake_toolchain(), \
             mock.patch.object(fastpath, 'try_compile', return_value=failed) as tc, \
             mock.patch.object(toolchain, 'run_tool') as run, \
             mock.patch('floria.compiler._ledger') as led:
            r = self.svc.compile('b')
        tc.assert_called_once()
        run.assert_not_called()
        self.assertFalse(r['ok'])
        self.assertEqual(r['mode'], 'fast')
        self.assertEqual(r['reason_code'], 'synctex_merge_failed')
        self.assertIn('编译失败', r['error'])
        led.assert_called_once()
        self.assertFalse(led.call_args.kwargs['ok'])
        self.assertEqual(led.call_args.kwargs['mode'], 'fast')
        self.assertEqual(led.call_args.kwargs['reason_code'], 'synctex_merge_failed')

    def test_ok_writes_ledger_and_last_ok(self):
        ok = {'status': fastpath.STATUS_OK, 'reason_code': None, 'reason': None,
              'ok': True, 'time': 1.5, 'pages': 2, 'mtime': 0,
              'log': '[编译链] 自动分章并行'}
        with fake_toolchain(), \
             mock.patch.object(fastpath, 'try_compile', return_value=ok) as tc, \
             mock.patch('floria.compiler._ledger') as led, \
             mock.patch.object(fastpath, 'write_last_ok') as last_ok:
            r = self.svc.compile('b')
        tc.assert_called_once()
        self.assertTrue(r['ok'])
        self.assertEqual(r['mode'], 'fast')
        self.assertTrue(r['fast'])
        self.assertEqual(led.call_args.kwargs['mode'], 'fast')
        self.assertIsNone(led.call_args.kwargs['reason_code'])
        last_ok.assert_called_once()

    def test_fastpath_exception_reported(self):
        with fake_toolchain(), \
             mock.patch.object(fastpath, 'try_compile', side_effect=RuntimeError('boom')), \
             mock.patch.object(toolchain, 'run_tool') as run, \
             mock.patch('floria.compiler._ledger') as led:
            r = self.svc.compile('b')
        run.assert_not_called()
        self.assertFalse(r['ok'])
        self.assertEqual(r['reason_code'], 'fastpath_exception')
        self.assertEqual(led.call_args.kwargs['reason_code'], 'fastpath_exception')

    def test_no_toolchain(self):
        with fake_toolchain(tex=None), \
             mock.patch.object(fastpath, 'try_compile') as tc:
            r = self.svc.compile('b')
        tc.assert_not_called()
        self.assertFalse(r['ok'])
        self.assertEqual(r['reason_code'], 'no_toolchain')

    def test_unchanged_source_short_circuits(self):
        open(os.path.join(self.root, 'b', 'main.pdf'), 'wb').close()
        with fake_toolchain(), \
             mock.patch.object(fastpath, 'unchanged_output', return_value=3), \
             mock.patch.object(fastpath, 'try_compile') as tc, \
             mock.patch('floria.compiler._ledger'):
            r = self.svc.compile('b')
        tc.assert_not_called()
        self.assertTrue(r['ok'])
        self.assertTrue(r.get('noop'))
        self.assertEqual(r['pages'], 3)


if __name__ == '__main__':
    unittest.main()
