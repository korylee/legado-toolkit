# -*- coding: utf-8 -*-
"""fetch 的请求头 / 编码 / 代理支持。"""

import unittest
from unittest.mock import patch

from core.fetch import parse_source_header
from core import fetch as F


class ParseSourceHeaderTests(unittest.TestCase):
    def test_json_form(self):
        h, why = parse_source_header('{"User-Agent":"X","Referer":"https://a.com"}')
        self.assertEqual(h["User-Agent"], "X")
        self.assertEqual(h["Referer"], "https://a.com")
        self.assertEqual(why, "")

    def test_line_form(self):
        h, why = parse_source_header("User-Agent: X\nReferer: https://a.com")
        self.assertEqual(h["User-Agent"], "X")
        self.assertEqual(h["Referer"], "https://a.com")
        self.assertEqual(why, "")

    def test_js_header_reported_not_applied(self):
        h, why = parse_source_header('<js>return {"a":"b"}</js>')
        self.assertEqual(h, {})
        self.assertTrue(why)

    def test_empty(self):
        h, why = parse_source_header("")
        self.assertEqual(h, {})
        self.assertEqual(why, "")

    def test_garbage_does_not_raise(self):
        h, why = parse_source_header("这不是 header")
        self.assertEqual(h, {})

    def test_none_means_unset(self):
        """None 等价于「字段未设置」，返回空头且不给原因。"""
        h, why = parse_source_header(None)
        self.assertEqual(h, {})
        self.assertEqual(why, "")

    def test_dirty_strings_do_not_raise(self):
        """半截 JSON / 非对象 JSON / 空 key / 只有冒号等，都不能抛异常。"""
        for raw in ('{"a":', "[1,2]", '"str"', "123", "null", "true",
                    ":", ": value", "A:", "   ", "abc\ndef"):
            with self.subTest(raw=raw):
                h, why = parse_source_header(raw)
                self.assertIsInstance(h, dict)

    def test_half_json_reports_reason(self):
        """半截 JSON 要给出原因，而不是静默当成换行写法丢掉。"""
        h, why = parse_source_header('{"a":')
        self.assertEqual(h, {})
        self.assertTrue(why)

    def test_non_string_does_not_raise(self):
        """非字符串是真实的脏值来源（书源 JSON 里的类型没人保证）。"""
        for bad in (123, 1.5, True, [1, 2], {"a": 1}, b"User-Agent: X"):
            with self.subTest(bad=repr(bad)):
                h, why = parse_source_header(bad)
                self.assertEqual(h, {})
                self.assertTrue(why, "不能静默吞掉：%r" % (bad,))

    def test_bom_json_is_parsed_not_shredded(self):
        """BOM 开头的 JSON 必须先去 BOM。

        不去的话 `startswith("{")` 为 False，会落到换行分隔分支，被解析成
        key='\\ufeff{"a"' / value='"b"}' —— 一个**垃圾头会被真的发给服务器**，
        比丢掉更糟（最终会表现成「源坏了」）。
        """
        h, why = parse_source_header('\ufeff{"User-Agent":"X"}')
        self.assertEqual(h, {"User-Agent": "X"})
        self.assertEqual(why, "")

    def test_bom_after_leading_whitespace_is_parsed(self):
        """BOM 前有空格时同样要剥离。

        lstrip("\\ufeff") 只剥开头**连续**的 BOM：raw 前若带空格，BOM 就不在首位，
        剥不掉 → startswith("{") 为 False → 落进换行分支 → 又是垃圾头，而且这次是
        静默的（why 仍为 ""）。
        """
        h, why = parse_source_header('  \ufeff{"User-Agent":"X"}')
        self.assertEqual(h, {"User-Agent": "X"})
        self.assertEqual(why, "")

    def test_non_object_json_reports_reason(self):
        """不是对象的 JSON 要给原因，不能静默返回空头。"""
        for bad in ('[1, 2]', '"str"', '123', 'null'):
            with self.subTest(bad=bad):
                h, why = parse_source_header(bad)
                self.assertEqual(h, {})
                self.assertTrue(why, "必须说明为什么没用上：%s" % bad)

    def test_value_containing_colon_is_kept(self):
        """换行写法里值本身含冒号（URL 带端口）不能被截断；CRLF 同验。"""
        h, why = parse_source_header(
            "User-Agent: X\r\nReferer: https://a.com:8080/x")
        self.assertEqual(h["User-Agent"], "X")
        self.assertEqual(h["Referer"], "https://a.com:8080/x")
        self.assertEqual(why, "")


