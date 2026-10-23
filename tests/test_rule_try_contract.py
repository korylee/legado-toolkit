# -*- coding: utf-8 -*-
"""rule_try 跨语言契约（AGENTS #22⑤）：Python 客户端 ⇄ Kotlin 执行体各改一边时拦住。

判定在执行侧（Kotlin RuleTry），权威常量（VALUE_MAX/VALUES_MAX）与请求键的
权威那份在 Kotlin——Python 靠这些逐词比对钉住自己对齐。"""
from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess
import unittest

from core.rule_try import _gate, _DAEMON_TIMEOUT_SEC, unfaithful_reason

AGSVC = pathlib.Path(__file__).resolve().parents[1] / "appservice" / "test" / "io" / "legado" / "app" / "service"
#: 包装语义要在**真 JS 引擎**里验（`@js:` 在引擎里就是那段式子）。没有 node 就跳过。
NODE = shutil.which("node")


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

    def test_script_needs_page_vars_and_shape_gates_the_rest(self):
        """快验的第一道闸是**跑不跑得了**（`refusal_code`，不跑引擎、不发请求）。

        两半判据都收在 `core.rule_try`：字段族（页面脚本要靠 `page_vars` 合成 `params`
        才验得动）+ 材料 × 规则形态。原先这两半分居 HTTP 端点与 `agent_loop`，同一个
        `webJs` 在两处拿到过**相反**的结论（2026-10-23）。
        """
        from core.rule_try import (UNFAITHFUL_MATERIAL_IS_SCRIPT,
                                   UNFAITHFUL_PAGE_VARS_ABSENT, refusal_code)

        runtime = {"page_vars": {"chapter_images": {"type": "[object Array]", "n": 3,
                                                    "first": "https://a/1.jpg"}}}
        script = "result = params.chapter_images.join('\\n');"
        # 有页面变量就能验；没有就「答不了」——不许拿空 params 跑出一句假的「取不到」
        self.assertEqual(refusal_code("content", "webJs", "rendered_dom", script, runtime), "")
        self.assertEqual(refusal_code("content", "webJs", "rendered_dom", script, {}),
                         UNFAITHFUL_PAGE_VARS_ABSENT)
        # 取值规则走另一半：材料 × 规则形态
        self.assertEqual(refusal_code("content", "content", "webjs_output",
                                      "class.a@text", runtime),
                         UNFAITHFUL_MATERIAL_IS_SCRIPT)
        self.assertEqual(refusal_code("content", "content", "rendered_dom",
                                      "class.a@text", runtime), "")


