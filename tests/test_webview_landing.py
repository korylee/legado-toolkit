# -*- coding: utf-8 -*-
"""自动把「这一页要渲染」落成源上的 webView 选项。

判据与落点都是**上游结构**决定的，不是猜的：
  - 正文页地址由 ``tocRule.chapterUrl`` 逐章取进 ``BookChapter.url``，``contentDebug``
    收的就是它（上游 ``BookChapterList.kt:238``）——所以正文要渲染，只能挂那条规则；
  - 选项的写法是把 ``,{"webView":true}`` **追到每条 URL 尾巴**（``##$##`` 替换），
    不是拼在规则后面——拼在后面会让引擎把整串当选择器。

钉三件事：落点、幂等、**复核不过必须回滚**（源不能被改坏）。
"""
from __future__ import annotations

import unittest

from backend.jobs import debug_materials as DM
from core.autofill import (WEBVIEW_SUFFIX_REPLACEMENT, rule_path_value,
                           webview_rule_path)


def _src(**toc) -> dict:
    return {"bookSourceUrl": "https://x.com", "bookSourceType": 2,
            "searchUrl": "https://x.com/s?q={{key}}",
            "ruleSearch": {"bookUrl": "class.a@href"},
            "ruleBookInfo": {"tocUrl": ""},
            "ruleToc": {"chapterList": "class.ch", "chapterUrl": toc.get(
                "chapterUrl", "tag.a@href")}}


class LandingTests(unittest.TestCase):
    def test_content_lands_on_chapter_url(self):
        """正文 URL 只从 `chapterUrl` 来（上游结构），落点没有别的可能。"""
        self.assertEqual(webview_rule_path(_src(), "content"), "ruleToc.chapterUrl")

    def test_toc_falls_back_to_detail_when_toc_url_is_empty(self):
        """`tocUrl` 为空时目录页就是详情页的产出：挂到一条不存在的规则上等于没做。"""
        self.assertEqual(webview_rule_path(_src(), "toc"), "ruleSearch.bookUrl")
        with_url = _src()
        with_url["ruleBookInfo"]["tocUrl"] = "class.toc@href"
        self.assertEqual(webview_rule_path(with_url, "toc"), "ruleBookInfo.tocUrl")

    def test_option_is_appended_to_every_url_not_to_the_rule(self):
        out = DM.apply_webview(_src(), "content")
        self.assertEqual(out["after"], "tag.a@href" + WEBVIEW_SUFFIX_REPLACEMENT)
        self.assertIn('##$##,{"webView":true}', out["after"])
        # 选择器那一段必须**原样**：改了它，规则本来选中什么就变了
        self.assertTrue(out["after"].startswith("tag.a@href##"))
        # 原对象不动（返回副本）
        self.assertEqual(_src()["ruleToc"]["chapterUrl"], "tag.a@href")

    def test_already_configured_is_left_alone(self):
        """已经带过 webView 就不再加：两个 `,{...}` 上游只认最后一个，前面的会被吃掉。"""
        src = _src(chapterUrl="tag.a@href" + WEBVIEW_SUFFIX_REPLACEMENT)
        out = DM.apply_webview(src, "content")
        self.assertIsNone(out["source"])
        self.assertIn("已经带", out["reason"])


class VerifyRollbackTests(unittest.TestCase):
    """复核：跑**这一步**，验「选项真进了产出」+「这一步没变坏」。不过必须回滚。"""

    def _run(self, *, url_out: str, verdict: str):
        seen = {}

        def fake_runner(source, key, **kw):
            seen["source"] = source
            seen["key"] = key
            return {"steps": [{"name": "content", "verdict": verdict,
                               "url": url_out,
                               "detail": "内容为空" if verdict != "pass" else ""}]}

        out = DM.verify_webview(_src(), "content",
                                url="https://x.com/1.html", runner=fake_runner)
        return out, seen

    def test_pass_requires_the_option_to_reach_the_output(self):
        """`verdict=pass` 只说明没坏；**选项进没进产出**才是改动生效的证据。"""
        ok, seen = self._run(
            url_out='https://x.com/1.html,{"webView":true}', verdict="pass")
        self.assertTrue(ok["ok"])
        self.assertTrue(ok["produced"])
        self.assertIsNotNone(ok["source"])
        # 验的必须是**改后**的源（验改前那份看不出问题）
        self.assertIn("webView", seen["source"]["ruleToc"]["chapterUrl"])

    def test_pass_without_option_is_not_success(self):
        """跑通了但选项没进产出（被上游吃掉 / 改错位置）：不算成功，不交源。"""
        out, _ = self._run(url_out="https://x.com/1.html", verdict="pass")
        self.assertFalse(out["ok"])
        self.assertFalse(out["produced"])
        self.assertIsNone(out["source"])
        self.assertIn("没进到产出的地址里", out["reason"])

    def test_failure_keeps_the_reason(self):
        out, _ = self._run(
            url_out='https://x.com/1.html,{"webView":true}', verdict="fail")
        self.assertFalse(out["ok"])
        self.assertIn("内容为空", out["reason"])

    def test_already_configured_is_a_noop_not_a_failure(self):
        """已经带过 webView：记「无需改动」，不报假警报。"""
        src = _src(chapterUrl="tag.a@href" + WEBVIEW_SUFFIX_REPLACEMENT)
        out = DM.verify_webview(src, "content", url="https://x.com/1.html")
        self.assertTrue(out["ok"])
        self.assertEqual(out["scope"], "noop")


if __name__ == "__main__":
    unittest.main()