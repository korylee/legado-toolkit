# -*- coding: utf-8 -*-
"""L4：从抓到的请求里挑出数据接口，并把响应变成规则草稿（`core/net_hunt.py`）。

判据是两条推断，各留一行证据：**哪一条是接口**（评分 + `why`）与**字段叫什么**（猜的，进附注）。
所以这里钉三件事：只留像数据的、埋点类扣分、以及「猜出来的东西必须写进附注」。

材料是**实测形状**（响应体取自两轮真抓到的接口：歡享小說的搜索接口回的是 HTML、JSON 那半
按常见书单接口的形状写），不联网。
"""
from __future__ import annotations

import json
import unittest

from core.net_hunt import (jsonpath_candidates, pick_data_requests, rules_from_response,
                           score_request, search_api_via_engine)

KEYWORD = "我"
#: 歡享小說那条：`POST /api/query`，body `q=我`，**响应是 HTML**（所以要配 CSS 规则）
HTML_API = {
    "url": "https://18read.net/api/query", "method": "POST", "post_data": "q=" + KEYWORD,
    "status": 200, "mime": "text/html; charset=utf-8",
    "body": ("<ul class='list'><li><h2><a href='/book/1'>我的书</a></h2>"
             "<span class='author'>甲</span></li></ul>"),
}
#: JSON 接口（常见书单形状）
JSON_API = {
    "url": "https://a.com/api/search?word=" + KEYWORD, "method": "GET", "post_data": "",
    "status": 200, "mime": "application/json",
    "body": json.dumps({"code": 0, "data": {"list": [
        {"bookName": "我的书", "bookUrl": "/book/1", "coverUrl": "/c/1.jpg", "author": "甲"},
        {"bookName": "我的书二", "bookUrl": "/book/2", "coverUrl": "/c/2.jpg", "author": "乙"},
    ]}}, ensure_ascii=False),
}
#: 埋点：也走 XHR、也带 body，但**不是接口**
NOISE = {
    "url": "https://a.com/report/statistics?t=1", "method": "POST", "post_data": json.dumps(
        {"event": "click", "kw": KEYWORD}),
    "status": 200, "mime": "application/json", "body": json.dumps({"ok": True}),
}


class PickTests(unittest.TestCase):

    def test_the_search_api_scores_above_the_tracker(self):
        picked = pick_data_requests([NOISE, HTML_API, JSON_API], want="list", keyword=KEYWORD)
        self.assertTrue(picked)
        self.assertEqual(picked[0]["entry"]["url"], JSON_API["url"], picked)
        urls = [p["entry"]["url"] for p in picked]
        self.assertNotIn(NOISE["url"], urls, "埋点类要扣到 0 分以下，一条都不留")
        for p in picked:
            self.assertTrue(p["why"], "每条都要有理由（用户要能反驳）")

    def test_a_tracker_alone_yields_nothing(self):
        self.assertEqual(pick_data_requests([NOISE], want="list", keyword=KEYWORD), [])

    def test_dull_responses_are_not_picked(self):
        plain = {"url": "https://a.com/f", "method": "GET", "post_data": "", "status": 200,
                 "mime": "image/png", "body": ""}
        self.assertEqual(pick_data_requests([plain], want="list", keyword=KEYWORD), [])

    def test_reasons_name_what_they_saw(self):
        got = score_request(JSON_API, "list", KEYWORD)
        self.assertIn("JSON", got["why"])
        self.assertIn("搜索词", got["why"])


class RulesFromResponseTests(unittest.TestCase):

    def test_html_response_goes_through_the_existing_inference(self):
        got = rules_from_response(HTML_API, KEYWORD)
        self.assertEqual(got["kind"], "html")
        self.assertTrue(got["rules"]["bookList"])
        self.assertIn("响应是 HTML", got["note"])

    def test_post_shape_is_copied_with_the_keyword_templated(self):
        got = rules_from_response(HTML_API, KEYWORD)
        self.assertTrue(got["search_url"].startswith(HTML_API["url"] + ","), got["search_url"])
        self.assertIn('"method":"POST"', got["search_url"])
        self.assertIn("{{key}}", got["search_url"], "关键词要换成 Legado 的模板占位")

    def test_json_response_yields_jsonpath_and_says_the_fields_are_guessed(self):
        got = rules_from_response(JSON_API, KEYWORD)
        self.assertEqual(got["kind"], "json")
        self.assertEqual(got["rules"]["bookList"], "$.data.list[*]")
        self.assertEqual(got["rules"]["name"], "$.bookName")
        self.assertEqual(got["rules"]["bookUrl"], "$.bookUrl")
        self.assertIn("猜的", got["note"], "字段名是推断出来的，必须写进附注")

    def test_json_without_a_homogeneous_array_says_so_instead_of_guessing(self):
        flat = dict(JSON_API, body=json.dumps({"code": 0, "msg": "ok"}))
        got = rules_from_response(flat, KEYWORD)
        self.assertEqual(got["rules"], {})
        self.assertIn("同构对象数组", got["note"])


