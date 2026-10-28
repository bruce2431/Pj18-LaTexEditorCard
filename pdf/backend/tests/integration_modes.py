# -*- coding: utf-8 -*-
"""真实 TeX 的集成回归（临时书稿，不碰用户书稿）。

2026-10-27 起**只有快速路径**（整本 latexmk 全量兜底已删除）：
  - 单章 / 无章 / \\part 书稿都按块编译（不再「因为结构不适用而整本」）
  - 冷启动没有 main.pdf 也能直接分块编译
  - 新增一章：新块编译 + 页码/目录重排，书签按目录条目重建
  - 改前置页正文：前置页单独重编，main.pdf 里的新文字可见
  - synctex 合并失败：快速路径直接失败，且 main.pdf 不被替换
  - \\appendix：明确报错（不能按章拼接）
  - AI/main.tex 历史上的 `Missing $ inserted` 回归

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

TEX_ZERO = r"""\documentclass[UTF8]{ctexbook}
\begin{document}
\section{引言}
没有章的小稿。
\end{document}
"""

TEX_ZERO = r"""\documentclass[UTF8]{ctexbook}
\begin{document}
\section{引言}
没有章的小稿。
\end{document}
"""

# ctexart：没有 \chapter / \part 计数器（MARXISM 就是这类书稿）
TEX_ARTICLE = r"""\documentclass[UTF8]{ctexart}
\begin{document}
\section{一}
第一节正文。
\section{二}
第二节正文。
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


def _edit(path, old, new):
    with open(path, encoding='utf-8') as f:
        text = f.read()
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text.replace(old, new))


def _temp_root():
    ws = os.path.dirname(PROJECT)
    cand = os.path.join(ws, '.claude', 'temp')
    try:
        os.makedirs(cand, exist_ok=True)
    except OSError:
        cand = tempfile.gettempdir()
    return tempfile.mkdtemp(prefix='pj18-integration-', dir=cand)


def _toc_pages(pdf_p):
    """书签树 → [(标题, 页码, 子书签)]（页码 1 基，pypdf 的 outline 是嵌套 list）。"""
    from pypdf import PdfReader
    r = PdfReader(pdf_p)

    def walk(nodes):
        out, i = [], 0
        while i < len(nodes):
            it = nodes[i]
            if isinstance(it, list):
                i += 1
                continue
            kids = []
            if i + 1 < len(nodes) and isinstance(nodes[i + 1], list):
                kids = walk(nodes[i + 1])
                i += 1
            try:
                out.append((str(it.title), r.get_destination_page_number(it) + 1, kids))
            except Exception:                   # noqa: BLE001
                pass
            i += 1
        return out

    return walk(r.outline)


class IntegrationModesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = _temp_root()
        cls.books_root = os.path.join(cls._tmp, 'books')
        os.makedirs(cls.books_root)

        env = toolchain.sanitized_env()
        # **不要把 MiKTeX bin 加进 PATH**：2026-10-26 实测 PATH 里带上自带 MiKTeX bin
        # 后 XeTeX 的字体查找会失败（SimSun cannot be found / 返回码 12）。
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

    def test_01_single_chapter_is_fast(self):
        d = _write_book(self.books_root, 'single', TEX_SINGLE)
        r = self._compile('single')
        self.assertTrue(r['ok'], r.get('error') or r.get('log'))
        self.assertEqual(r['mode'], 'fast')
        self.assertIsNone(r['reason_code'])
        self.assertTrue(os.path.isfile(os.path.join(d, 'main.pdf')))
        self.assertEqual((r.get('chunks') or {}).get('total'), 1)

    def test_02_zero_chapter_is_fast(self):
        _write_book(self.books_root, 'zero', TEX_ZERO)
        r = self._compile('zero')
        self.assertTrue(r['ok'], r.get('error') or r.get('log'))
        self.assertEqual(r['mode'], 'fast')
        self.assertEqual((r.get('chunks') or {}).get('total'), 1)

    def test_03_appendix_reports_error(self):
        _write_book(self.books_root, 'appendix', TEX_APPENDIX)
        r = self._compile('appendix')
        self.assertFalse(r['ok'])
        self.assertEqual(r['mode'], 'fast')
        self.assertEqual(r['reason_code'], 'has_appendix')

    def test_04_part_is_fast(self):
        d = _write_book(self.books_root, 'part', TEX_PART)
        r = self._compile('part')
        self.assertTrue(r['ok'], r.get('error') or r.get('log'))
        self.assertEqual(r['mode'], 'fast')
        self.assertEqual((r.get('chunks') or {}).get('total'), 3)
        pages = _toc_pages(os.path.join(d, 'main.pdf'))
        # \part 是顶层书签，chapter 嵌在它下面
        self.assertEqual(len(pages), 1)
        self.assertIn('上卷', pages[0][0])
        self.assertEqual(len(pages[0][2]), 2, '两章应嵌在分卷书签下')

    def test_05_dual_cold_start_then_add_chapter(self):
        d = _write_book(self.books_root, 'dual', TEX_DUAL)
        first = self._compile('dual')
        self.assertTrue(first['ok'], first.get('error') or first.get('log'))
        self.assertEqual(first['mode'], 'fast', '冷启动不再需要整本先编一次')
        self.assertEqual((first.get('chunks') or {}).get('total'), 2)

        # TOC 页码与实际页对得上（书签由分块 toc 合成）
        pages = _toc_pages(os.path.join(d, 'main.pdf'))
        self.assertEqual(len(pages), 2)
        self.assertIn('甲', pages[0][0])
        self.assertIn('乙', pages[1][0])
        cache = fastpath.read_cache(d) or {}
        starts = {c['id']: c['start'] for c in cache.get('chunks') or []}
        self.assertEqual(pages[0][1], starts['ch1'])
        self.assertEqual(pages[1][1], starts['ch2'])

        # 新增第三章：只有新块需要新编，书签随之变成三项
        _edit(os.path.join(d, 'main.tex'), '乙文', '乙文\n\\chapter{丙}\n丙文')
        second = self._compile('dual')
        self.assertTrue(second['ok'], second.get('error') or second.get('log'))
        self.assertEqual(second['mode'], 'fast')
        self.assertEqual((second.get('chunks') or {}).get('total'), 3)
        titles = [t for t, _, _ in _toc_pages(os.path.join(d, 'main.pdf'))]
        self.assertEqual(len(titles), 3)
        self.assertIn('丙', titles[2])
        self.assertEqual((second.get('chunks') or {}).get('rebuilt'), 1,
                         '新增一章应只编新块（后面的块页码未变就不用重编）')

    def test_06_synctex_failure_keeps_pdf(self):
        dual = os.path.join(self.books_root, 'dual')
        pdf_p = os.path.join(dual, 'main.pdf')
        before = _digest(pdf_p)

        _edit(os.path.join(dual, 'main.tex'), '甲文', '甲文（改）')
        with mock.patch.object(fastpath, '_merge_synctex',
                               side_effect=RuntimeError('boom-sync')):
            r = self._compile('dual')
        self.assertFalse(r['ok'])
        self.assertEqual(r['mode'], 'fast')
        self.assertEqual(r['reason_code'], 'synctex_merge_failed')
        self.assertEqual(_digest(pdf_p), before,
                         'synctex 合并失败时 main.pdf 不应被替换')

    def test_07_front_edit_rebuilds_front_pages(self):
        """改前置页正文：前置页单独重编，成品里必须是新文字（不再是旧的封面/目录）。"""
        d = _write_book(self.books_root, 'front', TEX_FRONT)
        first = self._compile('front')
        self.assertTrue(first['ok'], first.get('error') or first.get('log'))
        self.assertEqual(first['mode'], 'fast')

        _edit(os.path.join(d, 'main.tex'), 'FRONTEDIT-A', 'FRONTEDIT-B')
        second = self._compile('front')
        self.assertTrue(second['ok'], second.get('error') or second.get('log'))
        self.assertEqual(second['mode'], 'fast')

        from pypdf import PdfReader
        r = PdfReader(os.path.join(d, 'main.pdf'))
        front_txt = '\n'.join((r.pages[i].extract_text() or '')
                              for i in range(min(4, len(r.pages))))
        self.assertIn('FRONTEDIT-B', front_txt)
        self.assertNotIn('FRONTEDIT-A', front_txt)

        third = self._compile('front')
        self.assertTrue(third.get('noop'), '源码没变应直接复用成品（0 秒）')

    def test_08_single_edit_reuses_other_chunks(self):
        """改一章只重编那一块；缓存里每块都有 pages/built。"""
        d = _write_book(self.books_root, 'reuse', TEX_DUAL)
        first = self._compile('reuse')
        self.assertTrue(first['ok'], first.get('error') or first.get('log'))

        cache = fastpath.read_cache(d) or {}
        self.assertIsNotNone(cache.get('front'))
        self.assertTrue(all(isinstance(c.get('pages'), int) and c.get('built')
                            for c in cache.get('chunks') or []),
                        '缓存应含每块的 pages/built：%r' % (cache,))

        _edit(os.path.join(d, 'main.tex'), '乙文', '乙文（改）')
        r = self._compile('reuse')
        self.assertTrue(r['ok'], r.get('error') or r.get('log'))
        chunks = r.get('chunks') or {}
        self.assertEqual(chunks.get('rebuilt'), 1, '只应重编被改的那一块：%r' % (chunks,))
        self.assertEqual(chunks.get('reused'), 1)

        again = self._compile('reuse')
        self.assertTrue(again.get('noop'), '源码没变应直接复用成品（0 秒）')

    def test_09_ai_regression(self):
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

    def test_10_article_class_has_no_chapter_counter(self):
        # ctexart 没有 \chapter 计数器：wrapper 无条件 \setcounter{chapter} 会在
        # -halt-on-error 下直接失败（"No counter 'chapter' defined"），MARXISM 实测。
        _write_book(self.books_root, 'article', TEX_ARTICLE)
        r = self._compile('article')
        self.assertTrue(r['ok'], r.get('error') or r.get('log'))
        self.assertEqual(r['mode'], 'fast')
        self.assertNotIn("No counter 'chapter' defined", r.get('log') or '')


if __name__ == '__main__':
    unittest.main(verbosity=2)
