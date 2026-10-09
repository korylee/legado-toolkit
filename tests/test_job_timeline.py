# -*- coding: utf-8 -*-
"""运行观测帧（``backend/api/job_timeline``）：增量游标、失败清单、终态读法。

帧是**唯一一条读法**：轮询与 SSE 渲染同一份（见该模块的 `job_timeline` /
`job_stream`）。这里钉住的是消费契约：

- **行号即游标**，坏行也占号，文件末尾的半行留到下一拍；
- 事件与失败都来自**运行目录的两本账**，所以运行态与终态是同一条读法；
- ``failures.total`` 是精确总数（cap 只截清单）；
- 旧任务（事件只在 ``result_json`` 里）走一条**读旧数据**的兜底；
- ``done`` 只认终态状态——非 JVM kind 在跑的时候**不再**被当成已完成。
"""

import json
import pathlib
import shutil
import tempfile
import unittest
from unittest import mock

from fastapi import HTTPException

from backend.api import job_timeline
from backend.jobs import run_events
from core.store import Store


class JobTimelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="job_timeline_"))
        self.addCleanup(shutil.rmtree, str(self.tmp), ignore_errors=True)
        self.db = str(self.tmp / "sources.sqlite3")
        self.run_dir = self.tmp / "runs" / "batch-x"
        self.run_dir.mkdir(parents=True)

    def _store_with_job(self, job_id="j1", kind="jvm_run", status="running",
                        result=None, with_run_dir=True):
        with Store(self.db) as st:
            payload = {"manifest": {"run_dir": str(self.run_dir)}} if with_run_dir else {}
            st.create_job(job_id, kind, payload=payload)
            st.update_job(job_id, status=status, result=result or {})
        return Store(self.db)

    def _append_events(self, *events) -> None:
        path = self.run_dir / run_events.EVENTS_NAME
        with path.open("a", encoding="utf-8") as f:
            for ev in events:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")

    def _append_failures(self, *rows) -> None:
        path = self.run_dir / run_events.FAILURES_NAME
        with path.open("a", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def test_running_job_serves_incremental_events(self) -> None:
        """after 是行号游标：不重发之前的事件，cursor 报到文件末尾。"""
        self._append_events({"kind": "batch_started", "chunks": 2},
                            {"kind": "chunk_started", "index": 0},
                            {"kind": "chunk_done", "index": 0, "count": 2})
        st = self._store_with_job()
        full = job_timeline.run_frame(st, "j1", 0, 0)
        self.assertEqual([e["seq"] for e in full["events"]], [1, 2, 3])
        self.assertEqual(full["cursor"], 3)
        self.assertFalse(full["done"])
        inc = job_timeline.run_frame(st, "j1", 2, 0)
        self.assertEqual([e["seq"] for e in inc["events"]], [3])
        self.assertEqual(inc["cursor"], 3)
        empty = job_timeline.run_frame(st, "j1", 3, 0)
        self.assertEqual(empty["events"], [])

    def test_frame_carries_light_row_and_lane_facts(self) -> None:
        """帧 = 轻量任务行 + 观测事实：状态词表与「谁占着引擎」两边共用一份。"""
        st = self._store_with_job()
        frame = job_timeline.run_frame(st, "j1", 0, 0)
        self.assertEqual(frame["id"], "j1")
        self.assertEqual(frame["kind"], "jvm_run")
        self.assertEqual(frame["status"], "running")
        # 非事件循环上下文读不到 lane：留空而不是抛错（AGENTS #12）
        self.assertEqual(frame["lane_holder"], "")
        self.assertEqual(frame["lane_waiting"], 0)
        self.assertIn("elapsed_ms", frame)
        self.assertIn("phase_ms", frame)

    def test_large_event_file_pages_without_skipping(self) -> None:
        self._append_events(*({"kind": "e%d" % i} for i in range(600)))
        st = self._store_with_job()
        with mock.patch.object(job_timeline, "_EVENT_PAGE_CAP", 500):
            first = job_timeline.run_frame(st, "j1", 0, 0)
            second = job_timeline.run_frame(st, "j1", first["cursor"], 0)
        self.assertEqual(len(first["events"]), 500)
        self.assertEqual(first["cursor"], 500)
        self.assertEqual([e["kind"] for e in second["events"]],
                         ["e%d" % i for i in range(500, 600)])
        self.assertEqual(second["cursor"], 600)

    def test_incomplete_tail_waits_for_next_poll(self) -> None:
        path = self.run_dir / run_events.EVENTS_NAME
        path.write_text(json.dumps({"kind": "complete"}) + "\n"
                        + '{"kind":"partial"', encoding="utf-8")
        st = self._store_with_job()
        first = job_timeline.run_frame(st, "j1", 0, 0)
        self.assertEqual([e["kind"] for e in first["events"]], ["complete"])
        self.assertEqual(first["cursor"], 1)
        path.write_text(path.read_text(encoding="utf-8") + "}\n", encoding="utf-8")
        second = job_timeline.run_frame(st, "j1", first["cursor"], 0)
        self.assertEqual([e["kind"] for e in second["events"]], ["partial"])
        self.assertEqual(second["cursor"], 2)

    def test_broken_line_keeps_its_slot_and_is_skipped(self) -> None:
        """坏行也占一个行号（跳过不解析）——否则游标会在坏行处打转，
        客户端反复重取同一段。"""
        with (self.run_dir / run_events.EVENTS_NAME).open("a", encoding="utf-8") as f:
            f.write(json.dumps({"kind": "batch_started"}) + "\n")
            f.write("not-json{{{\n")
            f.write(json.dumps({"kind": "done"}) + "\n")
        st = self._store_with_job()
        got = job_timeline.run_frame(st, "j1", 0, 0)
        self.assertEqual([e["seq"] for e in got["events"]], [1, 3])
        self.assertEqual(got["cursor"], 3)
        tail = job_timeline.run_frame(st, "j1", 2, 0)
        self.assertEqual([e["seq"] for e in tail["events"]], [3])

    def test_failures_are_read_from_the_run_dir_with_chunk_labels(self) -> None:
        """失败清单来自 ``failures.jsonl``（跑批在产生处写一次，不再重扫 results）。"""
        self._append_failures(
            {"url": "https://b.com", "name": "乙", "state": "no_result",
             "reason": "搜索为空", "stage": "search", "chunk": "chunk-01"},
            {"url": "https://c.com", "name": "丙", "state": "error",
             "reason": "连不上站点", "stage": "search", "chunk": "chunk-02"})
        st = self._store_with_job()
        got = job_timeline.run_frame(st, "j1", 0, 0)
        f = got["failures"]
        self.assertEqual(f["total"], 2)
        self.assertFalse(f["truncated"])
        self.assertEqual([(i["url"], i["chunk"]) for i in f["items"]],
                         [("https://b.com", "chunk-01"),
                          ("https://c.com", "chunk-02")])
        self.assertEqual(f["items"][0]["reason"], "搜索为空")
        self.assertEqual(f["cursor"], 2)
        again = job_timeline.run_frame(st, "j1", 0, f["cursor"])
        self.assertEqual(again["failures"]["items"], [])
        # total 与 after 无关：增量轮询不会把总数说小
        self.assertEqual(again["failures"]["total"], 2)

    def test_failures_cap_keeps_exact_total(self) -> None:
        """cap 只截清单，total 仍精确——「还有 N 条未列出」靠它。"""
        self._append_failures(*({"url": "https://x.com", "state": "error"} for _ in range(3)))
        st = self._store_with_job()
        with mock.patch.object(job_timeline, "_FAILURES_CAP", 2):
            got = job_timeline.run_frame(st, "j1", 0, 0)
        self.assertEqual(got["failures"]["total"], 3)
        self.assertEqual(len(got["failures"]["items"]), 2)
        self.assertTrue(got["failures"]["truncated"])

    def test_terminal_reads_the_same_accounts_from_the_run_dir(self) -> None:
        """终态与运行中**同一条读法**：成功清场保留了事件与失败，就跑完也读文件。

        （上一版是"终态必须从 result_json 读、就算磁盘上有也不许用"——因为成功会
        整目录删掉，只剩 500 条尾巴；现在目录里的两本账就是事实本身。）
        """
        self._append_events({"kind": "batch_started"}, {"kind": "done", "count": 6})
        self._append_failures({"url": "https://b.com", "name": "乙", "state": "auth",
                               "reason": "站点要求登录", "stage": "", "chunk": "chunk-01"})
        st = self._store_with_job(status="done", result={
            "count": 6, "dist": {"ok": 3, "no_result": 2, "error": 1},
            "events": [{"kind": "只在 result_json 里的旧事件"}],
        })
        got = job_timeline.run_frame(st, "j1", 0, 0)
        self.assertTrue(got["done"])
        self.assertEqual([e["kind"] for e in got["events"]],
                         ["batch_started", "done"])
        self.assertEqual(got["failures"]["total"], 1)
        self.assertEqual(got["failures"]["items"][0]["state"], "auth")

    def test_legacy_terminal_falls_back_to_result_json(self) -> None:
        """旧任务：事件与失败只在 ``result_json`` 里（清场就删目录那版实现的遗留）。"""
        result = {
            "count": 6, "dist": {"ok": 3, "no_result": 2, "error": 1},
            "events": [{"kind": "batch_started"}, {"kind": "done", "count": 6}],
            "items": [{"url": "https://a.com", "name": "甲", "health": "ok", "error": ""},
                      {"url": "https://b.com", "name": "乙", "health": "auth",
                       "error": "站点要求登录"}],
        }
        st = self._store_with_job(status="done", result=result, with_run_dir=False)
        got = job_timeline.run_frame(st, "j1", 0, 0)
        self.assertTrue(got["done"])
        self.assertEqual([e["kind"] for e in got["events"]],
                         ["batch_started", "done"])
        self.assertEqual(got["failures"]["total"], 3)
        self.assertEqual(got["failures"]["items"],
                         [{"seq": 2, "url": "https://b.com", "name": "乙", "state": "auth",
                           "reason": "站点要求登录", "stage": "", "chunk": ""}])

    def test_non_jvm_run_reports_real_status(self) -> None:
        """没有时间线的 kind 也照实报状态：在跑就是没完——旧版一律 ``done``，
        于是前端会立刻停掉流，把一次在跑的任务看成已结束。"""
        st = self._store_with_job(kind="add", status="running", with_run_dir=False)
        got = job_timeline.run_frame(st, "j1", 0, 0)
        self.assertFalse(got["done"])
        self.assertEqual(got["events"], [])
        self.assertEqual(got["failures"]["items"], [])
        done = job_timeline.run_frame(
            self._store_with_job("j2", kind="add", status="done", with_run_dir=False),
            "j2", 0, 0)
        self.assertTrue(done["done"])

    def test_cancelled_job_serves_retained_events(self) -> None:
        self._append_events({"kind": "batch_started", "chunks": 2},
                            {"kind": "cancelled"})
        st = self._store_with_job(status="cancelled", result={})
        got = job_timeline.run_frame(st, "j1", 0, 0)
        self.assertTrue(got["done"])
        self.assertEqual([e["kind"] for e in got["events"]],
                         ["batch_started", "cancelled"])

    def test_missing_run_dir_is_tolerated(self) -> None:
        st = self._store_with_job()
        shutil.rmtree(str(self.run_dir))
        got = job_timeline.run_frame(st, "j1", 0, 0)
        self.assertEqual(got["events"], [])
        self.assertEqual(got["failures"]["total"], 0)
        self.assertFalse(got["done"])

    def test_missing_job_is_404(self) -> None:
        st = Store(self.db)
        with self.assertRaises(HTTPException) as ctx:
            job_timeline.run_frame(st, "nope", 0, 0)
        self.assertEqual(ctx.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
