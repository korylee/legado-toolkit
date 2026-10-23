# -*- coding: utf-8 -*-
"""rule_try 跨语言契约（AGENTS #22⑤）：Python 客户端 ⇄ Kotlin 执行体各改一边时拦住。

判定在执行侧（Kotlin RuleTry），权威常量（VALUE_MAX/VALUES_MAX）与请求键的
权威那份在 Kotlin——Python 靠这些逐词比对钉住自己对齐。"""
from __future__ import annotations

import pathlib
import unittest

from core.rule_try import _gate, _DAEMON_TIMEOUT_SEC, unfaithful_reason

AGSVC = pathlib.Path(__file__).resolve().parents[1] / "appservice" / "test" / "io" / "legado" / "app" / "service"


def _read(name: str) -> str:
    return (AGSVC / name).read_text(encoding="utf-8")


class RuleTryContractTests(unittest.TestCase):

    def test_request_keys_match_what_kotlin_reads(self):
        """Python 发的请求键 = Kotlin daemon 读的键（逐词）：漂一个键就是
        「参数读不到 → 当空串」的静默失败（AGENTS #4）。"""
        daemon = _read("AppServiceDaemon.kt")
        for key in ("op", "file", "body_path", "step", "rule", "base_url"):
            self.assertIn('req.str("%s")' % key, daemon,
                          "AppServiceDaemon 没读请求键 %s——Python 侧发了也白发" % key)
        self.assertIn('"op": "rule_try"', pathlib.Path(
            __file__).resolve().parents[1].joinpath(
            "core", "rule_try.py").read_text(encoding="utf-8"))

    def test_kotlin_ops_list_contains_rule_try(self):
        """daemon 的 ping 应答 ops 列表必须含 rule_try：Python 的探活/分派靠它。"""
        self.assertIn('"rule_try"', _read("AppServiceDaemon.kt"))

    def test_value_limits_match_the_kotlin_side(self):
        """VALUE_MAX / VALUES_MAX 两侧一致：Kotlin 产出、Python 闸门兜底，
        数字漂了不会有报错，只会「样本被无声截短」。"""
        kt = _read("RuleTry.kt")
        self.assertIn("const val VALUE_MAX = 200", kt)
        self.assertIn("const val VALUES_MAX = 5", kt)

    def test_launcher_reads_the_try_args_keys(self):
        """一次性路径：launcher 读的 try_* 键 = Python _gradle_fallback 写的键。"""
        launcher = _read("DebugServiceLauncher.kt")
        for key in ("try_body", "try_step", "try_rule", "try_base"):
            self.assertIn('getProperty("%s")' % key, launcher,
                          "launcher 没读 %s——Gradle 回落路径会静默拿到空参数" % key)
        self.assertIn('"--try"', _read("DebugService.kt"))

    def test_gate_shape(self):
        """应答形状闸门：ok 必须 bool、values 必须字符串列表——形状不对返回 None
        （调用方如实上报，不静默当失败也不静默放行）。"""
        good = {"ok": True, "values": ["a", "b"], "count": 2, "error": ""}
        self.assertEqual(_gate(good)["count"], 2)
        self.assertEqual(_gate(good)["values"], ["a", "b"])
        self.assertIsNone(_gate({"ok": "yes", "values": []}))
        self.assertIsNone(_gate({"ok": True, "values": [1, 2]}))
        self.assertIsNone(_gate(None))
        self.assertIsNone(_gate([1]))
        # 超长截断与条数上限
        clipped = _gate({"ok": True, "values": ["x" * 500] * 8, "count": 8})
        self.assertEqual(len(clipped["values"]), 5)
        self.assertLessEqual(len(clipped["values"][0]), 200)

    def test_timeout_budget_is_daemon_scale(self):
        """daemon 路径是纯内存解析（秒级）；预算给成分钟级会把卡死藏半分钟。"""
        self.assertLessEqual(_DAEMON_TIMEOUT_SEC, 60)


