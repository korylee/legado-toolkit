# -*- coding: utf-8 -*-
"""调试抽屉「AI 提议一步规则」：提示词、回放验证与三态结果（全程离线）。

关键边界只有一条：**模型只写规则文本，能不能用由回放器判**。所以这里盯的是
「本地验不了」和「规则不对」必须**分开**——前者是 `@js:` 那类我们跑不了的规则
（只能连 App 试），后者是在这份页面上取不到值。混成一句「没取到值」，用户会去
改一条其实只是我们验不了的规则。
"""

import asyncio
import unittest

from core.repair import suggest as S

#: `a` 嵌在 `h3` 里：`.item@tag.a@text` 与 `.item@tag.h3@text` 会取到**同一批值**，
#: 「等价候选」那条用例就靠这个形状（取不回同一批值就测不到等价分支）
HTML = """
<div class="wp">
  <ul id="list">
    <li class="item"><h3><a href="/b/1">诡秘之主</a></h3></li>
    <li class="item"><h3><a href="/b/2">绍宋</a></h3></li>
  </ul>
</div>
"""

CTX = {
    "html": HTML, "step": "search", "rule": ".old-list",
    "step_label": "搜索", "want_label": "搜索结果列表", "field": "ruleSearch.bookList",
    "replay_note": "规则命中 0 个节点",
    # 「App 独有样本」**故意不写进 HTML**：写进去的话，它也会出现在 DOM 大纲里，
    # 「值有没有进提示词」这条断言就变成恒真（实测：变异 9 逃过了一次）
    "app_values": ["诡秘之主", "App 独有样本"],
    "diagnosis": ["页面上没有目标类型的节点"],
    "source_type": 0,
}


class FakeLLM:
    enabled = True

    class _Cfg:
        model = "fake-model"

    config = _Cfg()

    def __init__(self, reply):
        self.reply = reply
        self.calls = 0
        self.seen = None

    async def chat(self, system, user, session=None, temperature=None):
        self.calls += 1
        self.seen = (system, user)
        return self.reply


class PromptTests(unittest.TestCase):
    def test_prompt_carries_what_the_model_needs(self):
        p = S.build_prompt(CTX)
        for want in ("搜索", "搜索结果列表", "ruleSearch.bookList", ".old-list",
                     "规则命中 0 个节点", "App 独有样本", "没有目标类型的节点",
                     "ul #list"):     # DOM 大纲里 id 写作 `#list`（与 dom_outline 一致）
            self.assertIn(want, p)

    def test_prompt_survives_missing_optional_parts(self):
        """脏/缺字段不能让拼提示词炸掉——它跑在请求线程里。"""
        p = S.build_prompt({"step": "content", "html": ""})
        self.assertIn("content", p)
        S.build_prompt({"app_values": [None, "", "  "], "diagnosis": [None]})

    def test_stable_parts_come_before_the_varying_ones(self):
        """**顺序是约束，不是排版**：前缀缓存按前缀命中，稳定内容必须在易变内容之前。

        改回「当前规则 → 表现 → 诊断 → 大纲」那一版，这条就红——那时的后果是
        同一个页面上每次提问都全量计费（实测过一次）。
        """
        p = S.build_prompt(CTX)
        outline = p.index("【页面 DOM 大纲")
        values = p.index("【App 实测取到的值")
        rule = p.index("【当前规则】")
        diag = p.index("【已知的问题】")
        self.assertLess(outline, rule)
        self.assertLess(values, rule)
        self.assertLess(rule, diag)


