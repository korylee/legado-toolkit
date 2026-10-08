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
        """`class.x` / `id.x` / `tag.a` 是 Legado 简写不是 CSS，必须转。

        转换只认这三种前缀（见 `focus_selector`）：它只是给模型看哪块 DOM 的取景框，
        不需要一个完整的规则解释器——认错的代价是提示词里少一段内容，不是给错规则。
        """
        self.assertEqual(S.focus_selector("class.book-list@tag.li"), ".book-list")
        self.assertEqual(S.focus_selector("id.content@text"), "#content")
        self.assertEqual(S.focus_selector("tag.a@href"), "a")
        self.assertEqual(S.focus_selector(""), "")
        # 已经是 CSS 的原样用（第 1 层的算法候选就是 `.父类 .子类` 这种）
        self.assertEqual(S.focus_selector(".list .item"), ".list .item")
        # `tag.a.x`：tag 前缀下名字取到第一个点为止
        self.assertEqual(S.focus_selector("tag.a.b@href"), "a")

    def test_unknown_focus_falls_back_to_the_whole_page(self):
        """选择器在大纲里不存在时回退整篇，不能报错（这条链跑在请求线程里）。"""
        p = S.build_prompt({"step": "search", "html": HTML, "focus": "class.nothing"})
        self.assertIn("item", p)

    def test_focus_with_no_selector_returns_empty(self):
        """认不出的首段要返回空串，不能崩、也不能硬当成 CSS（`@js:x` / `##正则##` 这类）。

        这些值在真实链路上到不了（第 1 层的候选是 `.类 .类` 形状），但这一层是
        **请求线程里的纯函数**：脏值在这里抛异常 = 用户看到一个 500；而硬当成 CSS
        会让大纲从一棵根本不存在的子树起——那比退回整篇更糟。
        """
        for bad in ("@js:x", "##正则##", "   ", "js:return 1", "<js>x</js>", "$.a.b"):
            with self.subTest(bad=bad):
                self.assertEqual(S.focus_selector(bad), "")


