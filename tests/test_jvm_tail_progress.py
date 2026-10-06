# -*- coding: utf-8 -*-
"""进度尾随线程的块级上限：cap 钳**本块行数**，不钳全局进度。

历史 bug：cap 错钳在 done（base+count）上，于是每块的进度都被压回「本块条数」——
全量批 145 块永远显示 25/3616（2026-10-05 用户报告「进度和实际对不上」的根因）。
守的是同一类「不报错但结果不对」：进度字段安静地停在错误值上，谁也不报错。
"""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from backend.jobs import jvm_exec


class TailProgressCapTests(unittest.TestCase):
    def _run_tail(self, lines: int, cap: int, base: int) -> list:
        """起尾随线程跑几个轮询周期，收回它上报的进度值。"""
        seen = []
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "results.jsonl"
            out.write_text("\n" * lines, encoding="utf-8")
            stop = threading.Event()
            with mock.patch("backend.jobs.runner.update_progress",
                            side_effect=lambda jid, n: seen.append(n)):
                t = threading.Thread(target=jvm_exec._tail_progress,
                                     args=("job", out, stop, 0.01, cap, base),
                                     daemon=True)
                t.start()
                time.sleep(0.15)
                stop.set()
                t.join(5)
        return seen

    def test_cap_limits_chunk_count_not_total(self):
        seen = self._run_tail(lines=30, cap=25, base=25)
        self.assertTrue(seen, "尾随线程没有上报任何进度")
        # 本块 30 行被钳到 25：全局 = 25 + 25 = 50，而不是被压回 25
        self.assertEqual(max(seen), 50)
        self.assertEqual(seen[-1], 50)

    def test_within_cap_reports_global_count(self):
        seen = self._run_tail(lines=10, cap=25, base=25)
        self.assertTrue(seen)
        self.assertEqual(max(seen), 35)


if __name__ == "__main__":
    unittest.main()
