# -*- coding: utf-8 -*-
"""调试 key 拼装的断言：形态错 = App 静默无响应（最难排查的那类失败）。

前端那份 `debugKeys.test.js` 已随 target/query 切换删除，这里是 key 形态的
唯一闸门：改 `build_key` 的行为，先改这里的断言。
"""

import unittest

from core.debug_keys import EXPLORE_PREFIX, TARGETS, build_key


class BuildKeyTests(unittest.TestCase):

    def test_key_forms_by_target(self):
        """五类形态各一条：与 Debug.kt:236-279 的 when 链一一对应。"""
        self.assertEqual(build_key("search", "我", keyword="kw"), "我")
        self.assertEqual(build_key("explore", "", "http://m.site/sort/1/1.html"),
                         EXPLORE_PREFIX + "http://m.site/sort/1/1.html")
        self.assertEqual(build_key("info", "https://a.com/b/1", keyword="kw"),
                         "https://a.com/b/1")
        self.assertEqual(build_key("toc", "/b/1/", keyword="kw"), "++/b/1/")
        self.assertEqual(build_key("content", "/b/1/c1.html", keyword="kw"),
                         "--/b/1/c1.html")

    def test_hand_copied_prefix_is_stripped_once(self):
        """手抄 URL 常把 ++ / -- 一起带上；只去一次（removePrefix 语义），
        循环去会把真想要的 ++++url 改成别的意思。"""
        self.assertEqual(build_key("toc", "++/b/1/", keyword="kw"), "++/b/1/")
        self.assertEqual(build_key("content", "--/b/1/c1", keyword="kw"), "--/b/1/c1")
        self.assertEqual(build_key("content", "----/b/1/c1", keyword="kw"),
                         "----/b/1/c1")

    def test_rerun_keeps_url_verbatim_with_options(self):
        """「从此步重跑」的 URL 含 ,{...} 请求选项：原文透传（App 的 AnalyzeUrl 认它），
        不做库内规范化（AGENTS #5 那套是关联键口径）。"""
        url = '/b/1/c1.html,{"webView":true}'
        self.assertEqual(build_key("content", url, keyword="kw"), "--" + url)

    def test_empty_input_falls_back_to_search_start(self):
        """详情/目录/正文留空 = 从搜索起步（App 自己沿链往下串）。"""
        for target in ("search", "info", "toc", "content"):
            with self.subTest(target=target):
                self.assertEqual(build_key(target, "", keyword="我"), "我")

    def test_explore_empty_is_an_explicit_error(self):
        """发现留空且源没配 exploreUrl → 报原因，不静默回落成搜索
        （「想逛发现页」被偷换成「搜默认词」是最阴的那类错）。"""
        with self.assertRaises(ValueError) as ctx:
            build_key("explore", "", "", keyword="我")
        self.assertIn("exploreUrl", str(ctx.exception))

    def test_explore_prefers_query_over_configured_url(self):
        self.assertEqual(
            build_key("explore", "http://q.site/1.html", "http://cfg.site/2.html",
                      keyword="kw"),
            EXPLORE_PREFIX + "http://q.site/1.html")

    def test_unknown_target_lists_valid_values(self):
        with self.assertRaises(ValueError) as ctx:
            build_key("chapter", "/b/1/", keyword="kw")
        for t in TARGETS:
            self.assertIn(t, str(ctx.exception))

    def test_search_start_without_keyword_is_an_error(self):
        """要回落搜索而关键词为空（调用方没传设置值）→ 报错，不产出空 key
        ——空 key 交给 App 同样是静默无响应。"""
        with self.assertRaises(ValueError):
            build_key("search", "", keyword="")
        with self.assertRaises(ValueError):
            build_key("toc", "   ", keyword="")


if __name__ == "__main__":
    unittest.main()
