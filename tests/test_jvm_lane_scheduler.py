# -*- coding: utf-8 -*-
"""JVM lane 的调度语义：优先级、老化、取消转交——都在 unit 边界重排等待者。

直接驱动 `runner._Lane`（调度器本体），不开任务、不跑 Gradle；老化常量用
patch 缩到毫秒级，睡眠只用于确定「谁先排队」，不赌执行速度。
"""

from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from backend.jobs import runner


class LaneSchedulerTests(unittest.TestCase):
    def _order(self, scenario):
        return asyncio.run(scenario())

    def test_debug_jumps_ahead_of_queued_batches(self) -> None:
        async def go():
            lane = runner._Lane()
            order = []

            async def worker(kind, name):
                await lane.acquire(kind)
                order.append(name)
                await asyncio.sleep(0.005)
                lane.release()

            await lane.acquire("batch")           # 测试自己持有：时机完全确定
            t1 = asyncio.create_task(worker("batch", "b1"))
            await asyncio.sleep(0.01)
            t2 = asyncio.create_task(worker("debug", "d1"))
            await asyncio.sleep(0.01)
            lane.release()
            await asyncio.gather(t1, t2)
            return order

        # 后到的调试越过排在前面的批量；两个批量之间先来后到
        self.assertEqual(self._order(go), ["d1", "b1"])

    def test_aging_lets_batch_run_but_debug_still_wins_the_boundary(self) -> None:
        async def go():
            lane = runner._Lane()
            order = []

            async def worker(kind, name, pre=0.0):
                if pre:
                    await asyncio.sleep(pre)
                await lane.acquire(kind)
                order.append(name)
                await asyncio.sleep(0.005)
                lane.release()

            await lane.acquire("batch")
            aged = asyncio.create_task(worker("batch", "aged"))
            await asyncio.sleep(0.01)
            with mock.patch.object(runner, "_AGING_DELAY", 0.0), \
                 mock.patch.object(runner, "_AGING_STEP", 0.01):
                # 老化的批量(2) < 新批量(10) 但 > 调试(0)：边界上调试先走，
                # 然后是老化的批量——新批量要等下一轮
                debug = asyncio.create_task(worker("debug", "dbg", pre=0.02))
                fresh = asyncio.create_task(worker("batch", "fresh", pre=0.04))
                await asyncio.sleep(0.06)
                lane.release()
                await asyncio.gather(aged, debug, fresh)
            return order

        # 老化的批量(2) < 新批量(10) 但 > 调试(0)：调试先走，老化批量压过新批量
        self.assertEqual(self._order(go), ["dbg", "aged", "fresh"])

    def test_snapshot_reports_holder_and_waiters(self) -> None:
        async def _hold(lane, kind):
            await lane.acquire(kind)
            await asyncio.sleep(0.05)
            lane.release()

        async def go():
            lane = runner._Lane()
            await lane.acquire("batch")
            w1 = asyncio.create_task(_hold(lane, "debug"))
            await asyncio.sleep(0.01)
            w2 = asyncio.create_task(_hold(lane, "batch"))
            await asyncio.sleep(0.01)
            snap = lane.snapshot()
            lane.release()
            await asyncio.gather(w1, w2)
            return snap

        snap = asyncio.run(go())
        self.assertEqual(snap["held"], "batch")
        kinds = [w["kind"] for w in snap["waiting"]]
        self.assertEqual(kinds, ["debug", "batch"])
        self.assertTrue(all("waiting_seconds" in w for w in snap["waiting"]))

    def test_cancelled_granted_waiter_hands_off_the_lane(self) -> None:
        """许可已发给被取消的等待者时必须立刻转交，否则 lane 死锁到下一次释放。"""
        async def go():
            lane = runner._Lane()
            await lane.acquire("batch")           # 持有者
            w1 = asyncio.create_task(lane.acquire("batch", "b1"))
            await asyncio.sleep(0.01)
            w2 = asyncio.create_task(lane.acquire("batch", "b2"))
            await asyncio.sleep(0.01)
            lane.release()                        # 许可发给 b1（先来后到）
            w1.cancel()                           # b1 没来得及消费就被取消
            with self.assertRaises(asyncio.CancelledError):
                await w1
            # b2 必须立即拿到转交的许可；若转交丢失，这里会超时
            await asyncio.wait_for(w2, timeout=1.0)
            lane.release()
            # lane 仍然健康：下一个获取者立刻成功
            await asyncio.wait_for(lane.acquire("debug", "d1"), timeout=1.0)
            lane.release()

        asyncio.run(go())


if __name__ == "__main__":
    unittest.main()