class SynthParamsTests(unittest.TestCase):
    """页面脚本快验：把 `page_vars` 合成成 `params` 注入规则环境。

    `webJs` 跑在页面环境，而**页面变量的形状**在渲染那次已经采到了。合成后同一段
    脚本在规则环境就能跑——实测 0.03–0.15s，重调整步 30–60s。合成的边界（只回答
    「跑不跑得通」，不回答「内容对不对」）由返回体的 `synthetic` 标记说清。
    """

    def test_array_length_stays_self_consistent(self):
        """数组按 `min(n, 10)` 复制：`length` 与内容**自洽**。

        不虚报长度：脚本若按下标遍历到 131 而只注入了 10 条，会崩在一个不存在的
        下标上——那种错误是假的，会把「能跑通的脚本」判成坏的。
        """
        from core.rule_try import _SYNTH_ITEMS_MAX, synth_params

        got = synth_params({
            "chapter_images": {"type": "[object Array]", "n": 131, "first": "https://a/1.jpg"},
            "few": {"type": "[object Array]", "n": 2, "first": "x"},
            "empty": {"type": "[object Array]", "n": 0, "first": ""},
        })
        self.assertEqual(len(got["chapter_images"]), _SYNTH_ITEMS_MAX)
        self.assertEqual(got["chapter_images"][0], "https://a/1.jpg")
        self.assertEqual(len(got["few"]), 2)
        self.assertEqual(got["empty"], [])

    def test_scalar_types_round_trip(self):
        """字符串 / 数字 / 布尔按样例还原类型——`String(params.x)` 之类的判断才不会走偏。"""
        from core.rule_try import synth_params

        got = synth_params({
            "comic_name": {"type": "[object String]", "first": "测试漫画"},
            "chapter_id": {"type": "[object Number]", "first": "482264"},
            "images_base64": {"type": "[object Boolean]", "first": "false"},
        })
        self.assertEqual(got["comic_name"], "测试漫画")
        self.assertEqual(got["chapter_id"], 482264)
        self.assertIs(got["images_base64"], False)

    def _injected_rule(self, script: str, page_vars: dict) -> dict:
        """跑一遍 `webjs_check`，把**交给引擎的那条规则**截下来。

        引擎是外部边界（socket / 子进程），打桩；这里要验的是我们生成的那段式子。
        """
        from core import rule_try as mod

        seen: dict = {}
        orig = mod.rule_try

        def fake(source, step, body, base_url, rule):
            seen.update(rule=rule, step=step)
            return {"ok": True, "count": 1, "values": ["x"], "error": ""}

        mod.rule_try = fake
        try:
            seen["out"] = mod.webjs_check({}, "content", "body", "http://x/1", script, page_vars)
        finally:
            mod.rule_try = orig
        return seen

    def test_script_reaches_the_engine_unchanged(self):
        """**传输层还原一次之后**，脚本一字不差到达引擎，且返回体标了 `synthetic`。

        `webjs_check` 的字面量比 `json.dumps(script)` 多转一层反斜杠：整条 `@js:` 文本
        在到 Rhino 前会经过一次 JSON 传输，那层把两个反斜杠还原成一个（2026-10-23 实测：
        不补偿时 `join('\\n')` 到引擎里变成真实换行，报 `unterminated string literal`）。
        这里把那条事实**显式写出来**——补偿的那一层正好等于传输层吃掉的那一层。
        """
        script = "if (!params.x) return '';\nresult = params.x.join('\\n');"
        seen = self._injected_rule(script, {"x": {"type": "[object Array]", "n": 2, "first": "a"}})
        rule = str(seen["rule"])
        self.assertTrue(rule.startswith("@js:"), rule[:60])
        # 引擎侧收到的文本 = 传输层还原一次后的样子
        self.assertEqual(json.loads(self._literal_of(rule.replace("\\\\", "\\"))), script)
        self.assertIs(seen["out"].get("synthetic"), True)

    @staticmethod
    def _literal_of(rule: str) -> str:
        """从规则文本里取出那个**脚本字面量**（包装形状变了就在这里红）。"""
        m = re.search(r'\+ ("(?:[^"\\]|\\.)*") \+ \'; return result;\'\)\)\(\)$', rule)
        assert m is not None, "包装形状变了：……%s" % rule[-120:]
        return m.group(1)

    @unittest.skipUnless(NODE, "没有 node：包装语义只能在真 JS 引擎里验（跳过，不算失败）")
    def test_wrapper_semantics_in_a_real_js_engine(self):
        """包装的两个语义：顶层 `return` 合法；脚本没给 `result` 时是 `undefined`。

        后者是**假通过**的来源——裸规则里没回落到 `result` 时会拿到整页 HTML（看起来
        「取到了」）。`@js:` 在引擎里就是这段式子，node 与 Rhino 都按 ES5+ 求值，所以这条
        钉的是我们生成的式子本身，与引擎差异无关；传输层那次还原按上面的实测事实模拟。
        """
        runner = ('const src=require("fs").readFileSync(0,"utf8");'
                  'const v=eval(src);process.stdout.write(JSON.stringify(v===undefined?null:v));')
        cases = (
            # 顶层 return 合法，且 `join('\n')` 拿到真换行（注入的 params 有 2 条样例）
            ("if (!params.x) return '';\nresult = params.x.join('\\n');", "a\na"),
            # 没给 result：undefined —— 不是整页 HTML
            ("var z = 1;", None),
        )
        for script, want in cases:
            rule = str(self._injected_rule(
                script, {"x": {"type": "[object Array]", "n": 2, "first": "a"}})["rule"])
            js = rule.replace("\\\\", "\\")[len("@js:"):]      # 传输层还原一次
            p = subprocess.run([NODE, "-e", runner], input=js, capture_output=True,
                               text=True, errors="replace", timeout=60)
            self.assertEqual(p.returncode, 0, (p.stderr or "")[-200:])
            self.assertEqual(p.stdout.strip(), json.dumps(want), script)


if __name__ == "__main__":
    unittest.main()
