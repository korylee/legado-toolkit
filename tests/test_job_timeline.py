# -*- coding: utf-8 -*-
"""任务时间线端点（jvm-batch-timeline）：增量游标、失败源过滤、终态切换。

端点只读：运行中从运行目录的 ``events.jsonl`` 与各块 ``results.jsonl`` 取，
终态从 ``result_json`` 取（成功清场后运行目录已不在）。生产者（执行线程
追加事件，见 ``backend/jobs/jvm_exec``）落地前，这里的夹具直接写文件，
钉住的是**消费契约**：行号即游标、坏行占号跳过、「通过」= 仅 state=="ok"。
"""

import json
import pathlib
import shutil
import tempfile
import unittest
from unittest import mock

from fastapi import HTTPException

from backend.api import job_timeline
from core.store import Store


class JobTimelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="job_timeline_"))
        self.addCleanup(shutil.rmtree, str(self.tmp), ignore_errors=True)
        self.db = str(self.tmp / "sources.sqlite3")
        self.run_dir = self.tmp / "runs" / "batch-x"
        self.run_dir.mkdir(parents=True)

    def _store_with_job(self, job_id="j1", kind="jvm_run", status="running",
                        result=None):
        with Store(self.db) as st:
            st.create_job(job_id, kind,
                          payload={"manifest": {"run_dir": str(self.run_dir)}})
            if status in ("done", "failed", "cancelled"):
                st.update_job(job_id, status=status, result=result or {})
        return Store(self.db)

    def _append_events(self, *events) -> None:
        with (self.run_dir / "events.jsonl").open("a", encoding="utf-8") as f:
            for ev in events:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")

    def _chunk(self, name: str, lines) -> None:
        d = self.run_dir / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "results.jsonl").write_text(
            "".join(line + "\n" for line in lines), encoding="utf-8")

    def test_running_job_serves_incremental_events(self) -> None:
        """after 是行号游标：不重发之前的事件，cursor 报到文件末尾。"""
        self._append_events({"kind": "batch_started", "chunks": 2},
                            {"kind": "chunk_started", "index": 0},
                            {"kind": "chunk_done", "index": 0, "count": 2})
        st = self._store_with_job()
        full = job_timeline.job_timeline("j1", 0, st=st)
        self.assertEqual([e["seq"] for e in full["events"]], [1, 2, 3])
        self.assertEqual(full["cursor"], 3)
        self.assertFalse(full["done"])
        inc = job_timeline.job_timeline("j1", 2, st=st)
        self.assertEqual([e["seq"] for e in inc["events"]], [3])
        self.assertEqual(inc["cursor"], 3)
        empty = job_timeline.job_timeline("j1", 3, st=st)
        self.assertEqual(empty["events"], [])

    def test_broken_line_keeps_its_slot_and_is_skipped(self) -> None:
        """坏行也占一个行号（跳过不解析）——否则游标会在坏行处打转，
        客户端反复重取同一段。"""
        with (self.run_dir / "events.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"kind": "batch_started"}) + "\n")
            f.write("not-json{{{\n")
            f.write(json.dumps({"kind": "done"}) + "\n")
        st = self._store_with_job()
        got = job_timeline.job_timeline("j1", 0, st=st)
        self.assertEqual([e["seq"] for e in got["events"]], [1, 3])
        self.assertEqual(got["cursor"], 3)
        tail = job_timeline.job_timeline("j1", 2, st=st)
        self.assertEqual([e["seq"] for e in tail["events"]], [3])

    def test_failures_filter_state_and_chunk_label(self) -> None:
        """「通过」= 仅 state=="ok"；失败源带块标签；单条的 results.jsonl
        （不在 chunk 目录下）也扫，chunk 标签为空。"""
        self._chunk("chunk-01", [
            json.dumps({"url": "https://a.com", "name": "甲", "state": "ok"}),
            json.dumps({"url": "https://b.com", "name": "乙", "state": "no_result",
                        "reason": "搜索为空", "stage": "search"}),
            "{broken",
        ])
        self._chunk("chunk-02", [
            json.dumps({"url": "https://c.com", "name": "丙", "state": "error",
                        "reason": "连不上站点", "stage": "search"}),
        ])
        self._chunk("chunk-03", [
            json.dumps({"url": "https://d.com", "name": "丁", "state": "ok"}),
        ])
        self._chunk("", [   # 单条路径的文件：run_dir/results.jsonl
            json.dumps({"url": "", "name": "", "state": "invalid",
                        "reason": "源 JSON 解析失败"}),
        ])
        st = self._store_with_job()
        got = job_timeline.job_timeline("j1", 0, st=st)
        f = got["failures"]
        self.assertEqual(f["total"], 3)
        self.assertFalse(f["truncated"])
        self.assertEqual([(i["url"], i["chunk"]) for i in f["items"]],
                         [("https://b.com", "chunk-01"),
                          ("https://c.com", "chunk-02"),
                          ("", "")])
        self.assertEqual(f["items"][0]["reason"], "搜索为空")

    def test_failures_cap_keeps_exact_total(self) -> None:
        """cap 只截清单，total 仍精确——「还有 N 条未列出」靠它。"""
        rows = [json.dumps({"url": "https://x.com", "state": "error"})] * 3
        self._chunk("chunk-01", rows)
        st = self._store_with_job()
        with mock.patch.object(job_timeline, "_FAILURES_CAP", 2):
            got = job_timeline.job_timeline("j1", 0, st=st)
        self.assertEqual(got["failures"]["total"], 3)
        self.assertEqual(len(got["failures"]["items"]), 2)
        self.assertTrue(got["failures"]["truncated"])

    def test_terminal_serves_from_result_json_not_disk(self) -> None:
        """终态从 result_json 取（成功清场后运行目录已不在）——
        就算运行目录里还留着旧文件也不许用。"""
        self._append_events({"kind": "stale-on-disk"})
        result = {
            "count": 6, "dist": {"ok": 3, "no_result": 2, "error": 1},
            "events": [{"kind": "batch_started"}, {"kind": "done", "count": 6}],
            "items": [{"url": "https://a.com", "name": "甲", "health": "ok",
                       "error": ""},
                      {"url": "https://b.com", "name": "乙", "health": "auth",
                       "error": "站点要求登录"}],
        }
        st = self._store_with_job(status="done", result=result)
        got = job_timeline.job_timeline("j1", 0, st=st)
        self.assertTrue(got["done"])
        self.assertEqual([e["kind"] for e in got["events"]],
                         ["batch_started", "done"])
        self.assertEqual(got["failures"]["total"], 3)
        self.assertEqual(got["failures"]["items"],
                         [{"url": "https://b.com", "name": "乙", "state": "auth",
                           "reason": "站点要求登录", "stage": "", "chunk": ""}])

    def test_non_jvm_run_is_done_with_empty_timeline(self) -> None:
        st = self._store_with_job(kind="add", status="running")
        got = job_timeline.job_timeline("j1", 0, st=st)
        self.assertTrue(got["done"])
        self.assertEqual(got["events"], [])
        self.assertEqual(got["failures"]["items"], [])

    def test_missing_run_dir_is_tolerated(self) -> None:
        st = self._store_with_job()
        shutil.rmtree(str(self.run_dir))
        got = job_timeline.job_timeline("j1", 0, st=st)
        self.assertEqual(got["events"], [])
        self.assertEqual(got["failures"]["total"], 0)
        self.assertFalse(got["done"])

    def test_missing_job_is_404(self) -> None:
        st = Store(self.db)
        with self.assertRaises(HTTPException) as ctx:
            job_timeline.job_timeline("nope", 0, st=st)
        self.assertEqual(ctx.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
