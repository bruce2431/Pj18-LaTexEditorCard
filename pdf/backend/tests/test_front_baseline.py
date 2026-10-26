# -*- coding: utf-8 -*-
"""前置页基线（封面/目录）的新鲜度判定。

回归 2026-10-26 PHYSIC：用户改了前置页正文（目录后的引言段落），快速路径仍从
旧 main.pdf 搬前置页，编出来的 PDF 看不出任何变化。根因是 `ensure_front_baseline`
把「没有 front.meta.json」当成基线可用——无法证明基线对应当前正文。
不依赖 TeX，只要 .build/floria 下的假 synctex。
"""
import gzip
import json
import os
import tempfile
import unittest

from floria import fastpath


def write_synctex(path, pages, inputs=None):
    """写一份最小可解析的 synctex：每个页一个 {N ... }N 页块。"""
    lines = ['SyncTeX Version:1', 'Input:1:main.tex', 'Output:pdf',
             'Magnification:1000', 'Unit:1', 'X Offset:0', 'Y Offset:0',
             'Content:']
    for p in range(1, pages + 1):
        lines.append('{%d' % p)
        lines.append('[1,1:0,0:0,0:0,0:0')
        lines.append('}%d' % p)
    lines += ['Postamble:', 'Count:0', '!%d' % pages, 'Post scriptum:']
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with gzip.open(path, 'wt', encoding='utf-8') as f:
        f.write('\n'.join(lines))


class FrontBaselineTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.book = self._tmp.name
        self.floria = os.path.join(self.book, '.build', 'floria')
        os.makedirs(self.floria, exist_ok=True)
        # 整本编译留下的 synctex / 基线
        write_synctex(os.path.join(self.book, 'main.synctex.gz'), 4)
        write_synctex(os.path.join(self.floria, 'front.synctex.gz'), 4)

    def tearDown(self):
        self._tmp.cleanup()

    def meta(self, **kw):
        with open(os.path.join(self.floria, 'front.meta.json'), 'w',
                  encoding='utf-8') as f:
            json.dump(kw, f)

    def ensure(self, front=4, fh='newhash'):
        return fastpath.ensure_front_baseline(self.book, front, fh)

    def test_missing_meta_is_stale(self):
        """没有 sidecar 就不知道基线对不对应当前正文 → 必须整本重编一次。"""
        self.assertFalse(self.ensure()[0])
        # 即使旧 synctex 覆盖 1..front，也不能就地采纳（那正是旧 bug 的入口）
        self.assertFalse(os.path.isfile(
            os.path.join(self.floria, 'front.meta.json')))

    def test_missing_hash_is_stale(self):
        self.meta(front=4)
        self.assertFalse(self.ensure()[0])

    def test_changed_front_is_stale(self):
        self.meta(front=4, front_hash='oldhash')
        self.assertFalse(self.ensure()[0])

    def test_changed_front_count_is_stale(self):
        self.meta(front=3, front_hash='newhash')
        self.assertFalse(self.ensure()[0])

    def test_matching_meta_reuses_baseline(self):
        self.meta(front=4, front_hash='newhash')
        self.assertEqual(self.ensure(), (True, False))

    def test_matching_meta_adopts_existing_full_synctex(self):
        """侧车 hash 对得上、但基线文件丢了 → 从 main.synctex.gz 就地采纳。"""
        self.meta(front=4, front_hash='newhash')
        os.remove(os.path.join(self.floria, 'front.synctex.gz'))
        self.assertEqual(self.ensure(), (True, True))
        self.assertTrue(os.path.isfile(
            os.path.join(self.floria, 'front.synctex.gz')))


if __name__ == '__main__':
    unittest.main()