class OutlineFocusTests(unittest.TestCase):
    """大纲的起点：从**命中的容器**起，而不是从 `<html>` 起。

    两条理由都是实测出来的：① `dom_outline` 的 max_depth=6，真实站点的列表容器常在
    `#app > .container > .row > .col > ul` 这种深度，从根走**到不了它**（外层多包
    4 层就完全看不见）；② 从容器起那 90 行全是相关内容（页面框架实测占掉三成）。
    """

    #: 目标容器在 8 层深处 —— 从根出大纲够不到
    DEEP = ("<html><body><div class='a'><div class='b'><div class='c'><div class='d'>"
            "<ul class='book-list'><li class='item'><a href='/b/1'>书名一</a></li>"
            "<li class='item'><a href='/b/2'>书名二</a></li></ul>"
            "</div></div></div></div></body></html>")

    def test_focus_pulls_the_deep_container_into_the_outline(self):
        p = S.build_prompt({"step": "search", "html": self.DEEP, "focus": "class.book-list"})
        self.assertIn("book-list", p)
        self.assertIn("书名一", p)

    def test_without_focus_a_deep_container_is_missing(self):
        """反向保护：不带 focus 时**确实**看不到目标——这条守着上面那条的意义。"""
        p = S.build_prompt({"step": "search", "html": self.DEEP})
        self.assertNotIn("book-list", p)

    def test_focus_accepts_legado_shorthand(self):
        """`class.x` / `id.x` / `tag.a` 是 Legado 简写不是 CSS，必须转（用回放器那份解析）。"""
        self.assertEqual(S.focus_selector("class.book-list@tag.li"), ".book-list")
        self.assertEqual(S.focus_selector("id.content@text"), "#content")
        self.assertEqual(S.focus_selector("tag.a@href"), "a")
        self.assertEqual(S.focus_selector(""), "")

    def test_unknown_focus_falls_back_to_the_whole_page(self):
        """选择器在大纲里不存在时回退整篇，不能报错（这条链跑在请求线程里）。"""
        p = S.build_prompt({"step": "search", "html": HTML, "focus": "class.nothing"})
        self.assertIn("item", p)

    def test_focus_with_no_selector_returns_empty(self):
        """解析不出首段时要返回空串，不能索引崩（`@js:x` / `##正则##` 这类都是空 steps）。

        这些值在真实链路上到不了（第 1 层的候选与表单里的规则都是可回放的规则），
        但这一层是**请求线程里的纯函数**：脏值在这里抛异常 = 用户看到一个 500。
        """
        for bad in ("@js:x", "##正则##", "   ", "js:return 1"):
            with self.subTest(bad=bad):
                self.assertEqual(S.focus_selector(bad), "")


class PreselectTests(unittest.TestCase):
    """程序先挑：拿 App 实测到的值当基准，比每条候选取到的东西。

    这一层的意义是**省钱**：多数情况一次就对上了，不必调模型。所以「挑不出来」
    的三种情况必须如实说、不许猜——猜错的代价是用户按着错规则改。
    """

    #: 列表页：两条等价的候选（a 在 h3 里）+ 一条无关的
    CANDS = ["class.item@tag.a@text", "class.item@tag.h3@text", "class.other@text"]

    def _pick(self, cands=None, **over):
        ctx = {"html": HTML, "step": "search", "source_type": 0,
               "app_values": ["诡秘之主", "绍宋"], "candidates": cands or self.CANDS}
        ctx.update(over)
        return S.preselect(ctx["candidates"], ctx["app_values"], ctx["html"],
                           ctx["step"], ctx["source_type"])

    def test_picks_the_equivalent_simplest_candidate(self):
        """取到同一批值的候选是**等价**的，挑更简洁的那条就行——不算歧义。

        不然「页面上有一堆等价写法」会让这一层形同虚设（实测：两个候选各 4 分，
        按「不猜」处理就永远轮不到它省钱）。
        """
        out = self._pick()
        self.assertIs(out["need_ai"], False)
        self.assertEqual(out["picked"]["rule"], "class.item@tag.a@text")
        self.assertIn("等价", out["reason"])

    def test_no_basis_means_ask_the_model(self):
        """没有基准就别挑：本地回放取到的值是**当前这条坏规则**的产物，自证循环。"""
        out = self._pick(app_values=[])
        self.assertIs(out["need_ai"], True)
        self.assertIn("没有 App 实测值", out["reason"])

    def test_nothing_matches_means_ask_the_model(self):
        out = self._pick(cands=["class.other@text"])
        self.assertIs(out["need_ai"], True)
        self.assertIn("对不上", out["reason"])

    def test_unreplayable_candidate_is_not_picked(self):
        out = self._pick(cands=["@js:return 1"])
        self.assertIs(out["need_ai"], True)
        self.assertIn("只能连 App 试", out["reason"])

    def test_different_value_sets_tie_means_ask_the_model(self):
        """都能对上、但取的不是同一批值 = 真歧义，不替用户猜。

        两条候选**同分**（各对上 2 条）才叫并列；分数不同走的是「单一命中」那条路。
        所以这里的 fixture 要让第二条多取一个（分相同、值不同）。
        """
        html = ('<ul class="list"><li class="item"><a href="/b/1">诡秘之主</a>'
                '<a href="/b/2">绍宋</a></li></ul>'
                '<div class="hot"><a href="/b/1">诡秘之主</a><a href="/b/2">绍宋</a>'
                '<a href="/b/3">另一本</a></div>')
        out = self._pick(cands=["class.item@tag.a@text", "class.hot@tag.a@text"], html=html)
        self.assertIs(out["need_ai"], True)
        self.assertIn("不是同一批值", out["reason"])

    def test_ranked_is_sorted_and_carries_no_values(self):
        """排名要按分数降序；**全量取值不外发**（正文那类一次几万字）。"""
        out = self._pick()
        scores = [r["score"] for r in out["ranked"]]
        self.assertEqual(scores, sorted(scores, reverse=True))
        for r in out["ranked"]:
            self.assertNotIn("values", r)
            self.assertNotIn("sig", r)


