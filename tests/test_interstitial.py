# -*- coding: utf-8 -*-
"""材料级的判据：**手里这份是不是站点本身**（反爬拦截页）。

实测起点（2026-09-21）：引擎在受保护的站上交回的是拦截页——`18read.net` 两次都是
Cloudflare 的「请稍候…」、`www.banxia.cc` 两次都是 `Attention Required!`。而界面上
看不出来，于是下游会**按拦截页判层、按它写规则**（选得中、选中的不是站点）。

判据复用 `models.ANTI_BOT_MARKERS`（不新造词表）；这里钉三件事：判据本身、
抽屉那份材料带标记且不判层、生成链在拦截页上要么要到引擎那份要么明确不给规则。
"""
from __future__ import annotations

import contextlib
import json
import pathlib
import tempfile
import unittest
from unittest import mock

from core.app_debug import fetch_debug_pages
from core.quality import interstitial_marker
from services.add_source import _page_for_analysis

#: 两页真样本的**形状**（取自 data/out/interstitial_*.html，测试不依赖那两个文件）
CHALLENGE = ("<html lang='en-US'><head><title>请稍候…</title></head><body>"
             "<h1>18read.net</h1><h2>正在进行安全验证</h2>"
             "<script>window._cf_chl_opt = {};</script></body></html>")
BLOCKED = ("<html><head><title>Attention Required! | Cloudflare</title></head><body>"
           "<div class='cf-error-details cf-wrapper'>Please enable cookies.</div>"
           "<!-- /.captcha-container --></body></html>")
#: 正常页：正文里 `请稍候` 出现多次（小爱漫画实测 60 次）——通用词不能当判据
NORMAL = ("<html><body><div class='chapter-message'>正在加载图片，请稍候</div>"
          "<div class='chapter-message'>正在加载图片，请稍候</div><img><img></div></body></html>")


class MarkerTests(unittest.TestCase):

    def test_both_real_interstitials_are_recognized(self):
        self.assertTrue(interstitial_marker(CHALLENGE))
        self.assertTrue(interstitial_marker(BLOCKED))

    def test_a_normal_page_that_says_please_wait_is_not_an_interstitial(self):
        self.assertEqual(interstitial_marker(NORMAL), "",
                         "`请稍候` 是通用词：拿它判会把正常页说成拦截页")
        self.assertEqual(interstitial_marker("<html><body>甲</body></html>"), "")


class DrawerMaterialTests(unittest.TestCase):
    """抽屉那份材料：拦截页照存（用户要看得到），但**不判层**并说明为什么。"""

    def _steps(self):
        return [{"name": "search", "url": "https://a.com/s", "page_id": "search"}]

    def test_interstitial_page_is_kept_but_not_judged_as_a_site(self):
        with mock.patch("core.app_debug.fetch_ex",
                        return_value=mock.Mock(html=CHALLENGE, cached=False,
                                               fetched_at="", charset="")):
            pages = fetch_debug_pages(self._steps(), {})
        self.assertEqual(pages[0]["html"], CHALLENGE, "材料照存，用户要能自己看")
        layer = pages[0]["page_layer"]
        self.assertEqual(layer["layer"], "")
        self.assertIn("拦截页", layer["unsure"])
        self.assertEqual(layer["evidence"], [])


class GenerationChainTests(unittest.TestCase):
    """生成链：拦截页上要么要到引擎那份，要么明确说「这一段先不给」。"""

    def test_our_interstitial_goes_to_the_engine_and_the_engine_copy_is_kept(self):
        notes, facts = [], {}
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch("services.add_source.fetch",
                                           side_effect=lambda u, *a, **k: CHALLENGE))
            stack.enter_context(mock.patch("core.jvm_debug.page_from_engine",
                                           side_effect=lambda u, **k: "<html>站点本身</html>"))
            html = _page_for_analysis("https://a.com/1.html", "media", notes, facts=facts)
        self.assertEqual(html, "<html>站点本身</html>")
        self.assertEqual(facts["challenge"], "安全验证", "这一笔要跟着事实交下去")
        self.assertTrue(any("拦截页" in n for n in notes), notes)

    def test_engine_that_still_returns_an_interstitial_raises_with_the_next_step(self):
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch("services.add_source.fetch",
                                           side_effect=lambda u, *a, **k: CHALLENGE))
            stack.enter_context(mock.patch("core.jvm_debug.page_from_engine",
                                           side_effect=lambda u, **k: BLOCKED))
            with self.assertRaises(RuntimeError) as ctx:
                _page_for_analysis("https://a.com/1.html", "media", [])
        msg = str(ctx.exception)
        self.assertIn("拦截页", msg)
        self.assertIn("人工过一次", msg, "要给下一步动作，不能只说失败")

    def test_engine_unavailable_on_an_interstitial_says_which_case_it_is(self):
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch("services.add_source.fetch",
                                           side_effect=lambda u, *a, **k: CHALLENGE))
            stack.enter_context(mock.patch("core.jvm_debug.page_from_engine",
                                           side_effect=RuntimeError("引擎没配")))
            with self.assertRaises(RuntimeError) as ctx:
                _page_for_analysis("https://a.com/1.html", "media", [])
        self.assertIn("拦截页", str(ctx.exception), "要说是拦截页那条路，而不是「判到 L2」")


class ContentRulePairedWithWebviewTests(unittest.TestCase):
    """拦截页上配出来的正文规则要带 webView：App 渲染时自己再过一次那道验证。"""

    def test_challenge_facts_pair_the_rule_with_webview(self):
        from core.analyzer import analyze_detail_page
        detail = ("<html><body><div class='row'><ul class='chapter-list'>"
                  "<li><a href='/b/1.html'>第一章</a></li>"
                  "<li><a href='/b/2.html'>第二章</a></li></ul></div></body></html>")
        chapter = ("<html><body><div class='reader'>"
                   + "".join("<img src='/img/%d.jpg'>" % i for i in range(1, 5))
                   + "</div></body></html>")
        r = analyze_detail_page(detail, "http://a.com/b/",
                                page_fetcher=lambda u: chapter, want="media",
                                page_facts={"layer": "", "why": "", "challenge": "安全验证"})
        self.assertIn("img@src", r["content"])
        self.assertTrue(r["toc"]["chapterUrl"].endswith(',{"webView":true}'), r["toc"])
        self.assertIn("反爬验证", r["note"])

    def test_challenge_beats_the_l3_refusal(self):
        """既是挑战页、层又没判出来时，别按「判不了」放过——按挑战页配 webView。"""
        from core.analyzer import _apply_layer_guard
        toc, note = {"chapterUrl": ".a@href"}, ""
        rule, toc, note = _apply_layer_guard("sel img@src", toc, note,
                                             {"layer": "", "challenge": "captcha"})
        self.assertEqual(rule, "sel img@src")
        self.assertIn("webView", toc["chapterUrl"])


if __name__ == "__main__":
    unittest.main()
