# -*- coding: utf-8 -*-
"""正文规则的**两个前提**：书源类型（用户的意图）× 页面档位（判据算出来的事实）。

为什么钉这两条：

- **类型**决定先看媒体还是先看文本。同一个正文页上两种东西都可能有（图片站也有 >200 字的
  说明文字），原来只按文本量挑，实测给小爱漫画（`bookSourceType=2`）配了 `article@textNodes`
  ——选得中，选中的不是数据（2026-09-21）。
- **档位**决定这条 CSS 规则给不给：判到 L3/L4 时数据要解密 / 走接口，CSS 必然取不到；
  判到 L2 时数据要渲染后才有，规则照给但**要配上 `webView`**，否则同样是静默取空。
"""
from __future__ import annotations

import contextlib
import json
import pathlib
import tempfile
import unittest
from unittest import mock

from core.analyzer import analyze_detail_page
from services.add_source import _page_for_analysis, run_add

BOOK_URL = "http://example.com/book/1/"
#: 详情页 stub：目录规则推得出来（两个以上章节链接）
DETAIL = """<html><body><div class="row"><ul class="chapter-list">
<li><a href="/book/1/1.html">第一章</a></li>
<li><a href="/book/1/2.html">第二章</a></li>
</ul></div></body></html>"""
#: 正文页 stub：图片列表 + 一段 >200 字的说明文字（两种东西同时在，正是要用类型分的地方）
CHAPTER = ("<html><body><div class='reader'>"
           + "".join("<img src='/img/%d.jpg'>" % i for i in range(1, 5))
           + "</div><div class='desc'><p>" + "简介" * 120 + "</p></div></body></html>")
CHAPTER_TEXT_ONLY = "<html><body><div class='body'><p>" + "正文" * 200 + "</p></div></body></html>"
#: 只有图片、一个字的正文都没有
CHAPTER_IMG_ONLY = ("<html><body><div class='reader'>"
                    + "".join("<img src='/img/%d.jpg'>" % i for i in range(1, 5))
                    + "</div></body></html>")


def _analyze(chapter_html, want, facts=None):
    return analyze_detail_page(DETAIL, BOOK_URL,
                               page_fetcher=lambda url: chapter_html,
                               want=want, page_facts=facts)


class TypeDecidesWhichBranchTests(unittest.TestCase):

    def test_media_source_gets_the_image_rule_even_with_a_long_text_block(self):
        r = _analyze(CHAPTER, "media")
        self.assertIn("img@src", r["content"], r)
        self.assertIn("图片正文", r["note"])

    def test_text_source_gets_the_text_rule_and_says_what_else_is_on_the_page(self):
        r = _analyze(CHAPTER, "text")
        self.assertIn("@textNodes", r["content"], r)
        self.assertIn("4 张图", r["note"], "页面上的另一种东西要写出来")

    def test_media_declared_but_no_images_says_it_fell_back(self):
        r = _analyze(CHAPTER_TEXT_ONLY, "media")
        self.assertIn("@textNodes", r["content"], r)
        self.assertIn("没找到图片列表", r["note"])

    def test_text_declared_but_the_page_only_has_images_falls_to_the_image_rule(self):
        r = _analyze(CHAPTER_IMG_ONLY, "text")
        self.assertIn("img@src", r["content"], r)


class LayerDecidesWhetherToHandOutTheRuleTests(unittest.TestCase):

    def test_l3_gets_no_css_rule_and_the_next_step(self):
        r = _analyze(CHAPTER, "media", facts={"layer": "L3", "why": "长 base64 负载"})
        self.assertEqual(r["content"], "", "L3 的页给 CSS 规则就是假成功")
        self.assertIn("L3", r["note"])
        self.assertIn("webJs", r["note"])
        self.assertIn("长 base64 负载", r["note"], "判据要能一路走到用户眼前")

    def test_l4_gets_no_css_rule_and_says_collect_the_api(self):
        r = _analyze(CHAPTER, "media", facts={"layer": "L4", "why": "页面里有接口路径"})
        self.assertEqual(r["content"], "")
        self.assertIn("接口", r["note"])

    def test_l2_keeps_the_rule_and_pairs_it_with_webview(self):
        r = _analyze(CHAPTER, "media", facts={"layer": "L2", "why": "图片容器在、但图没有地址"})
        self.assertIn("img@src", r["content"])
        self.assertTrue(r["toc"]["chapterUrl"].endswith(',{"webView":true}'), r["toc"])
        self.assertIn("webView", r["note"])

    def test_l1_leaves_everything_alone(self):
        """没判到档位（`page_facts` 空）时一个字都不改：不带上 webView、附注里不提档位。"""
        r = _analyze(CHAPTER, "media")
        self.assertIn("img@src", r["content"])
        self.assertNotIn("webView", r["toc"]["chapterUrl"])
        self.assertNotIn("判到", r["note"])