class PreselectTests(unittest.TestCase):
    """程序先挑：拿 App 实测到的值当基准，比**候选自己报的样本**。

    这一层的意义是**省钱**：多数情况一次就对上了，不必调模型。所以「挑不出来」
    的情况必须如实说、不许猜——猜错的代价是用户按着错规则改。
    **这里不跑规则**：候选的样本就是界面上摆给用户看的那几个（`core.candidates`
    造候选时按值分组，两边同一份材料）。
    """

    #: 列表页：两条候选报同一批样本（等价）+ 一条无关的
    CANDS = [
        {"rule": "class.item@tag.a@text", "samples": ["诡秘之主", "绍宋"]},
        {"rule": "class.item@tag.h3@text", "samples": ["诡秘之主", "绍宋"]},
        {"rule": "class.other@text", "samples": ["无关文本"]},
    ]

    def _pick(self, cands=None, **over):
        ctx = {"html": HTML, "step": "search", "source_type": 0, "kind": "text",
               "app_values": ["诡秘之主", "绍宋"], "candidates": cands or self.CANDS}
        ctx.update(over)
        return S.preselect(ctx["candidates"], ctx["app_values"], ctx["html"],
                           ctx["step"], ctx["source_type"], ctx["kind"])

    def test_picks_the_equivalent_simplest_candidate(self):
        """报同一批样本的候选是**等价**的，挑更简洁的那条就行——不算歧义。

        不然「页面上有一堆等价写法」会让这一层形同虚设（实测：两条各 4 分，
        按「不猜」处理就永远轮不到它省钱）。
        """
        out = self._pick()
        self.assertIs(out["need_ai"], False)
        self.assertEqual(out["picked"]["rule"], "class.item@tag.a@text")
        self.assertIn("等价", out["reason"])

    def test_no_basis_means_ask_the_model(self):
        """没有基准就别挑：当前规则自己取到的值是**它的产物**，拿它比等于自证循环。"""
        out = self._pick(app_values=[])
        self.assertIs(out["need_ai"], True)
        self.assertIn("没有 App 实测值", out["reason"])

    def test_nothing_matches_means_ask_the_model(self):
        out = self._pick(cands=[{"rule": "class.other@text",
                                 "samples": ["完全无关"]}])
        self.assertIs(out["need_ai"], True)
        self.assertIn("对不上", out["reason"])

    def test_no_exact_match_is_not_picked(self):
        """只有「包含」没有「完全相等」→ **不挑**。

        实测（2026-10-08，`去读书网`）：引擎那一步的 `values` 是事件流水
        （`⇒开始搜索关键字` / `◇书籍总数:50`），候选样本随便就能撞上几处（当时报「对上
        24 条」），挑出来的规则与 App 取到的东西无关。基线里没有真正的值时，这一层必须
        闭嘴——报一个高分把人引到错的规则上，比不挑更糟。
        """
        out = self._pick(app_values=["⇒开始搜索关键字:我"], cands=[
            {"rule": "class.nav@text", "samples": ["搜索关键"]},
            {"rule": "class.other@text", "samples": ["关键字"]},
        ])
        self.assertIs(out["need_ai"], True, out)
        self.assertIn("完全相等", out["reason"])

    def test_list_candidates_without_samples_fall_back_to_the_page(self):
        """列表族的候选**不报样本**（取到的是节点）——那就拿页面上像 App 实测值的那些比。

        这一支是列表步的主力：App 在列表步取到的正是条目里的书名，而候选是容器规则。
        """
        out = self._pick(cands=[{"rule": "class.list .item", "samples": []}], kind="list")
        self.assertIs(out["need_ai"], False, out)
        self.assertEqual(out["picked"]["rule"], "class.list .item")
        self.assertIn("样本", out["reason"])

    def test_nothing_comparable_on_the_page_says_so_instead_of_failing(self):
        """页面上根本没有像 App 实测值的材料 → 说「比不了」，**不是**「取不到值」。

        AGENTS #4：能力边界与源失效不能混成一句话。
        """
        out = self._pick(html="<html><body><p>无关</p></body></html>",
                         cands=[{"rule": "class.list .item", "samples": []}], kind="list")
        self.assertIs(out["need_ai"], True)
        self.assertIn("没法按样本比", out["reason"])

    def test_different_value_sets_tie_means_ask_the_model(self):
        """都能对上、但报的不是同一批样本 = 真歧义，不替用户猜。"""
        out = self._pick(cands=[
            {"rule": "class.item@tag.a@text", "samples": ["诡秘之主", "绍宋"]},
            {"rule": "class.hot@tag.a@text", "samples": ["诡秘之主", "绍宋", "另一本"]},
        ])
        self.assertIs(out["need_ai"], True)
        self.assertIn("不是同一批样本", out["reason"])

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


class CandidatePatchTests(unittest.TestCase):
    def setUp(self):
        self.source = {
            "bookSourceName": "demo",
            "bookSourceUrl": "https://example.com",
            "header": "secret-header",
            "ruleSearch": {"bookList": ".old", "name": ".name@text"},
            "ruleContent": {"content": ".content@text"},
        }

    def test_patches_one_rule_field_without_mutating_source(self):
        patched = S.patch_candidate(self.source, "ruleSearch.bookList", ".new")
        self.assertEqual(patched["ruleSearch"]["bookList"], ".new")
        self.assertEqual(patched["ruleSearch"]["name"], ".name@text")
        self.assertEqual(patched["ruleContent"], self.source["ruleContent"])
        self.assertEqual(self.source["ruleSearch"]["bookList"], ".old")
        self.assertEqual(patched["header"], "secret-header")

    def test_allows_engine_only_rule_without_local_validation(self):
        patched = S.patch_candidate(self.source, "ruleContent.content", "@js:readRuntime()")
        self.assertEqual(patched["ruleContent"]["content"], "@js:readRuntime()")

    def test_rejects_metadata_and_malformed_fields(self):
        for field in ("bookSourceUrl", "header", "ruleSearch", "other.name", "ruleSearch."):
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    S.patch_candidate(self.source, field, ".x")

    def test_rejects_invalid_source_group_and_rule(self):
        with self.assertRaises(ValueError):
            S.patch_candidate(dict(self.source, ruleSearch="bad"),
                              "ruleSearch.bookList", ".x")
        with self.assertRaises(ValueError):
            S.patch_candidate(self.source, "ruleSearch.bookList", "")
        with self.assertRaises(ValueError):
            S.patch_candidate(self.source, "ruleSearch.bookList", None)


