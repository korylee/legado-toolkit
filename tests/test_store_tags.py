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


class StoreTagTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = os.path.join(_ROOT, "tmp_store_tags_" + uuid.uuid4().hex[:8])
        os.makedirs(self.root)
        self.db = os.path.join(self.root, "sources.sqlite3")

    def tearDown(self) -> None:
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