class LoginWallTests(unittest.TestCase):
    """登录墙：模型看到的页面不是 App 看到的那份（App 带登录态），先说清楚。

    判定表在 core/checker，这里只验接线——`enabled_cookie_jar` 忘了传，
    「200 + 登录词」那一档就永远不生效（「请登录」在正常页面导航栏里太常见）。
    """

    WALL = "<html><body>请先登录后查看内容<input type='password'></body></html>"

    def test_login_markers_need_cookie_jar(self):
        self.assertIs(S.login_wall(self.WALL, True), True)
        self.assertIs(S.login_wall(self.WALL, False), False)

    def test_normal_page_is_not_a_wall(self):
        self.assertIs(S.login_wall(HTML, True), False)

    def test_dirty_html_does_not_raise(self):
        for bad in (None, "", 123, ["x"]):
            with self.subTest(bad=bad):
                self.assertIs(S.login_wall(bad, True), False)


class VerifyTests(unittest.TestCase):
    def test_replayable_rule_is_verified_with_values(self):
        v = S.verify(HTML, "class.item@tag.a@text", "search")
        self.assertIs(v["verified"], True)
        self.assertEqual(v["count"], 2)
        self.assertEqual(v["samples"][0], "诡秘之主")

    def test_js_rule_is_not_verified_and_says_connect_the_app(self):
        v = S.verify(HTML, "@js:return doc.select('.item')", "search")
        self.assertIs(v["verified"], False)
        self.assertIn("只能连 App 试", v["note"])

    def test_no_match_is_not_verified(self):
        v = S.verify(HTML, "class.nothing@tag.a@text", "search")
        self.assertIs(v["verified"], False)
        self.assertIn("取不到值", v["note"])
        self.assertEqual(v["rule_error"], "")