class CandidateEngineVerifyTests(unittest.TestCase):
    SOURCE = {
        "bookSourceUrl": "https://example.com",
        "ruleSearch": {"bookList": ".old"},
    }

    def _runner(self, result, seen=None):
        def run(source, key, timeout):
            if seen is not None:
                seen.update({"source": source, "key": key, "timeout": timeout})
            return result
        return run

    def test_engine_pass_only_marks_target_step_verified(self):
        seen = {}
        result = {"all_ok": False, "steps": [
            {"name": "search", "ok": True, "values": ["甲", "乙"], "detail": "ok"},
            {"name": "toc", "ok": False, "detail": "无目录"},
        ]}
        out = S.verify_candidate(self.SOURCE, "ruleSearch.bookList", ".item",
                                 "search", "demo", timeout=7,
                                 runner=self._runner(result, seen))
        self.assertEqual(out["status"], "verified")
        self.assertEqual(out["engine"]["status"], "pass")
        self.assertEqual(out["engine"]["count"], 2)
        self.assertEqual(seen["key"], "demo")
        self.assertEqual(seen["timeout"], 7)
        self.assertEqual(seen["source"]["ruleSearch"]["bookList"], ".item")
        self.assertEqual(self.SOURCE["ruleSearch"]["bookList"], ".old")

    def test_passing_step_with_zero_field_hits_is_rejected(self):
        """**整步通过 ≠ 这个字段取到了东西**——实测出来的假通过。

        实测（2026-10-08，`去读书网`）：把 `ruleSearch.bookList` 换成
        `.this-class-does-not-exist`，引擎仍报 `ok=True`（搜索步的 verdict 来自事件流：
        `◇书籍总数:N` + 页解析完成，站点自己的搜索页照样出结果），于是界面上
        「验证并应用」把一条**根本取不到书名**的规则判成了 verified。
        字段级的「命中 0 条」是能一眼看出的反证。
        """
        result = {"all_ok": True, "steps": [
            {"name": "search", "ok": True, "values": ["⇒开始搜索"],
             "detail": "该段没有解析完成信号", "notes": ["◇书籍总数:0"]},
        ]}
        out = S.verify_candidate(self.SOURCE, "ruleSearch.bookList",
                                 ".this-class-does-not-exist", "search", "我",
                                 runner=self._runner(result))
        self.assertEqual(out["status"], "rejected")
        self.assertEqual(out["engine"]["status"], "fail")
        self.assertIn("命中 0 条", out["engine"]["reason"])

    def test_engine_fail_rejects_candidate(self):
        result = {"all_ok": False, "steps": [
            {"name": "content", "ok": False, "values": [], "detail": "正文为空"},
        ]}
        out = S.verify_candidate(self.SOURCE, "ruleContent.content", ".content@text",
                                 "content", "https://example.com/c/1",
                                 runner=self._runner(result))
        self.assertEqual(out["status"], "rejected")
        self.assertEqual(out["engine"]["status"], "fail")
        self.assertIn("正文为空", out["engine"]["detail"])

    def test_missing_target_step_is_engine_unavailable(self):
        out = S.verify_candidate(self.SOURCE, "ruleSearch.bookList", ".item",
                                 "search", "demo",
                                 runner=self._runner({"steps": [], "error": "零事件"}))
        self.assertEqual(out["status"], "engine_unavailable")
        self.assertEqual(out["engine"]["status"], "unavailable")
        self.assertIn("零事件", out["engine"]["reason"])

    def test_runner_exception_is_engine_unavailable(self):
        def broken(source, key, timeout):
            raise RuntimeError("JVM 未配置")
        out = S.verify_candidate(self.SOURCE, "ruleSearch.bookList", ".item",
                                 "search", "demo", runner=broken)
        self.assertEqual(out["status"], "engine_unavailable")
        self.assertIn("JVM 未配置", out["engine"]["reason"])

    def test_invalid_target_and_timeout_are_rejected_before_runner(self):
        for args in (("bad", "demo", 60), ("search", "demo", 0)):
            with self.subTest(args=args):
                with self.assertRaises(ValueError):
                    S.verify_candidate(self.SOURCE, "ruleSearch.bookList", ".item",
                                       args[0], args[1], timeout=args[2],
                                       runner=self._runner({}))


