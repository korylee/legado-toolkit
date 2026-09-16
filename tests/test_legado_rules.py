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

    def test_fourth_segment_replaces_only_the_first_match(self):
        """`##正则##替换###` 的第四段 = **只替换第一个匹配**。

        依据 Legado `AnalyzeRule.kt:760-770`：

            val ruleStrS = rule.split("##")
            if (ruleStrS.size > 2) replacement  = ruleStrS[2]
            if (ruleStrS.size > 3) replaceFirst = true

        这个形式以前被整体报成 unsupported（「暂未实现」）——**安全，但没有结论**，
        实测语料里约 825 条规则卡在这。现在实现它：同一个值里有两处命中时只动第一处。
        """
        self.assertEqual(
            R.extract_all(HTML, "class.book-list@text##作者：(.)##X###"),
            ["测试书 X三 第二本 作者：李四"])

    def test_third_segment_still_replaces_all(self):
        """反向断言：没有第四段时仍然是**全部替换**。

        少了这条，把 `count=1` 写成这两个分支的默认值也会全绿——而三段式
        （`##正则##替换`）才是主力用法，实测语料里的绝大多数 `##` 规则都是它。
        """
        self.assertEqual(
            R.extract_all(HTML, "class.book-list@text##作者：(.)##X"),
            ["测试书 X三 第二本 X四"])

    def test_fourth_segment_with_no_match_returns_empty(self):
        """第四段形式**找不到匹配时返回空串**，不是原样保留。

        Legado 的 `replaceRegex` 是两支（`AnalyzeRule.kt:487-497`），

            if (rule.replaceFirst) {
                val match = regex.find(result)
                return if (match != null) match.value.replaceFirst(regex, replacement) else ""
            } else {
                return result.replace(regex, replacement)
            }

        「不匹配则原样返回」**只对三段式成立**——`_apply_regex` 原来的 docstring
        写「不匹配则原样保留，与 Legado 一致」，那句话在这里是错的。

        复刻它意味着：一条 `##...###` 规则若正则不命中，这一格就是空的
        （在报告里可能表现为判失败）。这是**有意与 App 对齐**，不是回归。
        """
        self.assertEqual(R.extract_all(HTML, "class.book-list@text##不存在##X###"), [""])
        # 对照组：同样不命中，三段式必须原样保留
        self.assertEqual(R.extract_all(HTML, "class.book-list@text##不存在##X"),
                         ["测试书 作者：张三 第二本 作者：李四"])


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

    def test_legado_only_syntax_reported(self):
        """Legado 支持、我们回放不了的语法，必须报 unsupported 而不是静默跑空。"""
        rules = [
            "@@class.a@text",              # 强制 jsoup
            "@webjs:return document.body",  # 注入 WebView
            "class.a@text&&class.b@text",   # && 合并
            "class.a@text%%class.b@text",   # %% 按索引交替
            "tag.div[2:5]",                 # 区间索引
            "tag.div[0:10:2]",              # 区间索引（带步长）
            "$.data.list$1",                # $n 取列表第 n 项
            "class.a@text@get:{name}",      # 变量读取
        ]
        for rule in rules:
            ok, why = R.rule_supported(rule)
            self.assertFalse(ok, "应判为不支持：%s" % rule)
            self.assertTrue(why, "必须给出原因：%s" % rule)

    def test_supported_syntax_not_affected(self):
        """反向断言：新检测不能误伤本来能跑通的规则。"""
        for rule in ("class.a@tag.b@text", "class.item@href", "@css:class.a@text",
                     "$.data.list[*].name", "id.content@text##广告##",
                     "id.content@text##广告##替换",
                     "id.content@text##广告##x###",   # ## 第四段：2026-09-16 起已实现
                     "class.a@text", "text", "class.list@tag.li"):
            ok, why = R.rule_supported(rule)
            self.assertTrue(ok, "被误伤：%s (%s)" % (rule, why))

    def test_js_in_middle_reports_js_reason(self):
        """`selector@js:code` 的 JS 体里出现 $1/&&/@get: 时，报的原因必须是 JS。

        真实语料里这类错报确实存在（刻意不写具体条数——它随统计口径而变）。结论（unknown）本来就对，
        但如果 JS 检测排在新检测段后面，报出的原因会变成「$n 取列表第 n 项
        暂未支持」——把用户引向错误的方向，而这个工具的全部价值就是告诉他
        为什么。断言必须检查**原因内容**，否则这类错报不会被任何用例发现。
        """
        rules = [
            r"text@js:result.replace(/^(\d+)章/,'第$1章')",
            "id.c@text@js:return result + '&&'",
            "id.c@text@js:java.get('x')@get:{y}",
        ]
        for rule in rules:
            ok, why = R.rule_supported(rule)
            self.assertFalse(ok, rule)
            self.assertIn("JS", why,
                          "原因必须指向 JS 而不是别的 token：%s -> %s" % (rule, why))

    def test_template_braces_reported(self):
        """{{}} 模板按 JS 求值，回放不了——以前它不被识别。

        这条是**真正在防误杀**：假 supported 会让规则被当 CSS 跑出空结果，
        于是判「源坏了」（红）。必须归 unknown（灰）。
        """
        for rule in ("{{$.name}}", "class.a@text{{$.tags}}", "{{@@.top@h1@text}}"):
            ok, why = R.rule_supported(rule)
            self.assertFalse(ok, rule)
            self.assertIn("模板", why, "%s -> %s" % (rule, why))

    def test_template_reported_before_fourth_segment(self):
        """多个 {{...}} 里的 ## 会被跨串计数误判成四段式，必须先报模板。

        `raw.count("##")` 是跨整串计数的：这条规则里有 3 个 `##`，但它们
        全部落在两个 `{{...}}` 内部，根本没有四段式——只有把 `{{` 检测排在
        前面，报出的原因才是对的。
        """
        rule = "标签：{{$.tags##换行##,}} 简介：{{$.intro##免责声明：|，.*}}"
        ok, why = R.rule_supported(rule)
        self.assertFalse(ok, rule)
        self.assertIn("模板", why, "应报模板而不是四段式：%s" % why)


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
        # 整串断言，不用 assertIn：错选到根节点时整份 JSON 串同样含 "list"，
        # 弱断言会照样通过
        self.assertEqual(hits[0], '{"list": [{"name": "A", "url": "/a"}, '
                                  '{"name": "B", "url": "/b"}]}')

    def test_unsupported_rule_returns_reason(self):
        _vals, hits, err = self._nodes(HTML, "@js:result")
        self.assertTrue(err)
        self.assertEqual(hits, [])

    def test_empty_rule(self):
        _vals, hits, err = self._nodes(HTML, "")
        self.assertTrue(err)
        self.assertEqual(hits, [])

    def test_at_html_prefix_is_not_a_legado_rule(self):
        """`@html:` **不是** Legado 的规则前缀——整份响应体不再是「命中内容」。

        Legado 只认 `@CSS:` / `@@` / `@XPath:` / `@Json:`（`AnalyzeRule.kt:603-618`），
        全仓库没有 `@html:`。我们曾把它当作「返回整份响应体」的合法规则，
        于是**在 App 里跑不出东西的规则，被我们判成「有正文」**——典型的误放。

        **别和 `@html` 搞混**：没有冒号的那个是**取值动作**，Legado 支持，
        库里 2863 条规则在用，必须照旧。区别就在那个冒号。

        实测（2026-09-16）：库里以 `@html:` 开头的规则 **0 条**，任意位置出现也是 0——
        所以删掉它是零影响的。这条测试锁的是「以后别再把它加回来」。
        """
        self.assertEqual(R.extract_all(HTML, "@html:"), [],
                         "整份响应体不该再被当成命中内容")

    def test_html_value_action_still_works(self):
        """反向断言：`@html`（**无冒号**）是取值动作，必须照旧可用。

        少了这条，为了删前缀把 `html` 从 VALUE_ACTIONS 里一起删掉也会全绿——
        而那是 2863 条规则在用的东西。
        """
        vals = R.extract_all(HTML, "class.item@html")
        self.assertTrue(vals)
        self.assertIn("<a href=\"/book/1\">", vals[0])


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------- 变异记录
# 以下为实测（改坏 → `python -B -m unittest tests.test_legado_rules` → 确认变红 → 还原）。
#
#  **2026-09-16 起本文件唯一有变异记录的一段：`##` 第四段（只替换第一个）的实现。**
#
#  M8  不命中时返回原文（`else ""` → `else s`）——**有意偏离 Legado 的那条路口**
#        → test_fourth_segment_with_no_match_returns_empty 红
#        （这条变异值得单列：它是「对齐 App」与「别让源集体翻红」两个原则的
#          分界点，改回原文而不加断言的话，两种行为在测试上完全看不出差别）
#  M9  忽略 `count=1`（回到全部替换）
#        → test_fourth_segment_replaces_only_the_first_match 红
#  M10 第四段判定写成 `len(parts) > 4`（差一段）
#        → 上面两条都红
#
#  M27 去掉「末段取值动作优先」（`class.a@tag.p@html` 又被当成「再选 html 元素」）
#        → test_html_value_action_still_works 红
#        这条守的是**误杀**方向：实测 2566 条规则末段用 @html，其中 2131 条是
#        `ruleContent.content`。不修的话，一跑 probe_depth=3，1737 个源的正文
#        会被判成「不可用」——而它们的规则其实是对的。
#  M28  把 `@html:` 前缀加回来
#        ⚠️ **第一次只还原了前缀，测试全绿**——因为那处改动是三个地方一起删的
#        （`RULE_PREFIXES` 的映射、`parse_rule` 里 `kind == "html"` 的早退、
#        `_root_of` 的 `kind == "html"` 分支）。只还原一处复现不出原行为，
#        等于没测。**三处一起还原**后才红。
#        （与 M18/M19 同型：多处改动只还原一部分，变异就是无效的。）
#
#  反向断言 `test_third_segment_still_replaces_all` 在 M8/M9/M10 下**全绿**——
#  它守的是「别把三段式也改成只替第一处」，方向相反，本就不该被这三条变异触发。
