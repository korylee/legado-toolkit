# -*- coding: utf-8 -*-
"""运行目录里两本账的生产者（``backend/jobs/run_events``）：追加、失败清单、清场。

守三件事，都是"不报错但事实不对"的那一类：

1. **失败清单只记没通过的**（只有 ``state == "ok"`` 算通过）——多记一条，界面上就
   多一条假的失败源；少记一条，用户就少一条要修的源。
2. **清场只留事实**：执行产物（sources/manifest/results/DONE）必须删——留着会让
   「再次运行」把已完成的批误判成"已恢复"而空转；事件与失败必须留——删了，
   跑成功的批就永远看不到时间线。
3. **越界路径一个都不删**：删目录这条路上，一个算错的路径就是工作目录。
"""

import json
import pathlib
import shutil
import tempfile
import unittest

from backend.jobs import run_events


class RunEventsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="run_events_"))
        self.addCleanup(shutil.rmtree, str(self.tmp), ignore_errors=True)
        self.root = self.tmp / "runs"
        self.run_dir = self.root / "batch-x"
        self.run_dir.mkdir(parents=True)
        #: 触发一次事件写入，失败清单的目录才存在（run_events 不做 mkdir）
        run_events.append_event(self.run_dir, "batch_started", chunks=2)

    def _lines(self, name: str):
        path = self.run_dir / name
        return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()
                if l.strip()]

    def test_events_carry_kind_and_timestamp(self) -> None:
        run_events.append_event(self.run_dir, "chunk_done", index=1, count=25)
        got = self._lines(run_events.EVENTS_NAME)
        self.assertEqual(got[0]["kind"], "batch_started")
        self.assertIsInstance(got[0]["ts"], float)
        self.assertEqual((got[1]["kind"], got[1]["index"], got[1]["count"]),
                         ("chunk_done", 1, 25))

    def test_record_failures_keeps_only_not_ok(self) -> None:
        run_events.record_failures(self.run_dir, [
            {"url": "https://a.com", "name": "甲", "state": "ok"},
            {"url": "https://b.com", "name": "乙", "state": "no_result",
             "reason": "搜索为空", "stage": "search"},
            {"url": "https://c.com", "name": "丙", "state": "timeout",
             "reason": "引擎对该源无响应，已按超时跳过"},
        ], "chunk-02")
        got = self._lines(run_events.FAILURES_NAME)
        self.assertEqual([row["url"] for row in got],
                         ["https://b.com", "https://c.com"])
        self.assertEqual(got[0]["chunk"], "chunk-02")
        self.assertEqual(got[0]["reason"], "搜索为空")
        # 判死那一条也要带块标签：界面要能说清是"哪一块的哪一条"
        self.assertEqual(got[1]["chunk"], "chunk-02")

    def test_trim_keeps_facts_and_removes_execution_artifacts(self) -> None:
        (self.run_dir / "chunk-01").mkdir()
        (self.run_dir / "chunk-01" / "results.jsonl").write_text("{}\n", encoding="utf-8")
        (self.run_dir / "chunk-01" / "DONE").write_text("", encoding="utf-8")
        (self.run_dir / "sources.json").write_text("[]", encoding="utf-8")
        (self.run_dir / "manifest.json").write_text("{}", encoding="utf-8")
        run_events.record_failures(self.run_dir, [
            {"url": "https://b.com", "state": "error"}], "chunk-01")

        run_events.trim_run_dir(self.run_dir, self.root)

        left = sorted(p.name for p in self.run_dir.iterdir())
        self.assertEqual(left, [run_events.EVENTS_NAME, run_events.FAILURES_NAME])
        # 执行产物必须真的没了：DONE 还在的话「再次运行」会跳过所有块
        self.assertFalse((self.run_dir / "chunk-01").exists())

    def test_outside_paths_are_never_touched(self) -> None:
        """越界的 run_dir：一个文件都不许动（删目录算错路径就是工作目录）。"""
        outside = self.tmp / "work"
        outside.mkdir()
        (outside / "keep.txt").write_text("x", encoding="utf-8")
        run_events.trim_run_dir(outside, self.root)
        run_events.remove_run_dir(outside, self.root)
        # 同一层但不是 root 的子目录、以及 root 自己，都不在射程内
        sibling = self.tmp / "runs2"
        sibling.mkdir()
        run_events.remove_run_dir(sibling, self.root)
        self.assertTrue((outside / "keep.txt").exists())
        self.assertTrue(sibling.exists())
        self.assertTrue(self.run_dir.exists())


if __name__ == "__main__":
    unittest.main()