class FetchSignatureTests(unittest.TestCase):
    def test_accepts_new_kwargs(self):
        """只验证签名，不发真实请求。"""
        import inspect
        sig = inspect.signature(F.fetch)
        for name in ("headers", "charset", "proxy"):
            self.assertIn(name, sig.parameters)

    def test_signature_defaults_and_order(self):
        """新参数必须都有默认值，且追加在原 timeout 之后——否则既有位置调用会错位。"""
        import inspect
        sig = inspect.signature(F.fetch)
        self.assertEqual(list(sig.parameters), ["url", "timeout", "headers", "charset", "proxy"])
        for name in ("headers", "charset", "proxy"):
            self.assertIsNot(sig.parameters[name].default, inspect.Parameter.empty)
        self.assertEqual(sig.parameters["timeout"].default, 15)


class FetchBehaviorTests(unittest.TestCase):
    """三个卖点的行为覆盖。没有这些用例，整段实现删空也不会有人发现。"""

    def _capture(self, **kw):
        """跑一次 fetch，返回实际发出去的 Request 对象与 opener 是否被用过。"""
        sent = {}
        body = "正文".encode("utf-8")

        class FakeResp:
            def read(self): return body
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_urlopen(req, timeout=None):
            sent["req"] = req
            return FakeResp()

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            F.fetch("https://a.com/x", **kw)
        return sent["req"]

    def test_default_ua_present(self):
        req = self._capture()
        self.assertIn("User-agent", req.headers)
        self.assertEqual(req.headers["User-agent"], F.DEFAULT_UA)

    def test_headers_are_merged(self):
        req = self._capture(headers={"Referer": "https://a.com"})
        self.assertEqual(req.headers["Referer"], "https://a.com")

    def test_empty_header_value_does_not_wipe_default_ua(self):
        """值为空的键不能覆盖默认 UA。用 if v 挡不住纯空白——urllib 发送前会 strip，
        结果服务端收到空 UA。"""
        for empty in ("", "   ", None):
            with self.subTest(empty=repr(empty)):
                req = self._capture(headers={"User-Agent": empty})
                self.assertEqual(req.headers["User-agent"], F.DEFAULT_UA)

    def test_charset_gbk_decodes(self):
        gbk_body = "绍宋".encode("gbk")

        class FakeResp:
            def read(self): return gbk_body
            def __enter__(self): return self
            def __exit__(self, *a): return False

        with patch("urllib.request.urlopen", return_value=FakeResp()):
            html = F.fetch("https://a.com/x", charset="gbk")
        self.assertIn("绍宋", html)

    def test_dirty_charset_does_not_raise(self):
        """charset 与 header 同源，都是书源 JSON 里的脏值。"""
        for bad in (123, ["gbk"], {"a": 1}, "no-such-codec"):
            with self.subTest(bad=repr(bad)):
                html = None
                class FakeResp:
                    def read(self): return "x".encode("utf-8")
                    def __enter__(self): return self
                    def __exit__(self, *a): return False
                with patch("urllib.request.urlopen", return_value=FakeResp()):
                    html = F.fetch("https://a.com/x", charset=bad)
                self.assertEqual(html, "x")

    def test_proxy_uses_opener(self):
        """proxy 非空必须走 build_opener(ProxyHandler)，不能直连。"""
        called = {}

        class FakeOpener:
            def open(self, req, timeout=None):
                called["proxy_opener"] = True
                class R:
                    def read(self): return b"ok"
                    def __enter__(self): return self
                    def __exit__(self, *a): return False
                return R()

        with patch("urllib.request.build_opener", return_value=FakeOpener()) as bo:
            html = F.fetch("https://a.com/x", proxy="http://127.0.0.1:1")
        self.assertTrue(called.get("proxy_opener"), "proxy 非空却没走 opener")
        self.assertTrue(bo.called, "没调用 build_opener")

    def test_no_proxy_uses_urlopen_directly(self):
        """proxy 留空必须直连。"""
        class FakeResp:
            def read(self): return b"ok"
            def __enter__(self): return self
            def __exit__(self, *a): return False

        with patch("urllib.request.urlopen", return_value=FakeResp()) as uo, \
             patch("urllib.request.build_opener") as bo:
            F.fetch("https://a.com/x")
        self.assertTrue(uo.called, "没走直连")
        self.assertFalse(bo.called, "不该建 opener")


if __name__ == "__main__":
    unittest.main()
