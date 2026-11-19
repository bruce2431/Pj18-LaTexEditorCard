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

from floria import fastpath, pdf_stamp, tikz_cache

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


class NewpageSplitTest(unittest.TestCase):
    """章内按「`\\newpage` 后紧接标题」再切块：改一节只重编一节。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def split(self, body):
        d = make_book(self.root, 'b', body)
        return fastpath._split_document(d)[0]

    def test_split_before_section(self):
        doc = self.split(
            '\\chapter{甲}\n甲文\n\\newpage\n\\section{一}\n节文\n'
            '\\newpage\n\\subsection{小}\n小文\n')
        self.assertEqual([c['id'] for c in doc['chunks']], ['ch1', 'ch1_2', 'ch1_3'])
        self.assertTrue(doc['chunks'][0]['openright'])
        self.assertFalse(doc['chunks'][1]['openright'])
        # \newpage 留在前一块末尾：去掉它会让前面的段落重排，断行与整本不一致
        self.assertTrue(doc['chunks'][0]['text'].rstrip().endswith('\\newpage'))
        self.assertTrue(doc['chunks'][1]['text'].lstrip().startswith('\\section{一}'))
        # 子块要复刻页眉状态：章名来自 \chapter；节名 = 子块首页页眉该显示的那一节
        # （\section 开头就是它自己，\subsection 开头则沿用上一节）
        self.assertEqual(doc['chunks'][1]['marks']['chapter'], '甲')
        self.assertEqual(doc['chunks'][1]['marks']['section'], '一')
        self.assertEqual(doc['chunks'][2]['marks']['section'], '一')
        self.assertTrue(doc['chunks'][2]['text'].lstrip().startswith('\\subsection'))

    def test_newpage_before_body_is_not_a_cut(self):
        doc = self.split(
            '\\chapter{甲}\n甲文\n\\newpage\n中间段落\n\\newpage\n\\section{一}\n节文\n')
        self.assertEqual([c['id'] for c in doc['chunks']], ['ch1', 'ch1_2'])
        self.assertIn('中间段落', doc['chunks'][0]['text'])

    def test_newpage_before_next_chapter_not_empty_piece(self):
        doc = self.split('\\chapter{甲}\n甲文\n\\newpage\n\\chapter{乙}\n乙文\n')
        self.assertEqual([c['id'] for c in doc['chunks']], ['ch1', 'ch2'])

    def test_subchunk_does_not_advance_chapter_number(self):
        doc = self.split('\\chapter{甲}\n甲\n\\newpage\n\\section{一}\n一\n'
                         '\\chapter{乙}\n乙\n')
        self.assertEqual([c['id'] for c in doc['chunks']], ['ch1', 'ch1_2', 'ch2'])
        self.assertTrue(doc['chunks'][1]['starred'])
        self.assertFalse(doc['chunks'][2]['starred'])

    def test_counter_snapshot(self):
        text = (
            '\\section{甲}\n'
            '\\begin{figure}x\\end{figure}\n'
            '\\begin{equation}a\\end{equation}\n'
            '\\begin{equation}b\\nonumber\\end{equation}\n'
            '\\subsection{乙}\n'
            '\\begin{equation}c\\end{equation}\n'
            '\\begin{align}x&=1\\\\y&=2\\nonumber\\end{align}\n'
            '\\begin{theorem}t\\end{theorem}\n'
        )
        s = fastpath._counter_snapshot(text)
        self.assertEqual(s['section'], 1)
        self.assertEqual(s['subsection'], 1)
        self.assertEqual(s['figure'], 1)
        # 1 equation + 1 equation + align 1 行编号（另一行 \nonumber）
        self.assertEqual(s['equation'], 3)
        self.assertEqual(s['theorem'], 1)

    def test_hash_includes_counters(self):
        c = {'text': '\\section{一}正文', 'counters': {'section': 1}}
        d = dict(c, counters={'section': 2})
        self.assertNotEqual(fastpath._chunk_hash('pre', c),
                            fastpath._chunk_hash('pre', d))


class XrefSigTest(unittest.TestCase):
    """分片缓存必须跟着「所引标签在聚合 aux 里的定义」失效，否则链接指向旧锚点。"""

    def test_no_refs_gives_none(self):
        self.assertIsNone(fastpath._xref_sig('正文没有引用', {}))

    def test_label_page_change_does_not_invalidate_sig(self):
        # 解耦模式下 \pageref 由合并阶段 /FPR 原位热更新，仅页码漂移不应让引用块失效
        text = '见第\\pageref{sec:a}节'
        a = {'sec:a': '\\newlabel{sec:a}{{1.1}{8}{甲}{subsection.1.1}{}}'}
        b = {'sec:a': '\\newlabel{sec:a}{{1.1}{9}{甲}{subsection.1.1}{}}'}
        self.assertEqual(fastpath._xref_sig(text, a),
                         fastpath._xref_sig(text, b))

    def test_label_number_change_changes_sig(self):
        text = '见第\\ref{sec:a}节'
        a = {'sec:a': '\\newlabel{sec:a}{{1.1}{8}{甲}{subsection.1.1}{}}'}
        b = {'sec:a': '\\newlabel{sec:a}{{1.2}{8}{甲}{subsection.1.1}{}}'}
        self.assertNotEqual(fastpath._xref_sig(text, a),
                            fastpath._xref_sig(text, b))

    def test_label_anchor_change_changes_sig(self):
        text = '\\ref{sec:a}'
        a = {'sec:a': '\\newlabel{sec:a}{{1.1}{8}{甲}{subsubsection*.30}{}}'}
        b = {'sec:a': '\\newlabel{sec:a}{{1.1}{8}{甲}{subsection.1.1}{}}'}
        self.assertNotEqual(fastpath._xref_sig(text, a),
                            fastpath._xref_sig(text, b))

    def test_missing_label_still_has_sig(self):
        # 未定义标签的签名不能是 None（None 表示「无引用」，会让旧块蒙混过关）
        self.assertIsNotNone(fastpath._xref_sig('\\ref{sec:x}', {}))


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


class PdfStampTest(unittest.TestCase):
    """页码解耦判定、导言区按需精简、TOC/aux 页码平移与 fmeta 解析。"""

    def test_can_decouple_supported_and_guards(self):
        self.assertTrue(pdf_stamp.can_decouple(
            r'\documentclass[UTF8]{ctexbook}\usepackage{amsmath}', '', [{'text': '正文'}]))
        self.assertFalse(pdf_stamp.can_decouple(
            r'\documentclass{ctexbook}\usepackage{fancyhdr}', '', [{'text': '正文'}]))
        self.assertFalse(pdf_stamp.can_decouple(
            r'\documentclass{ctexbook}', r'\frontmatter', [{'text': '正文'}]))
        self.assertTrue(pdf_stamp.can_decouple(
            r'\documentclass{ctexbook}', '% \\frontmatter\n', [{'text': '正文'}]))

    def test_strip_preamble_removes_unused_heavy_pkgs(self):
        pre = (
            '\\documentclass{ctexbook}\n'
            '\\usepackage{tikz}\n'
            '\\usetikzlibrary{arrows}\n'
            '\\usepackage{chemfig}\n'
            '\\setchemfig{atom sep=1.5em}\n'
            '\\usepackage{mhchem}\n'
            '\\usepackage{booktabs,float}\n'
            '\\usepackage{amsmath}\n'
        )
        s_plain = pdf_stamp.strip_preamble(pre, '纯正文段落')
        for tok in ('tikz', 'chemfig', 'mhchem', 'booktabs', 'float'):
            self.assertNotIn(tok, s_plain)
        self.assertIn('amsmath', s_plain)

        s_chem = pdf_stamp.strip_preamble(pre, r'\chemfig{H-O-H} \ce{H2O}')
        self.assertIn('chemfig', s_chem)
        self.assertIn('tikz', s_chem)
        self.assertIn('mhchem', s_chem)
        self.assertNotIn('booktabs', s_chem)

    def test_shift_toc_and_prepare_for_front(self):
        toc = (
            '\\contentsline {chapter}{\\numberline {一}甲}{3}{chapter.1}%\n'
            '\\contentsline {section}{\\numberline {1.1}乙}{5}{section.1.1}%\n'
        )
        shifted = pdf_stamp.shift_toc_pages(toc, 4)
        self.assertIn('{7}{chapter.1}', shifted)
        self.assertIn('{9}{section.1.1}', shifted)

        struct_t, wrap_t, pages = pdf_stamp.prepare_toc_for_front(shifted)
        self.assertEqual(pages, ['7', '9'])
        self.assertIn('{*}{chapter.1}', struct_t)
        self.assertIn(r'{\FloriaTocPg{0}{7}}', wrap_t)
        self.assertIn(r'{\FloriaTocPg{1}{9}}', wrap_t)

        # 页码再变但结构不变时，struct_text 必须完全相同（front 零重编保证）
        struct_t2, _, pages2 = pdf_stamp.prepare_toc_for_front(
            pdf_stamp.shift_toc_pages(toc, 10))
        self.assertEqual(struct_t, struct_t2)
        self.assertEqual(pages2, ['13', '15'])

    def test_shift_newlabel_line(self):
        ln = r'\newlabel{纯溶液和固体}{{1.2}{3}{标题}{section.1.2}{}}'
        name, shifted, pg = pdf_stamp.shift_newlabel_line(ln, 6)
        self.assertEqual(name, '纯溶液和固体')
        self.assertEqual(pg, '9')
        self.assertIn('{{1.2}{9}{标题}', shifted)

    def test_parse_fmeta_with_patch_line(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, 'wrapper.fmeta')
            with open(p, 'w', encoding='utf-8') as f:
                f.write(
                    'P 1 plain 1 -1\n'
                    'P 2 headings 1 2\n'
                    'BODY_PAGES 2\n'
                    'HAS_ATLAS 1\n'
                    'PATCH 0|1\n'
                )
            fm = pdf_stamp.parse_fmeta(p)
            self.assertEqual(fm['body_pages'], 2)
            self.assertTrue(fm['has_atlas'])
            self.assertEqual(fm['ftoc_pages'], {0})
            self.assertEqual(fm['fpr_pages'], {1})


class TikzCacheTest(unittest.TestCase):
    """TikZ 图元扫描：过滤上下文依赖、跳过注释/嵌套、捕获局部 \\setchemfig。"""

    def test_find_cacheable_pictures_filters_and_captures(self):
        blocks = [
            '\\setchemfig{atom sep=1.8em}\n',
            '\\begin{tikzpicture}\\draw (0,0)--(1,0);\\end{tikzpicture}\n',
            '\\begin{tikzpicture}\\node{\\chemfig{A-B}};\\end{tikzpicture}\n',
            '\\begin{tikzpicture}[remember picture]\\draw (0,0)--(1,1);\\end{tikzpicture}\n',
            '% \\begin{tikzpicture}\\draw (0,0)--(2,0);\\end{tikzpicture}\n',
            '\\begin{tikzpicture}\\draw (0,0)--(0,1);\\end{tikzpicture}\n',
            '\\begin{tikzpicture}\\draw (0,0)--(1,1);\\end{tikzpicture}\n',
            '\\begin{tikzpicture}\\node{\\ref{fig:1}};\\end{tikzpicture}\n',
        ]
        spans = tikz_cache.find_cacheable_pictures(''.join(blocks))
        self.assertEqual(len(spans), 4)
        # 第 2 个含 \chemfig，应自动捕获前置 \setchemfig
        self.assertEqual(spans[1][3], r'\setchemfig{atom sep=1.8em}')
        # 不含 \chemfig 的普通 tikzpicture 不携带 \setchemfig
        self.assertEqual(spans[0][3], '')


class CounterFilterAndNativeTest(unittest.TestCase):
    """块计数器按需过滤哈希 + Rust 原生合并器可用性与 SyncTeX 字节一致测试。"""

    def test_table_counter_ignored_for_table_free_chunk(self):
        c1 = {'text': '\\section{无表节}纯文字', 'counters': {'section': 1, 'table': 2, 'figure': 3},
              'marks': {'section_next': True}}
        c2 = dict(c1, counters={'section': 1, 'table': 1, 'figure': 0})
        self.assertEqual(fastpath._chunk_hash('pre', c1), fastpath._chunk_hash('pre', c2))

        ct1 = {'text': '\\section{有表节}\\begin{table}a\\end{table}',
               'counters': {'section': 1, 'table': 2}}
        ct2 = dict(ct1, counters={'section': 1, 'table': 1})
        self.assertNotEqual(fastpath._chunk_hash('pre', ct1), fastpath._chunk_hash('pre', ct2))

    def test_subsection_counter_reset_when_starting_with_section(self):
        c1 = {'text': '\\section{新节}\\subsection{子节}',
              'counters': {'section': 1, 'subsection': 3},
              'marks': {'section_next': True}}
        c2 = dict(c1, counters={'section': 1, 'subsection': 5})
        self.assertEqual(fastpath._chunk_hash('pre', c1), fastpath._chunk_hash('pre', c2))

        sub1 = {'text': '\\subsection{续子节}正文',
                'counters': {'section': 1, 'subsection': 3},
                'marks': {'section_next': False}}
        sub2 = dict(sub1, counters={'section': 1, 'subsection': 4})
        self.assertNotEqual(fastpath._chunk_hash('pre', sub1), fastpath._chunk_hash('pre', sub2))

    def test_theorem_counters_filtered_when_absent_and_kept_when_used(self):
        no_thm1 = {'text': '纯正文', 'counters': {'theorem': 2, 'lemma': 1}}
        no_thm2 = {'text': '纯正文', 'counters': {'theorem': 5, 'lemma': 0}}
        self.assertEqual(fastpath._chunk_hash('pre', no_thm1), fastpath._chunk_hash('pre', no_thm2))

        thm1 = {'text': '\\begin{lemma}引理\\end{lemma}', 'counters': {'theorem': 2, 'lemma': 1}}
        thm2 = {'text': '\\begin{lemma}引理\\end{lemma}', 'counters': {'theorem': 3, 'lemma': 1}}
        self.assertNotEqual(fastpath._chunk_hash('pre', thm1), fastpath._chunk_hash('pre', thm2))

    def test_native_synctex_matches_python_byte_for_byte(self):
        exe = pdf_stamp.find_native_merger()
        if not exe:
            self.skipTest('floria_merge binary not built')
        import gzip
        with tempfile.TemporaryDirectory() as td:
            s1 = os.path.join(td, 'a.slim')
            s2 = os.path.join(td, 'b.slim')
            with open(s1, 'w', encoding='utf-8') as f:
                f.write('H Magnification:1000\nH Unit:1\nH X Offset:0\nH Y Offset:0\n'
                        'PAGES 2\n{1\n[1,10:100,200:300,400,0\n}1\n{2\n}2\nFLORIA_SLIM_END\n')
            with open(s2, 'w', encoding='utf-8') as f:
                f.write('H Magnification:1000\nH Unit:1\nH X Offset:0\nH Y Offset:0\n'
                        'PAGES 1\n{1\nh1,5:50,60:70,80,0\n}1\nFLORIA_SLIM_END\n')
            items = [
                {'slim': s1, 'offset': 0, 'chunk_line': 1},
                {'slim': s2, 'offset': 2, 'chunk_line': 50},
            ]
            out_py = os.path.join(td, 'py.synctex.gz')
            out_rs = os.path.join(td, 'rs.synctex.gz')
            fastpath._merge_synctex_fast(items, out_py)
            res = pdf_stamp.run_native_merge(synctex_args=(items, out_rs))
            self.assertIsNotNone(res)
            self.assertTrue(res['ok'])
            with gzip.open(out_py, 'rb') as fp, gzip.open(out_rs, 'rb') as fr:
                self.assertEqual(fp.read(), fr.read())


if __name__ == '__main__':
    unittest.main()
