# -*- coding: utf-8 -*-
"""快速路径的文档结构判定：单章 / 双章 / appendix / part / 误判防护。

不依赖 TeX，只验证 _split_document 的 (doc, reason_code) 结果。
运行：cd pdf/backend && python -m unittest discover -s tests -t . -v
"""
import os
import tempfile
import unittest

from floria import fastpath

PREAMBLE = r'\documentclass[UTF8]{ctexbook}'


def make_book(root, name, body, preamble=PREAMBLE, begin=True):
    d = os.path.join(root, name)
    os.makedirs(d, exist_ok=True)
    text = preamble + '\n'
    if begin:
        text += '\\begin{document}\n' + body + '\n\\end{document}\n'
    else:
        text += body + '\n'
    with open(os.path.join(d, 'main.tex'), 'w', encoding='utf-8') as f:
        f.write(text)
    return d


class SplitModesTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def split(self, body, **kw):
        return fastpath._split_document(make_book(self.root, 'b', body, **kw))

    def test_single_chapter(self):
        doc, code = self.split(r'\chapter{唯一}\n正文')
        self.assertIsNone(doc)
        self.assertEqual(code, 'single_chapter')

    def test_two_chapters(self):
        doc, code = self.split(r'\chapter{甲}\n甲文\n\chapter{乙}\n乙文')
        self.assertIsNone(code)
        self.assertIsNotNone(doc)
        self.assertEqual([c['id'] for c in doc['chunks']], ['ch1', 'ch2'])

    def test_appendix(self):
        doc, code = self.split(r'\chapter{甲}\n甲\n\chapter{乙}\n乙\n\appendix\n\chapter{附}')
        self.assertIsNone(doc)
        self.assertEqual(code, 'has_appendix')

    def test_part(self):
        doc, code = self.split(r'\part{上卷}\n\chapter{甲}\n甲\n\chapter{乙}\n乙')
        self.assertIsNone(doc)
        self.assertEqual(code, 'has_part')

    def test_partial_is_not_part(self):
        """\\partial 以 \\part 开头，不能误判。"""
        doc, code = self.split(r'\chapter{甲}\n$\partial_x f$\n\chapter{乙}\n乙')
        self.assertIsNone(code)
        self.assertIsNotNone(doc)

    def test_commented_chapter_ignored(self):
        doc, code = self.split(r'\chapter{甲}\n甲\n% \chapter{乙}\n乙')
        self.assertIsNone(doc)
        self.assertEqual(code, 'single_chapter')

    def test_no_main_tex(self):
        d = os.path.join(self.root, 'empty')
        os.makedirs(d, exist_ok=True)
        doc, code = fastpath._split_document(d)
        self.assertIsNone(doc)
        self.assertEqual(code, 'no_main_tex')

    def test_front_hash_tracks_preamble(self):
        """前置页由导言区排版：改导言区也要判定前置页过期。"""
        body = r'\chapter{甲}\n甲文\n\chapter{乙}\n乙文'
        a, _ = self.split(body)
        b, _ = self.split(body, preamble=PREAMBLE + '\n\\raggedbottom')
        self.assertNotEqual(fastpath._front_hash(a), fastpath._front_hash(b))
        # 只改正文（导言区/前置页不变）→ 前置页指纹不变
        c, _ = self.split(body + '\n补一句')
        self.assertEqual(fastpath._front_hash(a), fastpath._front_hash(c))

    def test_no_begin_document(self):
        doc, code = self.split(r'\chapter{甲}', begin=False)
        self.assertIsNone(doc)
        self.assertEqual(code, 'no_begin_document')


class ResultShapeTest(unittest.TestCase):
    """_skip/_fail 的结构化返回契约。"""

    def test_skip_is_unsupported(self):
        r = fastpath._skip('single_chapter', '单章')
        self.assertEqual(r['status'], fastpath.STATUS_UNSUPPORTED)
        self.assertEqual(r['reason_code'], 'single_chapter')
        self.assertEqual(r['reason'], '单章')

    def test_fail_is_failed(self):
        r = fastpath._fail('merge_pdf_failed', '拼 PDF 失败')
        self.assertEqual(r['status'], fastpath.STATUS_FAILED)
        self.assertEqual(r['reason_code'], 'merge_pdf_failed')


if __name__ == '__main__':
    unittest.main()
