# -*- coding: utf-8 -*-
"""校验判定表与 URL 语法解析的测试。

2026-10-18 `core/checker.py` 随消费者并入 `core/jvm_health`（本地校验链退场后
它是唯一消费方），本文件只留仍然活着的判定：
  - `static_rule_complete`（现 core.jvm_health）
  - `build_record` 的 searchUrl 剥 `@` 时序（core.models）
  - `split_url_options` 的 URL 选项语法（core.urls）
  - quality 三态映射、`_normalize_url` 的源身份（署名）语义
退场判定（parse_search_request / build_domain_url / _decode_body 与探测链）
连同它们的测试一起删除——历史与变异记录在 git log。

**三态字段一律用 assertIs / assertIsNone 断言**：`assertFalse(record.content_ok)`
对 False 与 None 同时成立，被测分支生效与否都过——那正是本项目已产出 7 条的假测试形态。
"""

import unittest

from core.jvm_health import static_rule_complete
from core.loader import _normalize_url
from core.models import build_record
from core.urls import split_url_options


class StaticRuleCompletenessTests(unittest.TestCase):
    @staticmethod
    def _source(source_type=0):
        source = {
            "bookSourceType": source_type,
            "searchUrl": "/search?q={{key}}",
            "ruleSearch": {"bookList": ".item", "name": ".name", "bookUrl": ".item@href"},
            "ruleToc": {"chapterList": ".chapter"},
            "ruleContent": {"content": ".content"},
        }
        return source

    def test_required_rules_define_completeness_without_parsing_them(self):
        complete = self._source()
        self.assertIs(static_rule_complete(complete), True)

        for path in (
            ("searchUrl",),
            ("ruleSearch", "bookList"),
            ("ruleSearch", "name"),
            ("ruleSearch", "bookUrl"),
            ("ruleToc", "chapterList"),
            ("ruleContent", "content"),
        ):
            with self.subTest(path=path):
                source = self._source()
                target = source
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = "  "
                self.assertIs(static_rule_complete(source), False)

        download = self._source(source_type=3)
        download["ruleToc"] = {}
        download["ruleContent"] = {}
        self.assertIs(static_rule_complete(download), True)

    def test_unsupported_source_type_is_not_mislabelled_incomplete(self):
        self.assertIs(static_rule_complete(self._source(source_type=4)), None)
        self.assertIs(static_rule_complete(None), None)


class BuildRecordSearchUrlTests(unittest.TestCase):
    """`searchUrl` 的 `@` 后缀必须**先剥、再判 `has_search`**。

    顺序写反的后果（实测 **252 条**）：`@js:` 形态的 searchUrl 整段都落在 `@`
    之后，剥完是空串——而 `has_search` 是在剥之前算的，于是留下
    「有搜索规则、模板却是空」的**矛盾态**。checker 拿这个空模板去拼
    `domain + "/"`，**把站点首页当搜索页打**：白费一次请求，而且结论无从解释。

    修法只是把剥挪到判定之前——不用新增字段，也不是「特殊处理 @js:」。
    """

    def test_js_search_url_counts_as_no_search(self):
        """回放不了的搜索规则不能算「有搜索」。"""
        rec = build_record({"bookSourceUrl": "https://a.com",
                            "searchUrl": "@js:return 'https://a.com/s?q=' + key"}, 0)
        self.assertEqual(rec.search_url_template, "")
        self.assertFalse(rec.has_search,
                         "模板是空的却报 has_search=True → 探测会去打首页")

    def test_at_post_suffix_is_stripped_but_keeps_the_template(self):
        """反向断言：`@POST` 形态**要保住模板**——别把上一条的修法扩大化。"""
        rec = build_record({"bookSourceUrl": "https://a.com",
                            "searchUrl": "https://a.com/s?q={{key}}@POST"}, 0)
        self.assertEqual(rec.search_url_template, "https://a.com/s?q={{key}}")
        self.assertTrue(rec.has_search)

    def test_plain_search_url_is_untouched(self):
        """反向断言：不带 `@` 的普通 searchUrl（3800+ 条）一个字符都不能动。"""
        rec = build_record({"bookSourceUrl": "https://a.com",
                            "searchUrl": "https://a.com/s?q={{key}}"}, 0)
        self.assertTrue(rec.has_search)
        self.assertEqual(rec.search_url_template, "https://a.com/s?q={{key}}")


