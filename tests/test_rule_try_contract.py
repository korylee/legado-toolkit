# -*- coding: utf-8 -*-
"""rule_try 跨语言契约（AGENTS #22⑤）：Python 客户端 ⇄ Kotlin 执行体各改一边时拦住。

判定在执行侧（Kotlin RuleTry），权威常量（VALUE_MAX/VALUES_MAX）与请求键的
权威那份在 Kotlin——Python 靠这些逐词比对钉住自己对齐。"""
from __future__ import annotations

import pathlib
import unittest

from core.rule_try import _gate, _DAEMON_TIMEOUT_SEC

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


if __name__ == "__main__":
    unittest.main()
