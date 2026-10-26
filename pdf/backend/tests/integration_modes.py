# -*- coding: utf-8 -*-
"""真实 TeX 的模式选择集成回归（临时书稿，不碰用户书稿）。

覆盖：
  - 单章 / 双章 / \\appendix / \\part 书稿的模式选择与原因码
  - 双章首次整本（no_main_pdf）→ 第二次快速
  - synctex 合并失败：快速路径直接失败，且 main.pdf 不被替换
  - AI/main.tex 历史上的 `Missing $ inserted` 回归（源文件现在可编译）

运行：cd pdf/backend && runtime/python.exe -B tests/integration_modes.py
只写 <工作区>/.claude/temp/ 下的临时目录。
"""
import hashlib
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.dirname(HERE)                       # pdf/backend
PROJECT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(HERE)))))                         # 项目根（tests→backend→pdf→preview→.claude→项目）
VENDOR = os.path.join(APP_DIR, 'vendor')

for p in (APP_DIR, VENDOR):
    if p not in sys.path:
        sys.path.insert(0, p)

from floria import books, compiler, config, fastpath, toolchain  # noqa: E402

TEX_SINGLE = r"""\documentclass[UTF8]{ctexbook}
\begin{document}
\chapter{唯一}
正文
\end{document}
"""

TEX_DUAL = r"""\documentclass[UTF8]{ctexbook}
\usepackage{hyperref}
\begin{document}
\chapter{甲}
甲文
\chapter{乙}
乙文
\end{document}
"""

TEX_APPENDIX = r"""\documentclass[UTF8]{ctexbook}
\begin{document}
\chapter{甲}
甲文
\chapter{乙}
乙文
\appendix
\chapter{附录}
附录文
\end{document}
"""

TEX_PART = r"""\documentclass[UTF8]{ctexbook}
\begin{document}
\part{上卷}
\chapter{甲}
甲文
\chapter{乙}
乙文
\end{document}
"""


TEX_FRONT = r"""\documentclass[UTF8]{ctexbook}
\usepackage{hyperref}
\begin{document}
\tableofcontents
\newpage
FRONTEDIT-A
\chapter{甲}
甲文
\chapter{乙}
乙文
\end{document}
"""


def _digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def _write_book(root, name, text):
    d = os.path.join(root, name)
    os.makedirs(d)
    with open(os.path.join(d, 'main.tex'), 'w', encoding='utf-8') as f:
        f.write(text)
    return d


def _temp_root():
    ws = os.path.dirname(PROJECT)
    cand = os.path.join(ws, '.claude', 'temp')
    try:
        os.makedirs(cand, exist_ok=True)
    except OSError:
        cand = tempfile.gettempdir()
    return tempfile.mkdtemp(prefix='pj18-integration-', dir=cand)


class IntegrationModesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = _temp_root()
        cls.books_root = os.path.join(cls._tmp, 'books')
        os.makedirs(cls.books_root)

        perl = toolchain.find_perl()
        env = toolchain.sanitized_env()
        # 只前置 perl（latexmk 需要）。**不要把 MiKTeX bin 加进 PATH**：
        # 2026-10-26 实测，PATH 里带上自带 MiKTeX bin 后 XeTeX 的字体查找会失败
        # （SimSun cannot be found / 返回码 12），而 latexmk 本身用绝对路径调用、无需在 PATH。
        if perl:
            env['PATH'] = os.path.dirname(perl) + os.pathsep + env.get('PATH', '')
        cls._env_patch = mock.patch.object(
            toolchain, 'sanitized_env', side_effect=lambda: dict(env))
        cls._env_patch.start()

        cls._old_root = config.BOOKS_ROOT
        config.set_books_root(cls.books_root)
        books.invalidate()
        cls.svc = compiler.CompilerService()

    @classmethod
    def tearDownClass(cls):
        cls._env_patch.stop()
        config.set_books_root(cls._old_root or cls.books_root)
        books.invalidate()
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def _compile(self, name):
        books.invalidate()
        t0 = time.time()
        r = self.svc.compile(name)
        print('      [%s] ok=%s mode=%s reason_code=%s %.1fs'
              % (name, r.get('ok'), r.get('mode'), r.get('reason_code'),
                 time.time() - t0))
        return r

    def test_01_single_chapter_full(self):
        _write_book(self.books_root, 'single', TEX_SINGLE)
        r = self._compile('single')
        self.assertTrue(r['ok'], r.get('error') or r.get('log'))
        self.assertEqual(r['mode'], 'full')
        self.assertEqual(r['reason_code'], 'single_chapter')

    def test_02_appendix_full(self):
        _write_book(self.books_root, 'appendix', TEX_APPENDIX)
        r = self._compile('appendix')
        self.assertTrue(r['ok'], r.get('error') or r.get('log'))
        self.assertEqual(r['mode'], 'full')
        self.assertEqual(r['reason_code'], 'has_appendix')

    def test_03_part_full(self):
        _write_book(self.books_root, 'part', TEX_PART)
        r = self._compile('part')
        self.assertTrue(r['ok'], r.get('error') or r.get('log'))
        self.assertEqual(r['mode'], 'full')
        self.assertEqual(r['reason_code'], 'has_part')

    def test_04_two_chapters_full_then_fast(self):
        _write_book(self.books_root, 'dual', TEX_DUAL)
        first = self._compile('dual')
        self.assertTrue(first['ok'], first.get('error') or first.get('log'))
        self.assertEqual(first['mode'], 'full')
        self.assertEqual(first['reason_code'], 'no_main_pdf')

        second = self._compile('dual')
        self.assertTrue(second['ok'], second.get('error') or second.get('log'))
        self.assertEqual(second['mode'], 'fast')
        self.assertIsNone(second['reason_code'])

    def test_05_synctex_failure_keeps_pdf(self):
        dual = os.path.join(self.books_root, 'dual')
        pdf_p = os.path.join(dual, 'main.pdf')
        before = _digest(pdf_p)

        tex_p = os.path.join(dual, 'main.tex')
        with open(tex_p, encoding='utf-8') as f:
            text = f.read()
        with open(tex_p, 'w', encoding='utf-8') as f:
            f.write(text.replace('甲文', '甲文（改）'))

        with mock.patch.object(fastpath, '_merge_synctex',
                               side_effect=RuntimeError('boom-sync')):
            r = self._compile('dual')
        self.assertFalse(r['ok'])
        self.assertEqual(r['mode'], 'fast')
        self.assertEqual(r['reason_code'], 'synctex_merge_failed')
        self.assertEqual(_digest(pdf_p), before,
                         'synctex 合并失败时 main.pdf 不应被替换')

    def test_07_front_edit_rebuilds_front_pages(self):
        """回归 2026-10-26：改前置页正文后，快速路径不能沿用旧 main.pdf 的封面/目录。"""
        d = _write_book(self.books_root, 'front', TEX_FRONT)
        first = self._compile('front')
        self.assertTrue(first['ok'], first.get('error') or first.get('log'))
        self.assertEqual(first['mode'], 'full')

        second = self._compile('front')
        self.assertTrue(second['ok'], second.get('error') or second.get('log'))
        self.assertEqual(second['mode'], 'fast')

        tex_p = os.path.join(d, 'main.tex')
        with open(tex_p, encoding='utf-8') as f:
            text = f.read()
        with open(tex_p, 'w', encoding='utf-8') as f:
            f.write(text.replace('FRONTEDIT-A', 'FRONTEDIT-B'))

        third = self._compile('front')
        self.assertTrue(third['ok'], third.get('error') or third.get('log'))
        self.assertEqual(third['mode'], 'full', '前置页源码改过必须整本重编')
        self.assertEqual(third['reason_code'], 'front_baseline_missing')

        from pypdf import PdfReader
        r = PdfReader(os.path.join(d, 'main.pdf'))
        front_txt = '\n'.join((r.pages[i].extract_text() or '')
                              for i in range(min(4, len(r.pages))))
        self.assertIn('FRONTEDIT-B', front_txt)
        self.assertNotIn('FRONTEDIT-A', front_txt)

        fourth = self._compile('front')
        self.assertTrue(fourth['ok'], fourth.get('error') or fourth.get('log'))
        self.assertEqual(fourth['mode'], 'fast')

    def test_06_ai_regression(self):
        src = os.path.join(PROJECT, 'AI', 'main.tex')
        if not os.path.isfile(src):
            self.skipTest('AI/main.tex 不存在')
        d = os.path.join(self.books_root, 'ai')
        os.makedirs(d)
        with open(src, encoding='utf-8') as f:
            text = f.read()
        with open(os.path.join(d, 'main.tex'), 'w', encoding='utf-8') as f:
            f.write(text)
        img = os.path.join(PROJECT, 'AI', 'image')
        if os.path.isdir(img):
            shutil.copytree(img, os.path.join(d, 'image'))
        r = self._compile('ai')
        self.assertTrue(r['ok'], r.get('error') or r.get('log'))
        log = r.get('log') or ''
        self.assertNotIn('Missing $ inserted', log)
        self.assertNotIn(':error:', log)


if __name__ == '__main__':
    unittest.main(verbosity=2)
