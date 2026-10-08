# -*- coding: utf-8 -*-
"""在途调试的观测口：登记、相位推进、以及 `GET /rules/debug-status` 的返回形状。

为什么值得钉住：调试是**同步长轮询**（`/rules/jvm-debug` 不跑完不返回），等待期界面
上没有任何产物可读——这个口是那段空白里唯一的信息源，而它的两个消费者（后端路由与
前端 `utils/debugRun`）靠**字段名**对接：字段一改，界面会静默退回「已等待 N 秒」，
看起来像「进度功能没做」，而不是像坏了。

这里只钉**观测契约**：登记了什么、没登记时怎么答、lane 的持有者从哪来。不钉 UI 文案
（中文取词在前端，`frontend/src/utils/debugRun.js`）。
"""

import asyncio
import unittest

from backend.api import rules
from backend.jobs import runner


class ActiveRunRegistryTests(unittest.TestCase):
    def tearDown(self) -> None:
        # 模块级登记表：本用例登记过的 id 自己摘掉，别串到别的用例
        for rid in ("r1", "r2"):
            runner._finish_active_run(rid)

    def test_unregistered_run_is_not_an_error(self) -> None:
        """没传 run_id / 已收尾 → None：界面因此说「已等待 N 秒」而不是报错。"""
        self.assertIsNone(runner.active_run_snapshot(""))
        self.assertIsNone(runner.active_run_snapshot("never-registered"))

    def test_phase_starts_queued_and_advances_to_starting(self) -> None:
        runner.capture_active_run("r1", lane="jvm")
        first = runner.active_run_snapshot("r1")
        self.assertEqual(first["phase"], "queued")
        self.assertEqual(first["run_id"], "r1")
        self.assertGreaterEqual(first["elapsed_ms"], 0)
        # lane 现状是**既有事实**（不另造一份状态）：没别人占用时持有者为空
        self.assertEqual(first["lane_holder"], "")
        self.assertEqual(first["lane_waiting"], 0)

        runner.note_active_run("r1", "starting")
        second = runner.active_run_snapshot("r1")
        self.assertEqual(second["phase"], "starting")
        # phase_ms 是**当前相位**的时长，界面靠它决定「拉起引擎」要不要换成
        # 「引擎执行中」。它必须与 elapsed_ms 分开——前面可能排了很久的队
        self.assertLessEqual(second["phase_ms"], second["elapsed_ms"])

    def test_late_note_does_not_resurrect_a_finished_run(self) -> None:
        """收尾之后迟到的推进不得凭空造出一条永远「运行中」的假状态。"""
        runner.capture_active_run("r2", lane="jvm")
        runner._finish_active_run("r2")
        runner.note_active_run("r2", "starting")
        self.assertIsNone(runner.active_run_snapshot("r2"))

    def test_lane_holder_comes_from_the_existing_lane_snapshot(self) -> None:
        """「谁占着引擎」不另存一份：读的是 lane 自己的账。"""
        async def hold() -> str:
            runner.capture_active_run("r1", lane="jvm")
            async with runner.acquire_lane("jvm", kind="batch"):
                return runner.active_run_snapshot("r1")["lane_holder"]
        self.assertEqual(asyncio.run(hold()), "batch")

    def test_empty_run_id_is_not_registered(self) -> None:
        self.assertEqual(runner.capture_active_run(""), "")
        self.assertEqual(runner.capture_active_run("   "), "")


class DebugStatusEndpointTests(unittest.TestCase):
    def tearDown(self) -> None:
        runner._finish_active_run("r1")

    def test_unknown_run_id_answers_empty_phase(self) -> None:
        body = asyncio.run(rules.debug_status("nope"))
        self.assertEqual(body["phase"], "")
        self.assertEqual(body["run_id"], "nope")

    def test_registered_run_reports_phase_and_elapsed(self) -> None:
        runner.capture_active_run("r1", lane="jvm")
        runner.note_active_run("r1", "starting")
        body = asyncio.run(rules.debug_status("r1"))
        self.assertEqual(body["phase"], "starting")
        self.assertIn("elapsed_ms", body)
        self.assertIn("phase_ms", body)
        self.assertIn("lane_holder", body)
        self.assertIn("lane_waiting", body)

    def test_default_query_is_not_an_error(self) -> None:
        """不传 run_id（手工 curl / 旧前端）也要有一个形状合法的答复。"""
        body = asyncio.run(rules.debug_status())
        self.assertEqual(body["phase"], "")


if __name__ == "__main__":
    unittest.main()
