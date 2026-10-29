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


class OutlineFocusTests(unittest.TestCase):
    """大纲的起点：从**命中的容器**起，而不是从 `<html>` 起。

    理由仍是实测的：从容器起那 90 行全是相关内容（页面框架实测占掉三成）。

    **深度不再是问题**：`dom_outline` 会把「只有唯一子元素」的包装链折成一行
    （`a > b > c` 按一层算、标出真实层级），所以 8 层深处的容器从根也够得到
    （2026-10-23 起；在此之前这条限制是 focus 的主要理由）。
    """

    #: 目标容器在 8 层深处 —— 折叠后从根也够得到
    DEEP = ("<html><body><div class='a'><div class='b'><div class='c'><div class='d'>"
            "<ul class='book-list'><li class='item'><a href='/b/1'>书名一</a></li>"
            "<li class='item'><a href='/b/2'>书名二</a></li></ul>"
            "</div></div></div></div></body></html>")

    def test_focus_pulls_the_deep_container_into_the_outline(self):
        p = S._outline(self.DEEP, "class.book-list")
        self.assertIn("book-list", p)
        self.assertIn("书名一", p)

    def test_single_child_chains_no_longer_hide_deep_containers(self):
        """不带 focus 也能看到深层容器：单链折叠把包装层压成一行。

        这条替换了原来那条「不带 focus 就看不到」的反向断言——那条钉的是
        `max_depth=6` 的限制，而折叠已经把这个限制移除了（它的价值现在只剩
        「从目标起出大纲更省行数」，不再承担「够不够得到」）。
        """
        p = S._outline(self.DEEP)
        self.assertIn("book-list", p)
        # 容器**本身**可见（这是这条测试要钉的）；条目内的文本按同构折叠只留形状，
        # 那是有意的：2 条同构的 `li` 画成 `×2 同构` 就够写选择器了
        self.assertIn("×2 同构", p)

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
        p = S._outline(HTML, "class.nothing")
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
        return S.preselect(ctx["candidates"], ctx["app_values"], ctx["html"], ctx["kind"])

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

    判定表在 core.jvm_health，这里只验接线——`enabled_cookie_jar` 忘了传，
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
