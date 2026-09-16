# -*- coding: utf-8 -*-
"""`jobs` 表的保留策略。

**它原来会无限增长**：`list_jobs` 只是显示时 `LIMIT 50`，表本身没有任何清理。
一个校验任务几十 KB（带 `items[:500]`），攒几百条就是几十 MB，而且没有上限。

对照组就在同一个文件里——`exports` 有 `expires_at` + `pinned` + `sweep_exports`。
这里照它的形状补一套。区别只有一个：**任务不特殊保护 `running`**（见下）。
"""

from __future__ import annotations

from datetime import datetime, timedelta
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
            row = st.conn.execute("SELECT expires_at FROM jobs WHERE id='j1'").fetchone()
        self.assertTrue(str(row["expires_at"] or "").strip())

    def test_expired_jobs_are_swept(self) -> None:
        """过期的清掉，没过期的留着。

        清理时机对齐 `exports` 的做法：**在建新任务时顺带扫**（`export.py` 也是
        在列表/创建时调 `sweep_exports`），不另开一个定时器。
        """
        with Store(self.db) as st:
            st.create_job("old", "check")
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

    def test_old_rows_are_backfilled_not_swept(self) -> None:
        """**补列之前落下的行要被补上过期时间，而不是立刻扫掉。**

        `expires_at` 补列时是空串，而空串**按字符串比较小于任何时间戳**——
        不补的话 `sweep_jobs` 第一次跑就把历史任务全删了，用户那边看起来就是
        「升级一次，任务列表空了」。这是这套机制里唯一会把用户数据弄丢的地方。
        """
        import sqlite3

        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("CREATE TABLE sources (id INTEGER PRIMARY KEY, source_url TEXT UNIQUE, "
                     "name TEXT, source_type INTEGER, group_name TEXT, enabled INTEGER, "
                     "raw_json TEXT, fingerprint TEXT, deleted_at TEXT NOT NULL DEFAULT '', "
                     "created_at TEXT, updated_at TEXT)")
        # 老 jobs：没有 expires_at / pinned（其余列按当前 DDL 减去这两条）
        conn.execute(
            "CREATE TABLE jobs (id TEXT PRIMARY KEY, kind TEXT NOT NULL DEFAULT '', "
            "status TEXT NOT NULL DEFAULT '', progress INTEGER DEFAULT 0, "
            "total INTEGER DEFAULT 0, payload TEXT DEFAULT '', result_json TEXT DEFAULT '', "
            "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
        # **用一个「昨天更新过」的时间**，不是写死的旧日期：回填的口径是
        # `updated_at + TTL`，写 2026-09-01 的话它本来就过期了，被扫掉是对的，
        # 那样就测不出「回填有没有生效」——第一版就是这么写错的
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "INSERT INTO jobs(id,kind,status,created_at,updated_at) VALUES (?,?,?,?,?)",
            ("legacy", "check", "done", yesterday, yesterday))
        conn.commit()
        conn.close()

        with Store(self.db) as st:
            st.create_job("new", "check")     # 顺带 sweep
            ids = self._ids(st)
        self.assertIn("legacy", ids, "老任务被立刻扫掉了——回填没生效")
        self.assertIn("new", ids)

    def test_long_running_jobs_are_not_protected(self) -> None:
        """**跑着的任务不特殊保护**。

        看着像漏了，其实是有意的：过期时间是 7 天，而一个校验任务跑不了 7 天。
        真正会留下的是**僵尸行**——服务端重启后状态永远停在 running/pending、
        再也没人推进它。给 running 开豁免，恰恰会让这些僵尸永远清不掉。
        """
        with Store(self.db) as st:
            st.create_job("zombie", "check")
            st.update_job("zombie", status="running")
            self._expire(st, "zombie")
            st.create_job("new", "check")
            self.assertNotIn("zombie", self._ids(st))


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------- 变异记录
# 以下为实测（改坏 → `python -B -m unittest tests.test_store_jobs` → 确认变红 → 还原）。
#
#  M29  去掉 `_init_schema` 里的 `_backfill_job_expiry()`
#         → test_old_rows_are_backfilled_not_swept 红
#         **这条是整个机制里唯一会弄丢用户数据的地方**：补列后老行的 expires_at 是
#         空串，空串按字符串比较小于任何时间戳 → 不清一次就把历史任务全删了
#  M30  sweep 的 WHERE 加 `status NOT IN ('running','pending')`（给跑着的开豁免）
#         → test_long_running_jobs_are_not_protected 红
#         → test_expired_jobs_are_swept **也红**——`create_job` 建出来的就是 pending，
#           豁免之后它们永远不会被清。这条顺带证明了「不做豁免」是必须的
#  M31  sweep 的 WHERE 去掉 `pinned = 0`
#         → test_pinned_jobs_survive 红