class WiringTests(unittest.TestCase):
    """生成链那一半：类型与档位要真的走到**存下来的那份源**里（不是只 print）。"""

    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="content_rule_"))
        self.out = self.root / "gen.json"
        fixture = pathlib.Path(__file__).parent / "fixtures" / "samsbook_search_shaosong.html"
        self.search_html = fixture.read_text(encoding="utf-8")

    def _run(self, facts, chapter_html=CHAPTER):
        seen = {"layer": facts.get("layer", ""), "why": facts.get("why", "")}

        def fake_page(url, want, notes, timeout=60, facts=None):
            # 取页器如实报告「这一页在我们抓的那份上判到哪一档」。
            # `want` 是区分哪一页的判据：详情页要 list、正文页要 media
            if want != "media":
                return DETAIL
            if facts is not None:
                facts.update(dict(seen, url=url))
            return chapter_html

        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch("services.add_source.fetch",
                                           side_effect=lambda u, *a, **k: self.search_html))
            stack.enter_context(mock.patch("services.add_source._find_main_sources",
                                           return_value=[]))
            stack.enter_context(mock.patch("services.add_source._page_for_analysis",
                                           side_effect=fake_page))
            return run_add("http://example.com/search.php?q=绍宋", name="测试源",
                           source_type="manga", output=str(self.out), no_ask=True,
                           probe=False, verify=False, interactive=False)

    def test_manga_l2_source_saves_the_webview_option_and_the_note(self):
        out = self._run({"layer": "L2", "why": "图片容器在、但图没有地址"})
        self.assertEqual(out.rc, 0, out.error)
        src = json.loads(self.out.read_text(encoding="utf-8"))[0]
        self.assertEqual(src["bookSourceType"], 2)
        self.assertTrue(src["ruleToc"]["chapterUrl"].endswith(',{"webView":true}'),
                        src["ruleToc"])
        self.assertIn("webView", src["bookSourceComment"], src["bookSourceComment"])

    def test_l3_source_gets_no_content_rule_and_the_reason_is_saved(self):
        out = self._run({"layer": "L3", "why": "长 base64 负载"})
        self.assertEqual(out.rc, 0, out.error)
        src = json.loads(self.out.read_text(encoding="utf-8"))[0]
        self.assertNotIn("content", src.get("ruleContent") or {},
                         "L3 不给正文规则")
        self.assertIn("L3", src["bookSourceComment"], src["bookSourceComment"])


class PageFactsTests(unittest.TestCase):
    """取页器要把「这一页在我们抓的那份上判到哪一档」如实交下游——正文规则给不给靠它。

    这条单独钉：上面两个接线测试把取页器整个换掉了，**那份事实从哪来**就没人管了。
    """

    L3_PAGE = ("<html><body><script>var p = '%s';</script>"
               "<div id='c'><img><img></div></body></html>" % ("s4pZ" * 80))

    def test_facts_carry_the_layer_judged_on_our_own_fetch(self):
        notes, facts = [], {}
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch("services.add_source.fetch",
                                           side_effect=lambda u, *a, **k: self.L3_PAGE))
            # 引擎那条不联网：判到 L3 就会去要「渲染 / 解密后的那份」
            stack.enter_context(mock.patch("core.jvm_debug.page_from_engine",
                                           side_effect=lambda u, **k: self.L3_PAGE))
            html = _page_for_analysis("http://example.com/book/1/1.html", "media", notes,
                                      facts=facts)
        self.assertEqual(facts["layer"], "L3")
        self.assertIn("base64", facts["why"], "判据要跟着事实一起交下去")
        self.assertEqual(html, self.L3_PAGE)
        self.assertTrue(any("L3" in n for n in notes), notes)


if __name__ == "__main__":
    unittest.main()
