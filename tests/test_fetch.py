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
        h, why = parse_source_header('﻿{"User-Agent":"X"}')
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
        """换行写法里值本身含冒号（URL 带端口）不能被截断。"""
        h, why = parse_source_header("Referer: https://a.com:8080/x")
        self.assertEqual(h, {"Referer": "https://a.com:8080/x"})
        self.assertEqual(why, "")


class FetchSignatureTests(unittest.TestCase):
    def test_accepts_new_kwargs(self):
        """只验证签名，不发真实请求。"""
        import inspect
        sig = inspect.signature(F.fetch)
        for name in ("headers", "charset", "proxy"):
            self.assertIn(name, sig.parameters)


if __name__ == "__main__":
    unittest.main()
