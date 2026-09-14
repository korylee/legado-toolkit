# -*- coding: utf-8 -*-
"""Legado 规则回放器单元测试。"""

import unittest

from core.rules import replayer as R

HTML = """
<div class="book-list">
  <li class="item"><h3 class="name"><a href="/book/1">测试书</a></h3>
    <img data-original="/img/1.jpg"><span class="author">作者：张三</span></li>
  <li class="item"><h3 class="name"><a href="/book/2">第二本</a></h3>
    <img data-original="/img/2.jpg"><span class="author">作者：李四</span></li>
</div>
<div class="pager"><a href="/p/1">1</a><a href="/p/2">2</a><a href="/p/3">3</a></div>
"""

JSONTEXT = "{\"data\":{\"list\":[{\"name\":\"A\",\"url\":\"/a\"},{\"name\":\"B\",\"url\":\"/b\"}]}}"



class ShorthandTests(unittest.TestCase):
    def test_class_and_tag_prefix(self):
        self.assertEqual(R.extract_all(HTML, "class.item@tag.a@href"), ["/book/1", "/book/2"])
        self.assertEqual(R.extract_all(HTML, "class.book-list@tag.h3@tag.a@text"), ["测试书", "第二本"])

    def test_plain_css(self):
        self.assertEqual(R.extract_all(HTML, ".book-list li a@href"), ["/book/1", "/book/2"])

    def test_data_original_attr(self):
        self.assertEqual(R.extract_all(HTML, "class.book-list@tag.img@data-original"),
                         ["/img/1.jpg", "/img/2.jpg"])


class RegexTests(unittest.TestCase):
    def test_strip_prefix(self):
        self.assertEqual(R.extract_all(HTML, "class.author@text##作者：##"), ["张三", "李四"])

    def test_backreference(self):
        rule = r"class.item@tag.a@href##/book/(\d+)##book-$1"
        self.assertEqual(R.extract_all(HTML, rule), ["book-1", "book-2"])

    def test_no_match_keeps_original(self):
        self.assertEqual(R.extract_all(HTML, "class.author@text##不存在##"), ["作者：张三", "作者：李四"])


class IndexTests(unittest.TestCase):
    def test_last_and_first(self):
        self.assertEqual(R.extract_all(HTML, "class.pager@tag.a.-1@text"), ["3"])
        self.assertEqual(R.extract_all(HTML, "class.pager@tag.a.0@text"), ["1"])

    def test_out_of_range(self):
        self.assertEqual(R.extract_all(HTML, "class.pager@tag.a.9@text"), [])



class ListFieldTests(unittest.TestCase):
    def test_parse_list_then_fields(self):
        nodes, err = R.parse_list(HTML, "class.book-list li")
        self.assertEqual(err, "")
        self.assertEqual(len(nodes), 2)
        self.assertEqual(R.parse_field_first(nodes[0], "tag.h3@tag.a@text"), "测试书")
        self.assertEqual(R.parse_field_first(nodes[0], "tag.a@href"), "/book/1")
        self.assertEqual(R.parse_field_first(nodes[0], "tag.img@data-original"), "/img/1.jpg")
        self.assertEqual(R.parse_field_first(nodes[0], "class.author@text##作者：##"), "张三")


class JsonPathTests(unittest.TestCase):
    def test_list_and_relative_field(self):
        nodes, err = R.parse_list(JSONTEXT, "$.data.list[*]")
        self.assertEqual(err, "")
        self.assertEqual(len(nodes), 2)
        self.assertEqual([R.parse_field_first(n, "$.name") for n in nodes], ["A", "B"])

    def test_nested_extract(self):
        self.assertEqual(R.extract_all(JSONTEXT, "$.data.list[*].name"), ["A", "B"])

    def test_json_prefix(self):
        self.assertEqual(R.extract_all(JSONTEXT, "@json:$.data.list[*].url"), ["/a", "/b"])

    def test_recursive_descent_unsupported(self):
        ok, why = R.rule_supported("$..name")
        self.assertFalse(ok)
        self.assertIn("递归下降", why)



class UnsupportedTests(unittest.TestCase):
    def test_js_xpath_alternatives_reported_not_silent(self):
        for rule in ("@js:result", "<js>var a=1</js>", "@xpath://div", "class.a@text||class.b@text"):
            ok, why = R.rule_supported(rule)
            self.assertFalse(ok, rule)
            self.assertTrue(why, rule)

    def test_empty_rule(self):
        ok, _ = R.rule_supported("")
        self.assertFalse(ok)


class ImageHeuristicTests(unittest.TestCase):
    def test_image_ratio(self):
        self.assertGreater(R.image_ratio(["https://a.com/1.jpg", "https://a.com/2.webp"]), 0.9)
        self.assertLess(R.image_ratio(["第一章 正文", "第二章 正文"]), 0.1)

    def test_looks_like_image_rule(self):
        self.assertTrue(R.looks_like_image_rule("class.content@tag.img@src"))
        self.assertTrue(R.looks_like_image_rule("class.pic@data-original"))
        self.assertFalse(R.looks_like_image_rule("id.content@text"))


if __name__ == "__main__":
    unittest.main()
