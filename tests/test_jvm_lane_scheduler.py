# -*- coding: utf-8 -*-
"""JVM lane 的调度语义：优先级、老化、取消转交——都在 unit 边界重排等待者。

直接驱动 `runner._Lane`（调度器本体），不开任务、不跑 Gradle；老化常量用
patch 缩到毫秒级，睡眠只用于确定「谁先排队」，不赌执行速度。
"""

from __future__ import annotations

import asyncio
import pathlib
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

    def test_debug_then_current_batch_resume_then_new_batch(self) -> None:
        async def go():
            lane = runner._Lane()
            order = []

            queued = [asyncio.Event() for _ in range(3)]

            async def worker(kind, name, ready):
                ready.set()
                await lane.acquire(kind)
                order.append(name)
                lane.release()

            await lane.acquire("batch")
            workers = [
                asyncio.create_task(worker("batch_resume", "resume", queued[0])),
                asyncio.create_task(worker("batch", "new-batch", queued[1])),
                asyncio.create_task(worker("debug", "debug", queued[2])),
            ]
            await asyncio.gather(*(event.wait() for event in queued))
            lane.release()
            await asyncio.gather(*workers)
            return order

        self.assertEqual(self._order(go), ["debug", "resume", "new-batch"])

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


class RunnerSubmissionTests(unittest.TestCase):
    def test_jvm_run_gets_job_scoped_timeline_dir(self):
        run_dir = pathlib.Path("C:/data/runs/batch-1")
        saved = {}
        task = object()

        class _Store:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def create_job(self, job_id, _kind, **kwargs):
                saved.update(id=job_id, payload=kwargs["payload"])

        def discard_coroutine(coro):
            coro.close()
            return task

        payload = {"manifest": {"run_dir": str(run_dir)}}
        with mock.patch.object(runner, "HANDLERS", {"jvm_run": object()}), \
             mock.patch.object(runner, "Store", _Store), \
             mock.patch.object(runner.uuid, "uuid4",
                               return_value=mock.Mock(hex="0123456789abcdef")), \
             mock.patch.object(runner.asyncio, "create_task",
                               side_effect=discard_coroutine), \
             mock.patch.object(runner, "TASKS", {}):
            job_id = runner.submit("jvm_run", payload)

        self.assertEqual(saved["payload"]["manifest"]["run_dir"], str(run_dir))
        self.assertEqual(saved["payload"]["timeline_dir"],
                         str(run_dir / "attempts" / job_id))


class DebugSubmissionGateTests(unittest.TestCase):
    def test_debug_submit_is_rejected_when_jvm_lane_has_work(self):
        cases = [
            [{"kind": "jvm_debug", "status": "pending"}],
        ]
        for active_jobs in cases:
            with self.subTest(active_jobs=active_jobs):
                created = []

                class _Store:
                    def __enter__(self):
                        return self

                    def __exit__(self, *_args):
                        return None

                    def list_jobs(self, **_kwargs):
                        return active_jobs

                    def create_job(self, *_args, **_kwargs):
                        created.append(True)

                with mock.patch.object(runner, "HANDLERS", {"jvm_debug": object()}), \
                     mock.patch.object(runner, "Store", _Store), \
                     mock.patch.object(runner, "TASKS", {}):
                    with self.assertRaises(runner.JvmLaneBusyError):
                        runner.submit("jvm_debug", {})
                self.assertEqual(created, [])

    def test_debug_submit_is_allowed_ahead_of_validation(self):
        saved = {}
        task = object()

        class _Store:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def list_jobs(self, **_kwargs):
                return [{"kind": "jvm_run", "status": "running"}]

            def create_job(self, job_id, kind, **kwargs):
                saved.update(id=job_id, kind=kind, payload=kwargs["payload"])

        def discard_coroutine(coro):
            coro.close()
            return task

        with mock.patch.object(runner, "HANDLERS", {"jvm_debug": object()}), \
             mock.patch.object(runner, "Store", _Store), \
             mock.patch.object(runner.uuid, "uuid4",
                               return_value=mock.Mock(hex="debugjob123456789")), \
             mock.patch.object(runner.asyncio, "create_task",
                               side_effect=discard_coroutine), \
             mock.patch.object(runner, "TASKS", {}):
            job_id = runner.submit("jvm_debug", {"source": {"bookSourceUrl": "x"}})
        self.assertEqual(saved["id"], job_id)
        self.assertEqual(saved["kind"], "jvm_debug")


if __name__ == "__main__":
    unittest.main()
