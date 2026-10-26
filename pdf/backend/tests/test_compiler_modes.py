# -*- coding: utf-8 -*-
"""compiler 的模式选择契约：

- fastpath failed      → 直接报错（不调用 latexmk）
- fastpath unsupported → 明确执行整本模式，并保留 reason_code
- fastpath 抛异常      → 记为 fastpath_exception，同样不回落

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
def fake_toolchain():
    with mock.patch.object(toolchain, 'find_tex_tool',
                           side_effect=lambda name: '/fake/' + name), \
         mock.patch.object(toolchain, 'find_perl', return_value='/fake/perl'), \
         mock.patch.object(toolchain, 'sanitized_env', return_value={}), \
         mock.patch.object(toolchain, 'hoist_outputs'):
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

    def test_failed_does_not_fall_back(self):
        failed = {'status': fastpath.STATUS_FAILED, 'reason_code': 'synctex_merge_failed',
                  'reason': 'synctex 合并失败：boom', 'log': '[synctex 合并失败] boom'}
        with fake_toolchain(), \
             mock.patch.object(fastpath, 'try_compile', return_value=failed), \
             mock.patch.object(toolchain, 'run_tool') as run, \
             mock.patch('floria.compiler._ledger') as led:
            r = self.svc.compile('b')
        run.assert_not_called()
        self.assertFalse(r['ok'])
        self.assertEqual(r['mode'], 'fast')
        self.assertEqual(r['reason_code'], 'synctex_merge_failed')
        self.assertIn('快速编译失败', r['error'])
        led.assert_called_once()
        self.assertFalse(led.call_args.kwargs['ok'])
        self.assertEqual(led.call_args.kwargs['mode'], 'fast')
        self.assertEqual(led.call_args.kwargs['reason_code'], 'synctex_merge_failed')

    def test_unsupported_selects_full_mode(self):
        unsupported = {'status': fastpath.STATUS_UNSUPPORTED,
                       'reason_code': 'single_chapter', 'reason': '顶层 \\chapter 少于 2 个'}
        with fake_toolchain(), \
             mock.patch.object(fastpath, 'try_compile', return_value=unsupported), \
             mock.patch.object(toolchain, 'run_tool', return_value=(1, '! boom')) as run, \
             mock.patch('floria.compiler._ledger') as led:
            r = self.svc.compile('b')
        run.assert_called_once()
        self.assertFalse(r['ok'])                 # 整本这次失败，但模式选择正确
        self.assertEqual(r['mode'], 'full')
        self.assertEqual(r['reason_code'], 'single_chapter')
        self.assertEqual(led.call_args.kwargs['mode'], 'full')
        self.assertEqual(led.call_args.kwargs['reason_code'], 'single_chapter')

    def test_fastpath_exception_does_not_fall_back(self):
        with fake_toolchain(), \
             mock.patch.object(fastpath, 'try_compile', side_effect=RuntimeError('boom')), \
             mock.patch.object(toolchain, 'run_tool') as run, \
             mock.patch('floria.compiler._ledger') as led:
            r = self.svc.compile('b')
        run.assert_not_called()
        self.assertFalse(r['ok'])
        self.assertEqual(r['reason_code'], 'fastpath_exception')
        self.assertEqual(led.call_args.kwargs['reason_code'], 'fastpath_exception')


if __name__ == '__main__':
    unittest.main()