class SuggestTests(unittest.TestCase):
    def _run(self, reply, **over):
        ctx = dict(CTX)
        ctx.update(over)
        return asyncio.run(S.suggest(ctx, client=FakeLLM(reply)))

    def test_candidates_are_verified_one_by_one(self):
        out = self._run('{"candidates":[{"rule":"class.item@tag.a@text","why":"看着对"},'
                        '{"rule":"@js:return 1","why":"只能这样"}],"reason":"试两条"}')
        self.assertEqual(out["llm"], "ok")
        self.assertEqual(out["reason"], "试两条")
        first, second = out["candidates"]
        self.assertIs(first["verified"], True)
        self.assertEqual(first["count"], 2)
        # 验不了的那条要**单独**标出来，而不是和「不对」混成一句
        self.assertIs(second["verified"], False)
        self.assertIn("只能连 App 试", second["note"])

    def test_result_points_at_the_step(self):
        """候选必须用**这一步**的语义回放：搜索是列表、正文是取值，判定不同。"""
        out = self._run('{"candidates":[{"rule":"id.list@tag.li@tag.a@href"}]}',
                        step="search")
        # 列表步：选到 2 条 → 通过
        self.assertIs(out["candidates"][0]["verified"], True)
        out2 = self._run('{"candidates":[{"rule":"id.list@tag.li"}]}', step="content")
        # 同一条规则放在正文档：取到的是 <li> 元素文本，仍算取到值
        self.assertIs(out2["candidates"][0]["verified"], True)

    def test_duplicates_dropped_and_capped(self):
        rules = "".join('{"rule":"r%d"},' % i for i in range(6))
        out = self._run('{"candidates":[%s{"rule":"r0"}]}' % rules)
        self.assertEqual([c["rule"] for c in out["candidates"]], ["r0", "r1", "r2"])

    def test_single_rule_shape_is_accepted(self):
        """模型只给一条 rule（没包 candidates 数组）时也要能用。"""
        out = self._run('{"rule":"class.item@tag.a@text","why":"就这条"}')
        self.assertEqual(len(out["candidates"]), 1)
        self.assertIs(out["candidates"][0]["verified"], True)

    def test_bad_json_is_an_error_state_not_an_exception(self):
        out = self._run("我不是 JSON")
        self.assertEqual(out["llm"], "error")
        self.assertEqual(out["candidates"], [])
        self.assertIn("不是合法 JSON", out["error"])

    def test_no_model_configured_is_off_not_error(self):
        class Off:
            enabled = False
            config = None
        out = asyncio.run(S.suggest(dict(CTX), client=Off()))
        self.assertEqual(out["llm"], "off")
        self.assertIn("模型", out["error"])

    def test_llm_exception_becomes_error_state(self):
        class Boom(FakeLLM):
            async def chat(self, *a, **k):
                raise RuntimeError("连接被拒绝")

        out = asyncio.run(S.suggest(dict(CTX), client=Boom(None)))
        self.assertEqual(out["llm"], "error")
        self.assertIn("连接被拒绝", out["error"])

    def test_empty_candidates_is_reported_not_silent(self):
        out = self._run('{"candidates":[]}')
        self.assertEqual(out["candidates"], [])
        self.assertTrue(out["error"], "一条都没给时必须说清，不能显示成空白成功")

    def test_token_usage_is_passed_through(self):
        """用量要回传（含缓存命中数）：不然「这次花了多少、缓存吃到没有」只能猜。"""
        class WithUsage(FakeLLM):
            async def chat(self, *a, **k):
                self.last_usage = {"prompt_tokens": 1234, "prompt_cache_hit_tokens": 900}
                return await super().chat(*a, **k)

        out = asyncio.run(S.suggest(dict(CTX), client=WithUsage('{"candidates":[]}')))
        self.assertEqual(out["usage"]["prompt_tokens"], 1234)
        self.assertEqual(out["usage"]["prompt_cache_hit_tokens"], 900)

    def test_usage_is_empty_when_provider_gives_none(self):
        """客户端没这个属性（假客户端/别的 provider）时给空字典，不能让前端拿到 undefined。"""
        out = self._run('{"candidates":[]}')
        self.assertEqual(out["usage"], {})

    def test_dry_run_sends_no_model_request(self):
        """免费那趟**一个模型请求都不发**（它只跑本地挑选 + 登录墙）。

        这条守着「AI 提议必须用户主动」的前半截：换步骤时会自动跑的就是这一趟，
        它一旦开始调模型，就等于自动花了用户的钱。
        """
        llm = FakeLLM('{"candidates":[{"rule":"class.item@tag.a@text"}]}')
        ctx = dict(CTX, candidates=["class.item@tag.a@text"])
        out = asyncio.run(S.suggest(ctx, client=llm, dry_run=True))
        self.assertEqual(llm.calls, 0)
        self.assertEqual(out["llm"], "dry_run")
        self.assertEqual(out["candidates"], [])
        self.assertEqual(out["preselect"]["picked"]["rule"], "class.item@tag.a@text")

    def test_paid_run_also_carries_the_free_conclusion(self):
        """付费那趟也要带「程序挑的结论」——两个结论摆在一起才看得出该信谁。"""
        out = self._run('{"candidates":[{"rule":"class.item@tag.a@text"}]}',
                        candidates=["class.item@tag.a@text"])
        self.assertIsNotNone(out["preselect"])
        self.assertIn("login_wall", out)


