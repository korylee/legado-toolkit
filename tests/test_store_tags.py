# -*- coding: utf-8 -*-
"""Store 用户标签、导出合并和迁移测试。"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import unittest
import uuid

from core.loader import _normalize_url
from core.store import Store


_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
os.makedirs(_ROOT, exist_ok=True)


def make_source(group: str, url: str = "https://example.com",
                source_type: int = 0) -> dict:
    return {
        "bookSourceName": "测试源",
        "bookSourceUrl": url,
        "bookSourceType": source_type,
        "bookSourceGroup": group,
        "enabled": True,
        "ruleSearch": {"bookList": ".book"},
    }


class RecycleBinTagScopeTests(unittest.TestCase):
    """三个「全局标签动作」必须与 `tags_overview` **同口径**（都排除回收站）。

    口径不一致的形态是：`tags_overview`（标签管理的计数来源）只统计
    `deleted_at = ''`，而 `rename_user_tag` / `merge_user_tags` / `delete_user_tag`
    扫的是**全表**。于是界面写着「自用 · 2 个源」，点删除却改了 3 条——
    回收站里那条被静默改掉，用户从回收站把它恢复出来时标签已经没了，
    而他从未在那个源上做过任何操作。

    守的是「**界面让你看到的，就是操作会改到的**」：读和写不能两套口径。
    """

    def setUp(self) -> None:
        self.root = os.path.join(_ROOT, "tmp_store_tag_scope_" + uuid.uuid4().hex[:8])
        os.makedirs(self.root)
        self.db = os.path.join(self.root, "sources.sqlite3")
        # 数据目录一并隔离：软删除会往 `data_path("backups", "deleted.jsonl")`
        # 追加记录，不设 LEGADO_DATA_DIR 就写进真实的 data/backups/ 里
        # （实测那儿混着 183 个本文件产生的 `reason="测试"` 快照）
        self._old_data_dir = os.environ.get("LEGADO_DATA_DIR")
        os.environ["LEGADO_DATA_DIR"] = self.root

    def tearDown(self) -> None:
        if self._old_data_dir is None:
            os.environ.pop("LEGADO_DATA_DIR", None)
        else:
            os.environ["LEGADO_DATA_DIR"] = self._old_data_dir
        shutil.rmtree(self.root, ignore_errors=True)

    _LIVE = "https://live.example"
    _TRASHED = "https://trashed.example"

    def _seed(self, st: Store) -> None:
        st.upsert_sources([
            make_source("活跃,自用", url=self._LIVE),
            make_source("活跃,自用", url=self._TRASHED),
        ])
        st.soft_delete([self._TRASHED], "测试")

    def _user_tags(self, st: Store, url: str) -> str:
        row = st.conn.execute(
            "SELECT user_tags FROM sources WHERE source_url = ?",
            (_normalize_url(url),)).fetchone()
        return row["user_tags"]

    def test_delete_user_tag_skips_the_recycle_bin(self) -> None:
        with Store(self.db) as st:
            self._seed(st)
            # 计数只算活跃源，操作也必须只算活跃源
            self.assertEqual(
                {x["tag"]: x["count"] for x in st.tags_overview()}["自用"], 1)
            self.assertEqual(st.delete_user_tag("自用"), 1)
            self.assertNotIn("自用", self._user_tags(st, self._LIVE))
            # 回收站里那条不动——恢复出来时标签还在
            self.assertIn("自用", self._user_tags(st, self._TRASHED))

    def test_rename_user_tag_skips_the_recycle_bin(self) -> None:
        with Store(self.db) as st:
            self._seed(st)
            self.assertEqual(st.rename_user_tag("自用", "备份"), 1)
            self.assertIn("备份", self._user_tags(st, self._LIVE))
            self.assertIn("自用", self._user_tags(st, self._TRASHED))

    def test_merge_user_tags_skips_the_recycle_bin(self) -> None:
        with Store(self.db) as st:
            self._seed(st)
            self.assertEqual(st.merge_user_tags(["自用"], "备份"), 1)
            self.assertIn("备份", self._user_tags(st, self._LIVE))
            self.assertIn("自用", self._user_tags(st, self._TRASHED))


class StoreTagTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = os.path.join(_ROOT, "tmp_store_tags_" + uuid.uuid4().hex[:8])
        os.makedirs(self.root)
        self.db = os.path.join(self.root, "sources.sqlite3")
        # 数据目录一并隔离：软删除会往 `data_path("backups", "deleted.jsonl")`
        # 追加记录，不设 LEGADO_DATA_DIR 就写进真实的 data/backups/
        self._old_data_dir = os.environ.get("LEGADO_DATA_DIR")
        os.environ["LEGADO_DATA_DIR"] = self.root

    def tearDown(self) -> None:
        if self._old_data_dir is None:
            os.environ.pop("LEGADO_DATA_DIR", None)
        else:
            os.environ["LEGADO_DATA_DIR"] = self._old_data_dir
        shutil.rmtree(self.root, ignore_errors=True)

    def test_upsert_splits_system_and_user_tags(self) -> None:
        with Store(self.db) as st:
            st.upsert_sources([make_source("📖小说,可用,原创")])
            row = st.conn.execute("SELECT group_name, user_tags FROM sources").fetchone()
            self.assertEqual(row["group_name"], "📖小说,可用")
            self.assertEqual(row["user_tags"], "原创")
            self.assertEqual(st.export_sources()[0]["bookSourceGroup"], "📖小说,可用,原创")

    def test_conflict_preserves_user_tags(self) -> None:
        with Store(self.db) as st:
            st.upsert_sources([make_source("原创")])
            st.upsert_sources([make_source("📖小说,可用,其他")])
            row = st.conn.execute("SELECT group_name, user_tags FROM sources").fetchone()
            self.assertEqual(row["group_name"], "📖小说,可用")
            self.assertEqual(row["user_tags"], "原创")

    def test_type_change_rebuilds_type_tag_keeps_health_quality(self) -> None:
        with Store(self.db) as st:
            st.upsert_sources([make_source(
                "📥下载,可用,原创,规则完整",
                url="https://type-change.example",
                source_type=3,
            )])
            st.upsert_sources([make_source(
                "📥下载,可用,原创,规则完整",
                url="https://type-change.example",
                source_type=0,
            )])
            row = st.conn.execute(
                "SELECT group_name, user_tags FROM sources").fetchone()
            self.assertEqual(row["group_name"], "📖小说,可用,规则完整")
            self.assertEqual(row["user_tags"], "原创")
            self.assertEqual(
                st.export_sources()[0]["bookSourceGroup"],
                "📖小说,可用,规则完整,原创",
            )

    def test_add_remove_rename_merge(self) -> None:
        with Store(self.db) as st:
            st.upsert_sources([make_source("原创")])
            self.assertEqual(st.add_user_tags(["https://example.com"], ["精排", "自用"]), 1)
            self.assertEqual(st.remove_user_tags(["https://example.com"], ["自用"]), 1)
            self.assertEqual(st.rename_user_tag("精排", "排版优"), 1)
            self.assertEqual(st.merge_user_tags(["排版优"], "精排"), 1)
            self.assertEqual(st.export_sources()[0]["bookSourceGroup"], "📖小说,待验证,原创,精排")

    def test_tags_overview_marks_system_and_user(self) -> None:
        with Store(self.db) as st:
            st.upsert_sources([make_source("📖小说,可用,原创")])
            overview = {x["tag"]: x for x in st.tags_overview()}
            self.assertFalse(overview["可用"]["editable"])
            self.assertEqual(overview["可用"]["kind"], "system")
            self.assertTrue(overview["原创"]["editable"])
            self.assertEqual(overview["原创"]["kind"], "user")

    def test_download_source_type_tag_is_system(self) -> None:
        with Store(self.db) as st:
            st.upsert_sources([make_source(
                "📥下载,可用,原创", url="https://download.example", source_type=3)])
            row = st.conn.execute(
                "SELECT group_name, user_tags FROM sources").fetchone()
            self.assertEqual(row["group_name"], "📥下载,可用")
            self.assertEqual(row["user_tags"], "原创")
            overview = {x["tag"]: x for x in st.tags_overview()}
            self.assertEqual(overview["📥下载"]["kind"], "system")

    def test_cleanup_system_tags_from_user_tags(self) -> None:
        with Store(self.db) as st:
            st.upsert_sources([make_source(
                "📥下载,可用,原创", url="https://download.example", source_type=3)])
            st.conn.execute(
                "UPDATE sources SET user_tags=? WHERE source_url=?",
                ("📥下载,原创", "https://download.example"))
            st.conn.execute(
                "DELETE FROM meta WHERE key=?", ("system_tags_cleaned_at",))
            st.conn.commit()
            self.assertTrue(st.cleanup_system_tags_once())
            row = st.conn.execute(
                "SELECT user_tags FROM sources WHERE source_url=?",
                ("https://download.example",)).fetchone()
            self.assertEqual(row["user_tags"], "原创")

    def test_system_status_override_survives_rebuild_and_upsert(self) -> None:
        with Store(self.db) as st:
            st.upsert_sources([make_source("原创")])
            self.assertEqual(
                st.set_system_tags_override(
                    ["https://example.com"], "📖小说,已失效"), 1)
            row = st.conn.execute(
                "SELECT group_name, user_tags, system_tags_locked FROM sources"
            ).fetchone()
            self.assertEqual(row["group_name"], "📖小说,已失效")
            self.assertEqual(row["user_tags"], "原创")
            self.assertEqual(row["system_tags_locked"], 1)

            # 自动重建必须跳过锁定行
            st.rebuild_system_tags()
            row = st.conn.execute("SELECT group_name FROM sources").fetchone()
            self.assertEqual(row["group_name"], "📖小说,已失效")

            # 普通导入更新也不能覆盖人工状态，但用户标签继续保留
            st.upsert_sources([make_source("📖小说,可用,原创")])
            row = st.conn.execute(
                "SELECT group_name, user_tags, system_tags_locked FROM sources"
            ).fetchone()
            self.assertEqual(row["group_name"], "📖小说,已失效")
            self.assertEqual(row["user_tags"], "原创")
            self.assertEqual(row["system_tags_locked"], 1)

            # 恢复自动后按校验结果重建
            st.clear_system_tags_override(["https://example.com"])
            row = st.conn.execute(
                "SELECT group_name, user_tags, system_tags_locked FROM sources"
            ).fetchone()
            self.assertEqual(row["group_name"], "📖小说,待验证")
            self.assertEqual(row["user_tags"], "原创")
            self.assertEqual(row["system_tags_locked"], 0)

    def test_query_by_user_tag(self) -> None:
        with Store(self.db) as st:
            st.upsert_sources([make_source("原创")])
            self.assertEqual(len(st.query(user_tag="原创")), 1)
            self.assertEqual(len(st.query(user_tag="精排")), 0)

    def test_new_source_unknown_tags_are_filtered_after_aliases(self) -> None:
        with Store(self.db) as st:
            st.upsert_sources([make_source("R18,精排,原创", url="https://old.example")])
            st.upsert_sources([make_source("H漫,精品排版,原创,未知", url="https://new.example")])
            row = st.conn.execute(
                "SELECT user_tags FROM sources WHERE source_url=?", ("https://new.example",)).fetchone()
            self.assertEqual(row["user_tags"], "R18,精排,原创")
            self.assertEqual(st.export_sources()[1]["bookSourceGroup"], "📖小说,待验证,R18,精排,原创")

    def test_migration_from_legacy_schema(self) -> None:
        raw = make_source("📖小说,可用,原创,精排")
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute(
            "CREATE TABLE sources (id INTEGER PRIMARY KEY, source_url TEXT UNIQUE, "
            "name TEXT, source_type INTEGER, group_name TEXT, enabled INTEGER, "
            "raw_json TEXT, fingerprint TEXT, deleted_at TEXT NOT NULL DEFAULT '', "
            "created_at TEXT, updated_at TEXT)")
        conn.execute(
            "INSERT INTO sources(source_url,name,source_type,group_name,enabled,raw_json,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (_normalize_url(raw["bookSourceUrl"]), raw["bookSourceName"], 0, raw["bookSourceGroup"],
             1, json.dumps(raw, ensure_ascii=False), "2026-01-01 00:00:00", "2026-01-01 00:00:00"))
        conn.commit()
        conn.close()
        with Store(self.db) as st:
            row = st.conn.execute("SELECT group_name, user_tags FROM sources").fetchone()
            self.assertEqual(row["group_name"], "📖小说,可用")
            self.assertEqual(row["user_tags"], "原创,精排")
            self.assertEqual(st.export_sources()[0]["bookSourceGroup"], "📖小说,可用,原创,精排")


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------- 变异记录
# 以下为实测（改坏 → `python -B -m unittest tests.test_store_tags.RecycleBinTagScopeTests`
# → 确认变红 → 还原）。**必须带 `-B`**，理由见 test_reclassify.py 的脚注。
#
# 三个方法各是**一份独立的 SQL**，所以逐个变异、逐个断言——
# 合成一条的话，第一个失败后面的就不再执行，另外两处漏改看不出来。
#
#  M1  `delete_user_tag` 去掉 `WHERE deleted_at = ''`
#        → test_delete_user_tag_skips_the_recycle_bin 红（另外两条仍绿）
#  M2  `rename_user_tag` 去掉 `WHERE deleted_at = ''`
#        → test_rename_user_tag_skips_the_recycle_bin 红（另外两条仍绿）
#  M3  `merge_user_tags` 去掉 `WHERE deleted_at = ''`
#        → test_merge_user_tags_skips_the_recycle_bin 红（另外两条仍绿）