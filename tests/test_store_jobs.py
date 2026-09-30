# -*- coding: utf-8 -*-
"""`jobs` 表的保留策略。

**它原来会无限增长**：`list_jobs` 只是显示时 `LIMIT 50`，表本身没有任何清理。
一个校验任务几十 KB（带 `items[:500]`），攒几百条就是几十 MB，而且没有上限。

对照组就在同一个文件里——`exports` 有 `expires_at` + `pinned` + `sweep_exports`。
这里照它的形状补一套。任务 TTL 只清理终态，`running` / `pending` 由执行生命周期负责。
"""

from __future__ import annotations

from datetime import datetime, timedelta
import json
import os
import shutil
import unittest
import uuid

from core.store import Store

_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
os.makedirs(_ROOT, exist_ok=True)

_PAST = "2000-01-01 00:00:00"


class JobRetentionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = os.path.join(_ROOT, "tmp_store_jobs_" + uuid.uuid4().hex[:8])
        os.makedirs(self.root)
        self.db = os.path.join(self.root, "sources.sqlite3")

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    @staticmethod
    def _expire(st: Store, job_id: str) -> None:
        """把这个任务的过期时间拨到过去（不想为测试等 7 天）。"""
        with st.conn:
            st.conn.execute("UPDATE jobs SET expires_at = ? WHERE id = ?", (_PAST, job_id))

    def _ids(self, st: Store) -> set:
        return {j["id"] for j in st.list_jobs()}

    def test_new_job_gets_an_expiry(self) -> None:
        """新任务必须带上过期时间——没有它 sweep 无从下手。"""
        with Store(self.db) as st:
            st.create_job("j1", "check")
            row = st.conn.execute(
                "SELECT expires_at, phase FROM jobs WHERE id='j1'").fetchone()
        self.assertTrue(str(row["expires_at"] or "").strip())
        self.assertEqual(row["phase"], "queued")

    def test_phase_is_persisted_and_terminal_jobs_finish(self) -> None:
        """阶段是持久化字段，终态不能留下最后一个执行阶段。"""
        with Store(self.db) as st:
            st.create_job("j1", "check")
            st.update_job("j1", status="running", phase="running_validate")
            self.assertEqual(st.get_job("j1")["phase"], "running_validate")
            st.update_job("j1", status="done", result={"ok": True})
            row = st.get_job("j1")
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["phase"], "finished")

    def test_expired_jobs_are_swept(self) -> None:
        """过期的清掉，没过期的留着。

        清理时机对齐 `exports` 的做法：**在建新任务时顺带扫**（`export.py` 也是
        在列表/创建时调 `sweep_exports`），不另开一个定时器。
        """
        with Store(self.db) as st:
            st.create_job("old", "check")
            st.update_job("old", status="done")
            st.create_job("keep", "check")
            self._expire(st, "old")
            st.create_job("new", "check")      # 这一次创建会顺带扫一遍
            ids = self._ids(st)
        self.assertNotIn("old", ids)
        self.assertIn("keep", ids)
        self.assertIn("new", ids)

    def test_pinned_jobs_survive(self) -> None:
        """固定住的任务永不过期——对齐 `exports.pinned` 的语义。"""
        with Store(self.db) as st:
            st.create_job("pinned", "check")
            with st.conn:
                st.conn.execute("UPDATE jobs SET pinned = 1 WHERE id = 'pinned'")
            self._expire(st, "pinned")
            st.create_job("new", "check")
            self.assertIn("pinned", self._ids(st))

    def test_long_running_jobs_are_protected(self) -> None:
        """自动清理不能删除仍由执行线程持有的任务。"""
        with Store(self.db) as st:
            st.create_job("zombie", "check")
            st.update_job("zombie", status="running")
            self._expire(st, "zombie")
            st.create_job("new", "check")
            self.assertIn("zombie", self._ids(st))

    def test_terminal_jobs_are_swept_after_expiry(self) -> None:
        with Store(self.db) as st:
            st.create_job("done", "check")
            st.update_job("done", status="done")
            self._expire(st, "done")
            st.create_job("new", "check")
            self.assertNotIn("done", self._ids(st))

    def test_delete_job_only_removes_terminal_rows(self) -> None:
        with Store(self.db) as st:
            st.create_job("pending", "check")
            st.create_job("done", "check")
            st.update_job("done", status="done")
            self.assertFalse(st.delete_job("pending"))
            self.assertTrue(st.delete_job("done"))
            self.assertIsNotNone(st.get_job("pending"))
            self.assertIsNone(st.get_job("done"))

    def test_retry_origin_is_kept_on_job(self) -> None:
        with Store(self.db) as st:
            st.create_job("retry", "ping", payload={"steps": 1}, retry_of="old")
            row = st.get_job("retry")
            self.assertEqual(row["retry_of"], "old")
            self.assertEqual(json.loads(row["payload"]), {"steps": 1})

if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------- 变异记录
# 以下为实测（改坏 → `python -B -m unittest tests.test_store_jobs` → 确认变红 → 还原）。
#
#  M30  sweep 去掉终态过滤（把 pending/running 一并删掉）
#         → test_long_running_jobs_are_protected 红：执行中的任务不能被 TTL 清理
#  M31  sweep 的 WHERE 去掉 `pinned = 0`
#         → test_pinned_jobs_survive 红
