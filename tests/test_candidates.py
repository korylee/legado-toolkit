# -*- coding: utf-8 -*-
"""候选生成的断言（`core/candidates` 是交互候选面板启发式的唯一一份，
原前端 ruleCandidates.js 已随下沉删除）。

面板只展示与排序、没有「试」：这里钉的是规则写法、条数/去重/占比的口径
与样本有无；排序是启发式，只对「谁在前」做断言。
"""

import asyncio
import unittest

from fastapi import HTTPException

from backend.api.rules import rule_candidates
from backend.schemas import CandidatesRequest
from core.candidates import KINDS, find_candidates

HTML = """
<div class="list">
  <div class="item"><a href="/book/123.html"><img src="/a.jpg"/></a></div>
  <div class="item"><a href="/book/456.jpg"><img data-src="/b.jpg"/></a></div>
  <div class="item"><a href="/book/789"><img src=""/></a></div>
</div>
<div class="content">正文内容足够长。</div>
""".replace("正文内容足够长。", "正文内容足够长。" * 60)


def rules_of(kind):
    return {c["rule"]: c for c in find_candidates(HTML, kind)}


class FindCandidatesTests(unittest.TestCase):

    def test_link_rules_and_count(self):
        got = rules_of("link")
        self.assertIn(".item@tag.a@href", got)
        self.assertEqual(got[".item@tag.a@href"]["count"], 3)
        # 样本优先给「像目标」的（含数字/.html 的详情页链接）
        self.assertTrue(got[".item@tag.a@href"]["samples"][0].startswith("/book/"))

    def test_link_fallback_covers_all_links(self):
        """兜底 `tag.a@href` 的 count = 页面全部链接——它的存在意义就是让用户
        看出「页面里到底有多少链接」（前端那份只收一个锚点、恒为 1，与本意
        相反，下沉时按本意修正）。"""
        got = rules_of("link")
        self.assertEqual(got["tag.a@href"]["count"], 3)

    def test_media_groups_by_attr_and_skips_empty(self):
        got = rules_of("media")
        self.assertEqual(got["tag.img@src"]["count"], 1)      # 空src是占位，不计
        self.assertEqual(got["tag.img@data-src"]["count"], 1)
        for c in got.values():
            self.assertGreaterEqual(c["count"], 1)

    def test_text_rule_with_sample(self):
        got = rules_of("text")
        self.assertIn(".content@text", got)
        c = got[".content@text"]
        self.assertGreaterEqual(c["count"], 200)
        self.assertTrue(c["samples"] and c["samples"][0])

    def test_list_rule_from_repeated_siblings(self):
        got = rules_of("list")
        self.assertIn(".list .item", got)
        self.assertEqual(got[".list .item"]["count"], 3)
        self.assertEqual(got[".list .item"]["samples"], [])

    def test_measure_invariants(self):
        """口径：hits = 非空值条数；uniq ≤ hits；ratio = hits / 页面链接数。
        「命中 1228」与「命中 1198」靠去重与占比才分得开。"""
        for c in find_candidates(HTML, "link"):
            self.assertLessEqual(c["uniq"], c["hits"])
            self.assertGreater(c["ratio"], 0)
            self.assertLessEqual(c["ratio"], 1)

    def test_unknown_kind_lists_valid_values(self):
        with self.assertRaises(ValueError) as ctx:
            find_candidates(HTML, "chapter")
        for k in KINDS:
            self.assertIn(k, str(ctx.exception))

    def test_no_candidates_is_an_empty_list(self):
        """空数组是个结论（页面上确实没有），不是失败——调用方连同诊断一起展示。"""
        self.assertEqual(find_candidates("<p>hi</p>", "link"), [])


class CandidatesRouteTests(unittest.TestCase):
    """端点只做入参校验与转交（直调端点函数，仓库无 TestClient 先例）。"""

    def _call(self, **kw):
        return asyncio.run(rule_candidates(CandidatesRequest(**kw)))

    def test_returns_candidates(self):
        r = self._call(html=HTML, kind="link")
        self.assertTrue(any(c["rule"] == ".item@tag.a@href" for c in r["candidates"]))

    def test_unknown_kind_is_400(self):
        with self.assertRaises(HTTPException) as ctx:
            self._call(html=HTML, kind="chapter")
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("link", str(ctx.exception.detail))

    def test_empty_html_is_400(self):
        with self.assertRaises(HTTPException) as ctx:
            self._call(html="   ", kind="link")
        self.assertEqual(ctx.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