class CandidateVerifyRouteTests(unittest.TestCase):
    def _req(self, **over):
        from backend.schemas import CandidateVerifyRequest
        payload = {
            "source": {"bookSourceUrl": "https://example.com",
                       "ruleSearch": {"bookList": ".old"}},
            "field": "ruleSearch.bookList",
            "rule": ".item",
            "target": "search",
            "query": "demo",
        }
        payload.update(over)
        return CandidateVerifyRequest(**payload)

    def test_missing_source_url_is_400_before_engine(self):
        from fastapi import HTTPException
        from backend.api.rules import verify_candidate_rule
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(verify_candidate_rule(self._req(source={})))
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("bookSourceUrl", str(ctx.exception.detail))

    def test_timeout_out_of_range_is_400(self):
        from fastapi import HTTPException
        from backend.api.rules import verify_candidate_rule
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(verify_candidate_rule(self._req(timeout=1)))
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("调试预算", str(ctx.exception.detail))

    def test_route_calls_candidate_verifier_without_push(self):
        from unittest.mock import patch
        from backend.api.rules import verify_candidate_rule
        expected = {"status": "verified", "engine": {"status": "pass"}}
        with patch("core.repair.suggest.verify_candidate", return_value=expected) as verify:
            out = asyncio.run(verify_candidate_rule(self._req()))
        self.assertEqual(out, expected)
        verify.assert_called_once()
        args = verify.call_args.args
        self.assertEqual(args[1:5], ("ruleSearch.bookList", ".item", "search", "demo"))
        self.assertNotIn("push", verify.call_args.kwargs)


class SuggestTests(unittest.TestCase):
    def _run(self, reply, **over):
        ctx = dict(CTX)
        ctx.update(over)
        return asyncio.run(S.suggest(ctx, client=FakeLLM(reply)))

    def test_candidates_come_back_without_local_judgement(self):
        """模型给的候选**只带规则与理由**：这条链不做本地判定（AGENTS #3）。

        有没有用由用户点击后的「验证并应用」（本机引擎）说了算；在这里先判一遍，
        等于用第二套解释器替引擎下结论。
        """
        out = self._run('{"candidates":[{"rule":"class.item@tag.a@text","why":"看着对"},'
                        '{"rule":"@js:return 1","why":"只能这样"}],"reason":"试两条"}')
        self.assertEqual(out["llm"], "ok")
        self.assertEqual(out["reason"], "试两条")
        self.assertEqual([c["rule"] for c in out["candidates"]],
                         ["class.item@tag.a@text", "@js:return 1"])
        for c in out["candidates"]:
            self.assertNotIn("verified", c)
            self.assertNotIn("local", c)
            self.assertNotIn("engine", c)

    def test_result_points_at_the_step(self):
        """候选一律指向**这一步**：`step` 进结论体，界面才知道该把它填回哪个字段。"""
        out = self._run('{"candidates":[{"rule":"id.list@tag.li@tag.a@href"}]}',
                        step="search")
        self.assertEqual(out["candidates"][0]["rule"], "id.list@tag.li@tag.a@href")
        self.assertEqual(out["preselect"]["basis"], "app")

    def test_duplicates_dropped_and_capped(self):
        rules = "".join('{"rule":"r%d"},' % i for i in range(6))
        out = self._run('{"candidates":[%s{"rule":"r0"}]}' % rules)
        self.assertEqual([c["rule"] for c in out["candidates"]], ["r0", "r1", "r2"])

    def test_single_rule_shape_is_accepted(self):
        """模型只给一条 rule（没包 candidates 数组）时也要能用。"""
        out = self._run('{"rule":"class.item@tag.a@text","why":"就这条"}')
        self.assertEqual(len(out["candidates"]), 1)
        self.assertEqual(out["candidates"][0]["why"], "就这条")

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
        """免费那趟**一个模型请求都不发**（它只按候选样本挑一遍 + 登录墙）。

        这条守着「AI 提议必须用户主动」的前半截：换步骤时会自动跑的就是这一趟，
        它一旦开始调模型，就等于自动花了用户的钱。
        """
        llm = FakeLLM('{"candidates":[{"rule":"class.item@tag.a@text"}]}')
        cand = {"rule": "class.item@tag.a@text", "samples": ["诡秘之主", "绍宋"]}
        ctx = dict(CTX, candidates=[cand])
        out = asyncio.run(S.suggest(ctx, client=llm, dry_run=True))
        self.assertEqual(llm.calls, 0)
        self.assertEqual(out["llm"], "dry_run")
        self.assertEqual(out["candidates"], [])
        self.assertEqual(out["preselect"]["picked"]["rule"], "class.item@tag.a@text")

    def test_paid_run_also_carries_the_free_conclusion(self):
        """付费那趟也要带「程序挑的结论」——两个结论摆在一起才看得出该信谁。"""
        cand = {"rule": "class.item@tag.a@text", "samples": ["诡秘之主", "绍宋"]}
        out = self._run('{"candidates":[{"rule":"class.item@tag.a@text"}]}',
                        candidates=[cand])
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
                                               candidates=[{"rule": "class.item@text",
                                                            "samples": ["甲"]}],
                                               kind="text",
                                               enabled_cookie_jar=True, dry_run=True)))
        self.assertEqual(seen["step"], "search")
        self.assertEqual(seen["want_label"], "列表")
        self.assertEqual(seen["field"], "ruleSearch.bookList")
        self.assertEqual(seen["app_values"], ["甲"])
        self.assertEqual(seen["diagnosis"], ["乙"])
        # 免费那趟与登录墙判定都要透传到核心：漏掉任一，「程序先挑」就永远是空的
        self.assertEqual(seen["candidates"], [{"rule": "class.item@text",
                                               "samples": ["甲"]}])
        self.assertEqual(seen["kind"], "text")
        self.assertIs(seen["enabled_cookie_jar"], True)
        self.assertIs(seen["dry_run"], True)


