# -*- coding: utf-8 -*-
"""快速路径的文档结构判定：单章 / 无章 / 双章 / appendix / part / 误判防护。

2026-10-27 起整本 latexmk 兜底已删除：单章、无章、含 \part 的书稿都按块编译，
只有 \appendix（章编号改字母并重置计数器）仍然不能按章拼接。
不依赖 TeX，只验证 _split_document 与 TOC 解析。
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
        self.assertIsNone(code)
        self.assertEqual(len(doc['chunks']), 1)
        self.assertEqual(doc['chunks'][0]['text'].strip(), r'\chapter{唯一}\n正文')
        self.assertFalse(doc['chunks'][0]['starred'])
        self.assertEqual(doc['front'].strip(), '')

    def test_no_chapter(self):
        """无 \chapter 的短稿整篇正文一块。"""
        doc, code = self.split(r'\section{引言}\n正文')
        self.assertIsNone(code)
        self.assertEqual(len(doc['chunks']), 1)
        self.assertTrue(doc['chunks'][0]['starred'])
        self.assertEqual(doc['front'].strip(), '')

    def test_two_chapters(self):
        doc, code = self.split(r'\chapter{甲}\n甲文\n\chapter{乙}\n乙文')
        self.assertIsNone(code)
        self.assertEqual([c['id'] for c in doc['chunks']], ['ch1', 'ch2'])
        self.assertEqual(doc['front'].strip(), '')

    def test_front_before_first_chapter(self):
        doc, code = self.split(r'前言段落\n\chapter{甲}\n甲\n\chapter{乙}\n乙')
        self.assertIsNone(code)
        self.assertEqual(doc['front'].strip(), '前言段落' + chr(92) + 'n')
        self.assertEqual(len(doc['chunks']), 2)

    def test_appendix(self):
        doc, code = self.split(r'\chapter{甲}\n甲\n\chapter{乙}\n乙\n\appendix\n\chapter{附}')
        self.assertIsNone(doc)
        self.assertEqual(code, 'has_appendix')

    def test_part_is_a_block_boundary(self):
        """\part 自成一块（导出 part 计数），不与后一章挤在一起。"""
        doc, code = self.split(r'\chapter{甲}\n甲\n\part{下卷}\n\chapter{乙}\n乙')
        self.assertIsNone(code)
        self.assertEqual(len(doc['chunks']), 3)
        self.assertEqual([c['parts'] for c in doc['chunks']], [0, 0, 1])
        self.assertEqual(doc['chunks'][1]['text'].strip(),
                         chr(92) + 'part{下卷}' + chr(92) + 'n')

    def test_partial_is_not_part(self):
        """\\partial 以 \\part 开头，不能误判。"""
        doc, code = self.split(r'\chapter{甲}\n$\partial_x f$\n\chapter{乙}\n乙')
        self.assertIsNone(code)
        self.assertIsNotNone(doc)
        self.assertEqual(len(doc['chunks']), 2)

    def test_commented_chapter_ignored(self):
        doc, code = self.split(r'\chapter{甲}\n甲\n% \chapter{乙}\n乙')
        self.assertIsNone(code)
        self.assertEqual(len(doc['chunks']), 1)

    def test_starred_chapter_not_numbered(self):
        doc, _ = self.split(r'\chapter*{前言}\n甲\n\chapter{乙}\n乙')
        self.assertTrue(doc['chunks'][0]['starred'])
        self.assertFalse(doc['chunks'][1]['starred'])

    def test_no_main_tex(self):
        d = os.path.join(self.root, 'empty')
        os.makedirs(d, exist_ok=True)
        doc, code = fastpath._split_document(d)
        self.assertIsNone(doc)
        self.assertEqual(code, 'no_main_tex')

    def test_no_begin_document(self):
        doc, code = self.split(r'\chapter{甲}', begin=False)
        self.assertIsNone(doc)
        self.assertEqual(code, 'no_begin_document')


class TocParseTest(unittest.TestCase):
    """分块编出的 wrapper.toc 决定书签树：绝对页码 + 层级嵌套。"""

    def test_parse_absolute_pages(self):
        text = (
            '\\contentsline {part}{\\numberline {上卷}上卷}{1}{part.1}%\n'
            '\\contentsline {chapter}{\\numberline {第一章\\hspace {.3em}}总论}{7}{chapter.1}%\n'
            '\\contentsline {section}{\\numberline {1.1\\hspace {.3em}}背景}{7}{section.1.1}%\n'
        )
        out = fastpath._parse_toc(text)
        self.assertEqual([e['level'] for e in out], ['part', 'chapter', 'section'])
        self.assertEqual([e['page'] for e in out], [1, 7, 7])
        self.assertEqual(out[1]['title'], '第一章 总论')
        self.assertEqual(out[2]['title'], '1.1 背景')

    def test_parse_skips_broken_entries(self):
        out = fastpath._parse_toc('\\contentsline {chapter}{无页码}\n')
        self.assertEqual(out, [])


class CacheSeedTest(unittest.TestCase):
    """冷缓存时的起始页初值：synctex / 书签页反推，估错只会多跑一轮。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.book = self._tmp.name
        with open(os.path.join(self.book, 'main.tex'), 'w', encoding='utf-8') as f:
            f.write(PREAMBLE + '\n\\begin{document}\n'
                    '\\chapter{甲}\n甲\n\\chapter{乙}\n乙\n\\end{document}\n')
        self.chunks = [{'id': 'ch1', 'line': 10}, {'id': 'ch2', 'line': 20}]

    def tearDown(self):
        self._tmp.cleanup()

    def write_synctex(self, lines):
        import gzip
        with gzip.open(os.path.join(self.book, 'main.synctex.gz'), 'wt',
                       encoding='utf-8') as f:
            f.write('\n'.join(lines) + '\n')

    def test_front_hash_tracks_toc(self):
        a = fastpath._front_hash('pre', 'front', 'TOC-A')
        self.assertEqual(a, fastpath._front_hash('pre', 'front', 'TOC-A'))
        self.assertNotEqual(a, fastpath._front_hash('pre', 'front', 'TOC-B'))
        self.assertNotEqual(a, fastpath._front_hash('pre2', 'front', 'TOC-A'))

    def test_no_synctex_no_pdf_means_no_seed(self):
        self.assertIsNone(fastpath._seed_from_synctex(self.book, self.chunks))
        self.assertIsNone(fastpath._seed_from_pdf(self.book, self.chunks))

    def test_toc_link_records_do_not_steal_chapter_start(self):
        """目录页上指向第二章的链接记录（零星一两条）不能把第二章首页判成目录页。"""
        rec = []
        rec += ['{1'] + ['h1,20:1'] * 2                  # 目录页：只有链接记录
        rec += ['{3'] + ['h1,%d:1' % ln for ln in range(10, 16)]
        rec += ['{5'] + ['h1,%d:1' % ln for ln in range(20, 26)]
        rec += ['h2,%d:1' % ln for ln in range(10, 40)]   # 宏包同号行号必须忽略
        self.write_synctex(['SyncTeX Version:1', 'Input:1:main.tex',
                            'Input:2:C:/texmf/ctexbook.cls', 'Content:'] + rec)
        front, starts = fastpath._seed_from_synctex(self.book, self.chunks)
        self.assertEqual(starts, [3, 5])
        self.assertEqual(front, 2)

    def test_even_start_snaps_back_to_odd(self):
        """openright 下章首必为奇数页：偶数结果回退到前一页。"""
        rec = ['{3', 'h1,10:1', 'h1,11:1'] + ['{4'] + ['h1,%d:1' % ln for ln in range(10, 20)]
        rec += ['{5'] + ['h1,%d:1' % ln for ln in range(20, 30)]
        self.write_synctex(['SyncTeX Version:1', 'Input:1:main.tex', 'Content:'] + rec)
        front, starts = fastpath._seed_from_synctex(self.book, self.chunks)
        self.assertEqual(starts, [3, 5])
        self.assertEqual(front, 2)


class ResultShapeTest(unittest.TestCase):
    """_fail 的结构化返回契约。"""

    def test_fail_is_failed(self):
        r = fastpath._fail('merge_pdf_failed', '拼 PDF 失败')
        self.assertEqual(r['status'], fastpath.STATUS_FAILED)
        self.assertEqual(r['reason_code'], 'merge_pdf_failed')


if __name__ == '__main__':
    unittest.main()