class UrlOptionTests(unittest.TestCase):
    """Legado 的 URL 选项语法 `url,{json}`——`searchUrl` 里 43% 的源在用。

    切法必须照抄 `AnalyzeUrl.kt:776`：

        val paramPattern: Regex = Regex("\s*,\s*(?=\{)")

    即**切第一个** `,{`（逗号前后可有空白），不是最后一个——URL 的查询串里
    完全可能出现 `,{`。切错了的后果不是「少传个参数」，是**整个请求变形**。

    **为什么要修**：选项没人解析时，整段 JSON 被留在 URL 里用 GET 发出去——
    请求是**畸形**的，而且与 App 的行为不一致（`AnalyzeUrl` 就是按这套语法发的）。
    实测选项分布：method 1167 / body 1141 / charset 627 / headers 53。

    ⚠️ **别把「相关」当「因果」——这是我在这条上犯过的错**：带该语法的源搜索
    命中率 **1.0%（17/1674）**、不带的是 **20.5%（446/2175）**，差 20 倍，看着
    像「就是它造成的」（我当时甚至写了「它解释了那 1612 条 3★ 仅规则」）。
    修完之后抽样做 A/B（25 个源，新旧行为对照）——只**救回 3 个**、还**弄坏 1 个**
    （净 +2）。那 20 倍的差距主要是**混淆**：用这套语法的源本身就偏 API 型/较新/
    已失效，跟语法无关。

    **结论**：修它是因为它畸形、且与 App 不一致，**不是因为它能救回 43%**。
    """

    def test_splits_url_and_options(self):
        url, opt = split_url_options('https://a.com/s,{"method":"POST"}')
        self.assertEqual(url, "https://a.com/s")
        self.assertEqual(opt, {"method": "POST"})

    def test_splits_at_the_first_comma_brace_not_the_last(self):
        """**切第一个**，不是最后一个。

        切最后一个的话 URL 里会残留一段 `,{...}` —— 请求就变形了。这里故意让
        「第一个」与「最后一个」不同，**只断言 URL 干净**（不要求选项可解析：
        切第一个之后剩下的那截本来就不是合法 JSON，Legado 也当没有选项）。
        """
        url, _opt = split_url_options('https://a.com/s?q=x,{"a":1},{"method":"POST"}')
        self.assertEqual(url, "https://a.com/s?q=x")

    def test_whitespace_around_the_comma_is_allowed(self):
        """`,` 与 `{` 之间可以有空白——实测语料里的 JSON 是多行缩进的。"""
        url, opt = split_url_options('https://a.com/s  ,\n  {"method": "POST"}')
        self.assertEqual(url, "https://a.com/s")
        self.assertEqual(opt["method"], "POST")

    def test_no_options_returns_the_whole_thing(self):
        self.assertEqual(split_url_options("https://a.com/s?q=1"),
                         ("https://a.com/s?q=1", {}))

    def test_broken_json_still_cuts_the_url(self):
        """JSON 解不出来时**照样切**、选项当空——这条是**对齐 App**，不是随手。

        Legado 先按 `paramPattern` 切出 `urlNoOption` 并拿去发请求，之后才去解析
        选项；解析失败只是不应用选项，**URL 已经被切了**（`AnalyzeUrl.kt:219-231`）。
        我们跟着切，才是同一个行为。
        """
        url, opt = split_url_options('https://a.com/s,{"body": "id"="x"}')
        self.assertEqual(url, "https://a.com/s")
        self.assertEqual(opt, {})


class JudgeMappingTests(unittest.TestCase):
    """quality 的三态映射必须落到既有的 None/True/False 语义上。"""

    def test_mapping_is_stable(self):
        from core import quality as Q
        self.assertIs(Q.Judgement(Q.VERDICT_PASS).checker_state, True)
        self.assertIs(Q.Judgement(Q.VERDICT_FAIL).checker_state, False)
        self.assertIsNone(Q.Judgement(Q.VERDICT_UNKNOWN).checker_state)


class NormalizeUrlIdentityTests(unittest.TestCase):
    """探测用的根 URL 要剥 `#署名`，**源身份 key 必须保留署名**——方向相反。

    署名不同即两个源，与 Legado 的 getSourceKey() 一致。两处用途不同，
    别为了"统一"把它们改成一样。
    """

    def test_signature_is_kept(self) -> None:
        self.assertEqual(_normalize_url("https://a.com##签名"),
                         "https://a.com##签名")
        self.assertNotEqual(_normalize_url("https://a.com##甲"),
                            _normalize_url("https://a.com##乙"))


# ------------------------------------------------------------------ 仍适用的变异记录
#
# BuildRecordSearchUrlTests：
#  M40  把剥 `@` 挪回 `has_search` 判定**之后**（= 还原原来的顺序）
#         → test_js_search_url_counts_as_no_search 红
#         （这就是那 252 条的全部成因：一行顺序。两条反向断言在 M40 下保持绿——
#           它们守的是「别把修法扩大化」，方向相反）
#
# UrlOptionTests：
#  M37  `split_url_options` 切**最后一个** `,{`（而不是第一个）
#         → test_splits_at_the_first_comma_brace_not_the_last 红
#  M39  JSON 解析失败时**不切 URL**（返回原串）
#         → test_broken_json_still_cuts_the_url 红（连带第一条也红——同一处）


if __name__ == "__main__":
    unittest.main()