class JsonPathTests(unittest.TestCase):

    def test_finds_the_object_array_and_its_fields(self):
        cands = jsonpath_candidates(JSON_API["body"])
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0]["path"], "$.data.list[*]")
        self.assertEqual(cands[0]["count"], 2)

    def test_garbage_is_not_a_candidate(self):
        self.assertEqual(jsonpath_candidates("<html>不是 JSON</html>"), [])
        self.assertEqual(jsonpath_candidates('{"a": 1, "b": "x"}'), [])


class ShapeGateTests(unittest.TestCase):
    """侧车那个键的形状闸门（`core/app_debug.network_entries`）。

    为什么必须另钉：上面那些测试全把 `page_from_engine` 整个换掉了——**闸门就从没被执行过**
    （变异验证抓到的第二个缺口）。而它的失败模式正是「静默」：形状不对时整块丢掉 + 一句日志，
    下游只会看到「L4 找不到接口」。
    """

    def _one(self, **kw):
        item = {"url": "https://a.com/api", "method": "post", "post_data": "q=1",
                "status": "200", "mime": "application/json", "body": '{"a":1}'}
        item.update(kw)
        return item

    def test_a_good_entry_is_normalized(self):
        from core.app_debug import network_entries
        got = network_entries([self._one()])
        self.assertEqual(got[0]["method"], "POST", "方法统一成大写")
        self.assertEqual(got[0]["status"], 200, "状态码转成整数")
        self.assertEqual(got[0]["body"], '{"a":1}')

    def test_bad_shapes_are_dropped_not_crashed(self):
        from core.app_debug import network_entries
        self.assertEqual(network_entries("不是数组"), [])
        self.assertEqual(network_entries(None), [])
        self.assertEqual(network_entries([{"no_url": 1}, "garbage"]), [])
        self.assertEqual(network_entries([self._one(body=[1, 2])])[0]["body"], "",
                         "body 不是字符串就当没有（别把列表塞进规则材料）")

    def test_long_body_is_cut(self):
        from core.app_debug import MAX_NET_BODY_CHARS, network_entries
        got = network_entries([self._one(body="x" * (MAX_NET_BODY_CHARS + 100))])
        self.assertEqual(len(got[0]["body"]), MAX_NET_BODY_CHARS)


class EngineStepTests(unittest.TestCase):
    """`search_api_via_engine`：拿不到就返回 None（调用方按「这条路没走通」处理）。"""

    URL = "https://a.com/s?q=我"

    def test_with_requests_hands_back_the_captured_requests(self):
        """`page_from_engine(..., with_requests=True)` 要**真的把抓包带回来**。

        这条盯的是那一句 `return html, list(out.get("network") or [])`——
        上面几个测试把它整个换掉了，所以它坏掉也没人知道（变异验证抓到的缺口）。
        """
        from unittest import mock
        from core.jvm_debug import page_from_engine
        fake = {"pages": [{"url": self.URL, "origin": "engine", "html": "<html>页</html>"}],
                "network": [HTML_API], "error": ""}
        with mock.patch("core.jvm_debug.run_jvm_debug", return_value=fake):
            html, entries = page_from_engine(self.URL, with_requests=True)
        self.assertEqual(html, "<html>页</html>")
        self.assertEqual([e["url"] for e in entries], [HTML_API["url"]])
        with mock.patch("core.jvm_debug.run_jvm_debug", return_value=fake):
            self.assertIsInstance(page_from_engine(self.URL), str,
                                  "不带那个开关时仍是老契约（只给 HTML）")

    def test_engine_failure_is_a_none_not_an_exception(self):
        from unittest import mock
        with mock.patch("core.jvm_debug.page_from_engine",
                        side_effect=RuntimeError("引擎不可用")):
            self.assertIsNone(search_api_via_engine("https://a.com/s?q=我", KEYWORD))

    def test_a_picked_api_comes_back_with_its_evidence(self):
        from unittest import mock
        with mock.patch("core.jvm_debug.page_from_engine",
                        return_value=("<html>页面</html>", [HTML_API, NOISE])):
            got = search_api_via_engine("https://a.com/s?q=我", KEYWORD)
        self.assertIsNotNone(got)
        self.assertEqual(got["request"]["url"], HTML_API["url"])
        self.assertTrue(got["why"])
        self.assertTrue(got["rules"]["bookList"])

    def test_no_usable_request_is_a_none(self):
        from unittest import mock
        with mock.patch("core.jvm_debug.page_from_engine",
                        return_value=("<html>页面</html>", [NOISE])):
            self.assertIsNone(search_api_via_engine("https://a.com/s?q=我", KEYWORD))


if __name__ == "__main__":
    unittest.main()
