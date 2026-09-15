# -*- coding: utf-8 -*-
"""fetch 的请求头 / 编码 / 代理支持。"""

import unittest

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

    def test_non_string_inputs_do_not_raise(self):
        """header 字段可能来自脏 JSON（数字 / 列表 / 对象），绝不能抛异常。"""
        for raw in (123, 1.5, True, [1, 2], {"a": 1}, b"User-Agent: X"):
            with self.subTest(raw=raw):
                h, why = parse_source_header(raw)
                self.assertEqual(h, {})
                # 脏值要说明原因，不能静默吞掉
                self.assertTrue(why, "非字符串 header 必须给出原因")

    def test_dirty_strings_do_not_raise(self):
        """半截 JSON / 非对象 JSON / 空 key / 只有冒号等，都不能抛异常。"""
        for raw in ('{"a":', "[1,2]", '"str"', "123", "null", "true",
                    ":", ": value", "A:", "   ", "abc\ndef"):
            with self.subTest(raw=raw):
                h, why = parse_source_header(raw)
                self.assertIsInstance(h, dict)

    def test_line_form_survives_crlf_and_colon_in_value(self):
        """Windows 换行与值内冒号（URL 端口）必须解析正确。"""
        h, why = parse_source_header(
            "User-Agent: X\r\nReferer: https://a.com:8080/x")
        self.assertEqual(h["User-Agent"], "X")
        self.assertEqual(h["Referer"], "https://a.com:8080/x")
        self.assertEqual(why, "")

    def test_half_json_reports_reason(self):
        """半截 JSON 要给出原因，而不是静默当成换行写法丢掉。"""
        h, why = parse_source_header('{"a":')
        self.assertEqual(h, {})
        self.assertTrue(why)


class FetchSignatureTests(unittest.TestCase):
    def test_accepts_new_kwargs(self):
        """只验证签名，不发真实请求。"""
        import inspect
        sig = inspect.signature(F.fetch)
        for name in ("headers", "charset", "proxy"):
            self.assertIn(name, sig.parameters)


if __name__ == "__main__":
    unittest.main()
