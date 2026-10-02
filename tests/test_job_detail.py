# -*- coding: utf-8 -*-
"""任务明细接口的稳定形状。"""

import json
import unittest

from backend.api.jobs import _job_detail


class JobDetailShapeTests(unittest.TestCase):
    @staticmethod
    def _job(result):
        return {
            "id": "j1", "kind": "check", "status": "done",
            "progress": 2, "total": 2,
            "created_at": "2026-09-23 10:00:00",
            "updated_at": "2026-09-23 10:00:02",
            "result_json": json.dumps(result, ensure_ascii=False),
        }

    def test_check_result_is_reduced_to_summary(self):
        detail = _job_detail(self._job({
            "checked": 2, "cached": 1, "fetched": 1,
            "save_failures": 1,
            "transitions": {
                "first_checked": 1,
                "changed": {"dead": 2},
                "changed_items": [{"url": "https://a.example", "from": "ok", "to": "dead"}],
            },
            "execution_mode": "validate_daemon",
            "execution_note": "单条校验复用常驻 Validate JVM",
            "items": [{"url": "https://a.example"}],
        }))
        self.assertEqual(detail["id"], "j1")
        self.assertEqual(detail["summary"]["checked"], 2)
        self.assertEqual(detail["summary"]["changed_total"], 2)
        self.assertEqual(len(detail["summary"]["changed_items"]), 1)
        self.assertEqual(detail["execution_mode"], "validate_daemon")
        self.assertIn("常驻 Validate JVM", detail["execution_note"])
        self.assertIsNone(detail["result"])

    def test_jvm_batch_exposes_bounded_chunk_reports(self):
        long_stderr = "x" * 4100
        result = {
            "checked": 2, "cached": 0, "fetched": 2,
            "chunk_reports": [
                {"index": 0, "ok": True, "count": 2,
                 "execution_mode": "validate_daemon", "cost_sec": 12.5},
                {"index": 1, "ok": False, "exit": 1,
                 "execution_mode": "gradle", "cost_sec": 8.0,
                 "daemon_failure": "daemon 探测未通过",
                 "reason": "Gradle 失败",
                 "gradle": {"exit": 1, "stderr": long_stderr,
                            "stdout": "build output"}},
            ],
            "items": [{"url": "https://a.example"}],
            "daemon_prepare": {"outcome": "started", "reason": ""},
        }
        detail = _job_detail({**self._job(result), "kind": "jvm_run"})

        self.assertIsNone(detail["result"])
        self.assertEqual(detail["chunk_report_total"], 2)
        self.assertFalse(detail["chunk_reports_truncated"])
        self.assertEqual(len(detail["chunk_reports"]), 2)
        self.assertEqual(detail["chunk_reports"][0]["execution_mode"], "validate_daemon")
        self.assertEqual(detail["daemon_prepare"]["outcome"], "started")
        failed = detail["chunk_reports"][1]
        self.assertEqual(failed["daemon_failure"], "daemon 探测未通过")
        self.assertEqual(failed["gradle"]["stdout"], "build output")
        self.assertLessEqual(len(failed["gradle"]["stderr"]), 4001)
        self.assertTrue(failed["gradle"]["stderr"].endswith("x" * 100))

    def test_jvm_batch_chunk_report_count_is_bounded(self):
        result = {"checked": 0, "chunk_reports": [{"index": i} for i in range(251)]}
        detail = _job_detail({**self._job(result), "kind": "jvm_run"})

        self.assertEqual(len(detail["chunk_reports"]), 250)
        self.assertEqual(detail["chunk_report_total"], 251)
        self.assertTrue(detail["chunk_reports_truncated"])

    def test_jvm_batch_resumed_chunk_is_projected(self):
        """恢复块必须带 resumed 标记——没有它，前端会把「上次已完成、本次未重跑」
        说成一次真实的 daemon 执行。"""
        result = {"checked": 0, "cached": 0, "fetched": 0,
                  "chunk_reports": [{"index": 0, "ok": True, "count": 2,
                                     "resumed": True}]}
        detail = _job_detail({**self._job(result), "kind": "jvm_run"})
        self.assertTrue(detail["chunk_reports"][0]["resumed"])

    def test_long_reason_keeps_head_not_tail(self):
        """原因/定性类的要点在开头：保尾会把「daemon 返回 code=2：」这类
        定性切掉，只剩日志尾巴。"""
        long_reason = "daemon 返回 code=2：" + "y" * 1300
        result = {"checked": 0, "cached": 0, "fetched": 0,
                  "chunk_reports": [{"index": 0, "ok": False,
                                     "reason": long_reason}]}
        detail = _job_detail({**self._job(result), "kind": "jvm_run"})
        got = detail["chunk_reports"][0]["reason"]
        self.assertTrue(got.startswith("daemon 返回 code=2："))
        self.assertLessEqual(len(got), 1201)

    def test_other_job_keeps_structured_result(self):
        result = {"source_url": "https://a.example", "saved": True}
        detail = _job_detail({**self._job(result), "kind": "add"})
        self.assertIsNone(detail["summary"])
        self.assertEqual(detail["result"], result)

    def test_invalid_result_keeps_reason(self):
        detail = _job_detail({**self._job({}), "result_json": "not-json"})
        self.assertIn("不是有效 JSON", detail["error"])
        self.assertIsNone(detail["summary"])
        self.assertIsNone(detail["result"])


if __name__ == "__main__":
    unittest.main()