class SuggestRouteTests(unittest.TestCase):
    """接口层：缺页面要 400，没配模型不是 HTTP 错误。"""

    def _req(self, **kw):
        from backend.schemas import SuggestRuleRequest

        payload = {"html": HTML, "step": "search", "rule": ".old-list",
                   "step_label": "搜索", "want_label": "列表"}
        payload.update(kw)
        return SuggestRuleRequest(**payload)

    def test_missing_html_is_400(self):
        from fastapi import HTTPException

        from backend.api.rules import suggest_rule

        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(suggest_rule(self._req(html="")))
        self.assertEqual(ctx.exception.status_code, 400)

    def test_ctx_reaches_the_core(self):
        from unittest.mock import patch

        from backend.api.rules import suggest_rule

        seen = {}

        async def fake_suggest(ctx, client=None, temperature=None, dry_run=False):
            seen.update(ctx)
            seen["dry_run"] = dry_run
            return {"candidates": [], "llm": "ok", "error": "", "reason": ""}

        with patch("core.repair.suggest.suggest", side_effect=fake_suggest):
            asyncio.run(suggest_rule(self._req(app_values=["甲"], diagnosis=["乙"],
                                               field="ruleSearch.bookList",
                                               candidates=["class.item@text"],
                                               enabled_cookie_jar=True, dry_run=True)))
        self.assertEqual(seen["step"], "search")
        self.assertEqual(seen["want_label"], "列表")
        self.assertEqual(seen["field"], "ruleSearch.bookList")
        self.assertEqual(seen["app_values"], ["甲"])
        self.assertEqual(seen["diagnosis"], ["乙"])
        # 免费那趟与登录墙判定都要透传到核心：漏掉任一，「程序先挑」就永远是空的
        self.assertEqual(seen["candidates"], ["class.item@text"])
        self.assertIs(seen["enabled_cookie_jar"], True)
        self.assertIs(seen["dry_run"], True)


# ---------------------------------------------------------------- 变异记录
# 以下为实测（改坏 → `python -B -m unittest tests.test_suggest` → 确认变红 → 还原）。
#
#  M5  候选不做回放验证（一律标 verified=True）
#        → test_candidates_are_verified_one_by_one 红
#  M6  verify 里 `if rule_error` 改成恒假（「本地验不了」与「取不到值」合流）
#        → test_js_rule_is_not_verified_and_says_connect_the_app 红
#  M7  没配模型时不标 llm="off"（返回空 candidates 装作没事）
#        → test_no_model_configured_is_off_not_error 红
#  M8  候选数上限失效（不 break）
#        → test_duplicates_dropped_and_capped 红
#  M9  提示词不带 App 实测值
#        → test_prompt_carries_what_the_model_needs 红
#  M10 提示词不带当前回放结论
#        → test_prompt_carries_what_the_model_needs 红
#  M11 大纲挪回最后（稳定内容不再前置 → 前缀缓存吃不到）
#        → test_stable_parts_come_before_the_varying_ones 红
#  M12 大纲忽略 focus（仍从 <html> 走）
#        → test_focus_pulls_the_deep_container_into_the_outline 红
#  M13 focus 不做 Legado 简写转换（class.x 直接当 CSS 用）
#        → test_focus_accepts_legado_shorthand 红
#  M14 token 用量不回传
#        → test_token_usage_is_passed_through 红
#  M15 focus_selector 对空 steps 直接索引（脏值抛 IndexError）
#        → test_focus_with_no_selector_returns_empty 红
#  N1  preselect 不拿 App 值比对（分数恒 0）
#        → PreselectTests.test_picks_the_equivalent_simplest_candidate 红
#  N2  等价候选也不挑（同分就退回让模型看）
#        → PreselectTests.test_picks_the_equivalent_simplest_candidate 红
#  N3  preselect 没有基准也照样挑
#        → PreselectTests.test_no_basis_means_ask_the_model 红
#  N4  dry_run 仍然调模型（免费那趟变成花钱那趟）
#        → test_dry_run_sends_no_model_request 红
#  N5  login_wall 不接线（恒 False）
#        → LoginWallTests.test_login_markers_need_cookie_jar 红
#  N6  路由不透传 enabled_cookie_jar
#        → SuggestRouteTests.test_ctx_reaches_the_core 红
#
#  **没覆盖的**：
#    - 真实模型的输出质量（本文件全是假客户端）。提示词改了要手工跑一次真模型看。
#    - `MAX_HTML_CHARS` 截断：要构造一个几十万字符的页面才测得到，性价比低；
#      它挡的是「页面大到解析本身出问题」，属于防御性上限。

if __name__ == "__main__":
    unittest.main()
