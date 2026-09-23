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
            "items": [{"url": "https://a.example"}],
        }))
        self.assertEqual(detail["id"], "j1")
        self.assertEqual(detail["summary"]["checked"], 2)
        self.assertEqual(detail["summary"]["changed_total"], 2)
        self.assertEqual(len(detail["summary"]["changed_items"]), 1)
        self.assertIsNone(detail["result"])

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