class UnfaithfulReasonTests(unittest.TestCase):
    """快验的忠实性判据（材料种类 × 规则形态）：答不了就给码，不给判决。

    为什么要有它：快验**只有否决权**（AGENTS #3/D1）。判据不在的话，材料是脚本输出
    时一条选择器规则会「取到 0 条」（看起来像源坏了），材料是 DOM 时读 `result` 的规则
    拿整页 HTML 当 result 用（看起来像成了）——两种都会把用户带向反的方向（AGENTS #4）。
    """

    def test_reason_follows_material_and_rule_shape(self):
        from core.rule_try import (UNFAITHFUL_MATERIAL_IS_SCRIPT,
                                   UNFAITHFUL_RESULT_ABSENT, UNFAITHFUL_RULE_EMPTY,
                                   unfaithful_reason)

        cases = (
            # (材料种类, 规则, 期望码)
            ("rendered_dom", "class.a@text", ""),                      # 选择器要 DOM ✓
            ("static_html", "class.a@href", ""),
            ("webjs_output", "class.a@text", UNFAITHFUL_MATERIAL_IS_SCRIPT),
            ("webjs_output", '@js:result.split("\n")', ""),            # 读 result 要脚本产出 ✓
            ("rendered_dom", '@js:result.split("\n")', UNFAITHFUL_RESULT_ABSENT),
            ("rendered_dom", "@js:java.ajax(url)", ""),                # 不读 result：与材料无关
            # 说不清就照跑：侧车只有最后一次渲染的事实，非末页永远 unknown，
            # 一律拒绝会把整条 AI 验证链变成「待验收」
            ("unknown", "class.a@text", ""),
            ("unknown", "@js:result", UNFAITHFUL_RESULT_ABSENT),
            # 我们另抓的静态页：浏览器没参与，result 不存在 → 读 result 的规则拦掉
            ("fetched_html", "@js:result", UNFAITHFUL_RESULT_ABSENT),
            ("fetched_html", "class.a@text", ""),
            ("", "class.a@text", ""),
            ("rendered_dom", "", UNFAITHFUL_RULE_EMPTY),
        )
        for material_kind, rule, want in cases:
            self.assertEqual(unfaithful_reason(material_kind, rule), want,
                             "%r × %r 判据变了" % (material_kind, rule))

    def test_unknown_material_stays_permissive(self):
        """认不出的种类（对端升级 / 空串）不阻塞：宁可多试一次。

        这条兜底的方向是判据的一部分，不是实现细节——反过来会让「对端多了一种材料」
        变成「这一步什么都做不了」。
        """
        from core.jvm_debug import MATERIAL_FETCHED, MATERIAL_WEBJS_OUTPUT
        from core.material import blocked_next, can_fast_verify, can_fill_dom

        for kind in ("brand_new", ""):
            self.assertTrue(can_fill_dom(kind), kind)
            self.assertEqual(blocked_next(kind), "", kind)
            self.assertTrue(can_fast_verify(kind, "selector"), kind)
        # 反方向：材料事实明确时不许再「宽松」——脚本输出拦选择器、另抓的静态页要渲染
        self.assertFalse(can_fast_verify(MATERIAL_WEBJS_OUTPUT, "selector"))
        self.assertEqual(blocked_next(MATERIAL_FETCHED), "render_first")

    def test_field_kind_gates_fast_verify(self):
        """字段族也能否掉快验（与材料无关）：页面脚本不走单值口径。

        判据在 `NO_FAST_VERIFY_KINDS`，消费方是 `/rule-try` 端点；这里钉「按字段算出来的
        族」对得上这张表——只钉表里那两个字面量的话，改坏 `FILL_SPECS` 的 `kind` 也不会红。
        """
        from core.autofill import _field_kind
        from core.material import NO_FAST_VERIFY_KINDS

        for step, field, kind in (("content", "webJs", "script"),
                                  ("content", "content", "text"),
                                  ("content", "nextContentUrl", "text"),
                                  ("bookUrl", "name", "text")):
            self.assertEqual(_field_kind(step, field), kind, (step, field))
        self.assertIn(_field_kind("content", "webJs"), NO_FAST_VERIFY_KINDS)


if __name__ == "__main__":
    unittest.main()
