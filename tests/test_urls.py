# -*- coding: utf-8 -*-
"""`core.urls`：Legado 的 `url,{json}` 选项解析与「规则产出的 URL」规范化。"""

import unittest

from core.urls import rule_url, split_url_options


class SplitUrlOptionsTests(unittest.TestCase):
    def test_splits_url_and_options(self):
        url, opt = split_url_options('/x/1.html,{"webView":true}')
        self.assertEqual(url, "/x/1.html")
        self.assertEqual(opt, {"webView": True})

    def test_no_options(self):
        self.assertEqual(split_url_options("/x/1.html"), ("/x/1.html", {}))

    def test_unparsable_json_still_cuts_the_url(self):
        """对齐 App：先按 paramPattern 切出 urlNoOption，之后才解析选项——
        解析失败只是不应用选项，URL 已经被切了。"""
        url, opt = split_url_options("/x/1.html,{not json}")
        self.assertEqual(url, "/x/1.html")
        self.assertEqual(opt, {})


class RuleUrlTests(unittest.TestCase):
    """规则产出的 URL → 可直接请求的地址。

    选项里装的是 **App 才懂的东西**（webView / method / body）。带上它去发请求
    必然拿不到页面：实测 `.../1.html,{"webView":true}` 返回 **404**，同一个地址
    剥掉选项后返回 **200**。改前 `_probe_content` 因此把
    `正文请求失败(status=404)` 写进 checks，读起来像**源坏了**，其实是我们的
    地址拼错了（AGENTS #4：不能把我们的问题说成源的问题）。
    """

    BASE = "https://www.koudaimh.com/manhua/haizeiwang-LgnWd"

    def test_strips_webview_option(self):
        self.assertEqual(
            rule_url(self.BASE, '/manhua/haizeiwang-LgnWd/1.html,{"webView":true}'),
            "https://www.koudaimh.com/manhua/haizeiwang-LgnWd/1.html")

    def test_strips_other_options_too(self):
        """`method` / `body` 同理——只认 webView 会留下一半的坑。"""
        self.assertEqual(
            rule_url("https://a.com/", '/s,{"method":"POST","body":"k={{key}}"}'),
            "https://a.com/s")

    def test_relative_and_absolute(self):
        # 相对地址按 urljoin 语义补全：base 的最后一段被当**文件名**而不是目录。
        # 这与 App 的 `URL(base, spec)`（NetworkUtils.getAbsoluteURL）一致，
        # 所以 base 本身有没有尾斜杠会改变结果——这里把行为钉住，别按直觉改。
        self.assertEqual(rule_url(self.BASE, "1.html"),
                         "https://www.koudaimh.com/manhua/1.html")
        self.assertEqual(rule_url(self.BASE + "/", "1.html"), self.BASE + "/1.html")
        # 库里章节链接实测都是 `/manhua/...` 这种绝对路径 → 不受上面影响
        self.assertEqual(rule_url(self.BASE, "/manhua/haizeiwang-LgnWd/2.html"),
                         "https://www.koudaimh.com/manhua/haizeiwang-LgnWd/2.html")
        self.assertEqual(rule_url(self.BASE, "https://o.com/a.html"), "https://o.com/a.html")
        self.assertEqual(rule_url(self.BASE, "//o.com/a.html"), "https://o.com/a.html")

    def test_empty(self):
        self.assertEqual(rule_url(self.BASE, ""), "")


if __name__ == "__main__":
    unittest.main()