# ---------------------------------------------------------------- 变异记录
# 以下为实测（改坏 → `python -B -m unittest tests.test_suggest` → 确认变红 → 还原）。
#
#  M5  模型给的候选被本地判过（不再只有 rule/why）
#        → test_candidates_come_back_without_local_judgement 红
#  M6  preselect 拿候选自己的值替代样本比对（不再用 samples）
#        → PreselectTests.test_nothing_matches_means_ask_the_model 红
#  M7  没配模型时不标 llm="off"（返回空 candidates 装作没事）
#        → test_no_model_configured_is_off_not_error 红
#  M8  候选数上限失效（不 break）
#        → test_duplicates_dropped_and_capped 红
#  M9  提示词不带 App 实测值
#        → test_prompt_carries_what_the_model_needs 红
#  M10 提示词不带当前表现（`replay_note` 那句）
#        → test_prompt_carries_what_the_model_needs 红
#  M11 大纲挪回最后（稳定内容不再前置 → 前缀缓存吃不到）
#        → test_stable_parts_come_before_the_varying_ones 红
#  M12 大纲忽略 focus（仍从 <html> 走）
#        → test_focus_pulls_the_deep_container_into_the_outline 红
#  M13 focus 不做 Legado 简写转换（class.x 直接当 CSS 用）
#        → test_focus_accepts_legado_shorthand 红
#  M14 token 用量不回传
#        → test_token_usage_is_passed_through 红
#  M15 focus_selector 不挡脏值（`@js:x` 直接当 CSS 用）
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
#  N7  列表候选没样本时直接放弃（不退回页面上像 App 实测值的那些）
#        → PreselectTests.test_list_candidates_without_samples_fall_back_to_the_page 红
#  N8  「页面上比不了」说成「对不上」（能力边界当成规则判负）
#        → PreselectTests.test_nothing_comparable_on_the_page_says_so_instead_of_failing 红
#  N9  preselect 只看「包含」也挑（基线是事件流水时高分挑错规则）
#        → PreselectTests.test_no_exact_match_is_not_picked 红
#  N10 verify_candidate 不看字段级零命中（整步 pass 就判 verified）
#        → CandidateEngineVerifyTests.test_passing_step_with_zero_field_hits_is_rejected 红
#
#  **没覆盖的**：
#    - 真实模型的输出质量（本文件全是假客户端）。提示词改了要手工跑一次真模型看。
#    - `MAX_HTML_CHARS` 截断：要构造一个几十万字符的页面才测得到，性价比低；
#      它挡的是「页面大到解析本身出问题」，属于防御性上限。

if __name__ == "__main__":
    unittest.main()
