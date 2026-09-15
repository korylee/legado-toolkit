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


class ExtractAllNodesTests(unittest.TestCase):
    """命中片段：规则选中的 DOM 块的 outerHTML。"""

    #: 测试固定的证据预算。真实口径归 core.quality 所有
    #: （MATCHED_NODES_LIMIT / MAX_MATCHED_HTML_CHARS），replayer 不设默认值
    LIMIT = 3
    MAX_CHARS = 200_000

    def _nodes(self, content, rule, **kw):
        """统一传参入口：replayer 故意不设默认值，这里补上测试口径。"""
        kw.setdefault("limit", self.LIMIT)
        kw.setdefault("max_chars", self.MAX_CHARS)
        return R.extract_all_nodes(content, rule, **kw)

    def test_content_rule_returns_matched_block(self):
        vals, hits, err = self._nodes(HTML, "class.book-list@tag.li@tag.a@text")
        self.assertEqual(err, "")
        self.assertEqual(vals, ["测试书", "第二本"])
        # 必须**逐字**断言：命中的是属性取值动作**之前**的那个 <a>。
        # 用 assertIn("/book/1", hits[0]) 是不够的——父节点 <li> 的 outerHTML
        # 同样含 /book/1，实现若错选到祖先，那种弱断言照样通过，
        # 而「选到哪一块」正是本次改动的全部价值。
        self.assertEqual(hits[0], '<a href="/book/1">测试书</a>')
        self.assertEqual(hits[1], '<a href="/book/2">第二本</a>')

    def test_hits_are_capped(self):
        _vals, hits, _err = self._nodes(HTML, "class.book-list@tag.li", limit=1)
        self.assertEqual(len(hits), 1)

    def test_hits_truncated_by_max_chars(self):
        _vals, hits, _err = self._nodes(HTML, "class.book-list@tag.li", max_chars=10)
        # 先钉住条数：若实现返回空列表，下面的循环会变成空转的假覆盖
        self.assertEqual(len(hits), 2)
        for h in hits:
            self.assertEqual(len(h), 10)
            self.assertTrue(h.startswith("<li class="))

    def test_json_leaf_hits_equal_values(self):
        """JSON 字符串叶子下 hits 与 values 相同——调用方需自行去重。

        这是已知语义：字符串叶子经 _json_to_text 原样返回，UI 上会出现两份
        一样的内容。补这条用例把该行为固定下来，避免日后被当成 bug 修。
        """
        vals, hits, err = self._nodes(JSONTEXT, "$.data.list[*].name")
        self.assertEqual(err, "")
        self.assertEqual(vals, ["A", "B"])
        self.assertEqual(hits, vals)

    def test_json_object_hit_is_serialized(self):
        """选中 dict 节点时 hits 是该节点的 JSON 串，不是空。"""
        _vals, hits, err = self._nodes(JSONTEXT, "$.data")
        self.assertEqual(err, "")
        self.assertEqual(len(hits), 1)
        self.assertIn("list", hits[0])

    def test_unsupported_rule_returns_reason(self):
        _vals, hits, err = self._nodes(HTML, "@js:result")
        self.assertTrue(err)
        self.assertEqual(hits, [])

    def test_empty_rule(self):
        _vals, hits, err = self._nodes(HTML, "")
        self.assertTrue(err)
        self.assertEqual(hits, [])

    def test_html_rule_returns_raw_response(self):
        """@html: 分支不做任何选择，整份响应体就是命中内容。

        该分支此前无任何测试保护（变异测试证实：把它改成永假，原有测试全绿）。
        """
        vals, hits, err = self._nodes(HTML, "@html:")
        self.assertEqual(err, "")
        self.assertEqual(vals, [HTML])
        self.assertEqual(hits, [HTML])


if __name__ == "__main__":
    unittest.main()
