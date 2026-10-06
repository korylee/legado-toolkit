# -*- coding: utf-8 -*-
"""块内输出停滞看门狗与隔离重跑的纯件。

背景（2026-10-05）：enmuku 一条源把常驻引擎的请求挂死，块级 socket 等待
（每源预算 × 块源数 ≈ 11 分钟）成了唯一护栏——用户看着「卡住不动」却无能为力。
看门狗盯 results 文件停滞、隔离阶梯把剩余源拆成单源块重跑，这里守四个纯件：
停滞触发、剩余源计算（两侧归一，AGENTS #5）、判死行、结论合并。
"""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from backend.jobs import jvm_exec


class WatchOutputStallTests(unittest.TestCase):
    def test_stall_triggers_abort_once(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "results.jsonl"
            out.write_text('{"url":"a"}\n', encoding="utf-8")
            stop = threading.Event()
            why = {}
            calls = []
            t = threading.Thread(target=jvm_exec._watch_output_stall, daemon=True,
                                 args=(out, 0.3, stop, lambda: calls.append(1),
                                       0.05, None, why))
            t.start()
            t.join(3)
            self.assertFalse(t.is_alive(), "看门狗触发后应当退出")
            self.assertEqual(len(calls), 1, "abort 只触发一次")
            self.assertTrue(why.get("stalled"))

    def test_growing_file_does_not_trigger(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "results.jsonl"
            out.write_text("", encoding="utf-8")
            stop = threading.Event()
            why = {}
            calls = []
            t = threading.Thread(target=jvm_exec._watch_output_stall, daemon=True,
                                 args=(out, 0.4, stop, lambda: calls.append(1),
                                       0.05, None, why))
            t.start()
            for i in range(6):
                time.sleep(0.1)
                with open(out, "a", encoding="utf-8") as fh:
                    fh.write('{"url":"%d"}\n' % i)   # 持续有产出：不该触发
            stop.set()
            t.join(3)
            self.assertEqual(calls, [], "文件在长就不算停滞")
            self.assertEqual(why, {})

    def test_missing_file_does_not_trigger(self):
        """引擎还没写出第一行不算停滞（那段由 socket 等待上限兜底）。"""
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "results.jsonl"      # 故意不存在
            stop = threading.Event()
            calls = []
            t = threading.Thread(target=jvm_exec._watch_output_stall, daemon=True,
                                 args=(out, 0.25, stop, lambda: calls.append(1),
                                       0.05, None, None))
            t.start()
            time.sleep(0.6)
            stop.set()
            t.join(3)
            self.assertEqual(calls, [])

    def test_cancel_triggers_abort(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "results.jsonl"
            out.write_text("", encoding="utf-8")
            stop = threading.Event()
            why = {}
            calls = []
            cancelled = threading.Event()
            cancelled.set()
            t = threading.Thread(target=jvm_exec._watch_output_stall, daemon=True,
                                 args=(out, 60, stop, lambda: calls.append(1),
                                       0.05, cancelled.is_set, why))
            t.start()
            t.join(3)
            self.assertEqual(len(calls), 1)
            self.assertTrue(why.get("cancelled"))
            self.assertFalse(why.get("stalled"), "取消不该被记成停滞")


class RemainingSourcesTests(unittest.TestCase):
    def test_matches_after_normalization(self):
        """sources.json 给 bookSourceUrl 原文、results 给跑批写回的 url——
        两侧都要归一（AGENTS #5），尾斜杠差一个字符就全对不上。"""
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "results.jsonl"
            out.write_text(json.dumps({"url": "http://a.com/"}) + "\n"
                           + json.dumps({"url": "http://b.com"}) + "\n",
                           encoding="utf-8")
            rows = jvm_exec._remaining_sources(
                [{"bookSourceUrl": "http://a.com"},
                 {"bookSourceUrl": "http://b.com"},
                 {"bookSourceUrl": "http://c.com"}], out)
            self.assertEqual([r["bookSourceUrl"] for r in rows], ["http://c.com"])

    def test_broken_results_count_as_remaining(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "results.jsonl"
            out.write_text('{"url":"http://a.com"}\n{"url": "http://x',  # 半行
                           encoding="utf-8")
            rows = jvm_exec._remaining_sources(
                [{"bookSourceUrl": "http://a.com"},
                 {"bookSourceUrl": "http://b.com"}], out)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["bookSourceUrl"], "http://b.com")


class UnresponsiveRowTests(unittest.TestCase):
    def test_row_is_pending_timeout_with_reason(self):
        row = jvm_exec._unresponsive_row(
            {"bookSourceUrl": "http://a.com", "bookSourceName": "甲"},
            "search", "引擎对该源无响应，已按超时跳过")
        # timeout → ❓待验证：引擎没给出结论，不诬源为坏（兜底档否定式，AGENTS #17）
        self.assertEqual(row["state"], "timeout")
        self.assertEqual(row["url"], "http://a.com")
        self.assertEqual(row["name"], "甲")
        self.assertIn("无响应", row["reason"])


class MergeResultRowsTests(unittest.TestCase):
    def test_merges_and_keeps_original_lines(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "results.jsonl"
            out.write_text('{"url":"a","state":"ok"}\n', encoding="utf-8")
            jvm_exec._merge_result_rows(out, [
                {"url": "b", "state": "timeout", "reason": "引擎对该源无响应，已按超时跳过"}])
            rows = [json.loads(l) for l in
                    out.read_text(encoding="utf-8").splitlines() if l.strip()]
            self.assertEqual([r["url"] for r in rows], ["a", "b"])
            self.assertEqual(rows[1]["state"], "timeout")

    def test_merge_on_missing_base_creates_file(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "results.jsonl"
            jvm_exec._merge_result_rows(out, [{"url": "a", "state": "ok"}])
            rows = [json.loads(l) for l in
                    out.read_text(encoding="utf-8").splitlines() if l.strip()]
            self.assertEqual(len(rows), 1)


if __name__ == "__main__":
    unittest.main()
