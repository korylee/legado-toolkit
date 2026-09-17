# -*- coding: utf-8 -*-
"""合并重复源（`services/merge_sources`）：干跑 → 合并 → 撤销。

守四件事：
  1. **判据在后端重算**：跨站点、规则不同的组一律拒绝（前端只是入口，这步是删源）；
  2. **`tags_added` 只装真正新增的**：原样回传会在撤销时把用户本来就有的标签摘掉；
  3. **干跑不写库**：它的输出就是确认框要渲染的那份清单，写了库就等于"预览即执行"；
  4. **撤销回到合并前**：被删条回来、真增的标签摘掉、备注还原。
"""

from __future__ import annotations

import json
import os
import shutil
import unittest
import uuid

from core.paths import data_path
from core.store import Store
from services.merge_sources import MergeRejected, merge, undo

_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
os.makedirs(_ROOT, exist_ok=True)

RULES = {"ruleSearch": {"bookList": ".book"}, "ruleToc": {"chapterList": ".ch"},
         "ruleContent": {"content": ".content"}}


def make_source(url: str, name: str, comment: str = "", group: str = "",
                rules: dict = None) -> dict:
    """默认那一份 `RULES` 就是「行为相同」；要造"同站不同规则"时显式传另一份。"""
    return {"bookSourceName": name, "bookSourceUrl": url, "bookSourceType": 0,
            "bookSourceGroup": group, "bookSourceComment": comment,
            "enabled": True, **(rules or RULES)}


class _MergeCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = os.path.join(_ROOT, "tmp_merge_" + uuid.uuid4().hex[:8])
        os.makedirs(self.root)
        self.db = os.path.join(self.root, "sources.sqlite3")
        # 快照目录一并隔离：合并会给 deleted.jsonl 追加一条（含 reason）
        self._old_data_dir = os.environ.get("LEGADO_DATA_DIR")
        os.environ["LEGADO_DATA_DIR"] = self.root

    def tearDown(self) -> None:
        if self._old_data_dir is None:
            os.environ.pop("LEGADO_DATA_DIR", None)
        else:
            os.environ["LEGADO_DATA_DIR"] = self._old_data_dir
        shutil.rmtree(self.root, ignore_errors=True)

    def _seed(self) -> Store:
        st = Store(self.db)
        st.upsert_sources([
            make_source("https://a.com", "甲站", comment="自写", group=""),
            make_source("https://a.com#🎃", "甲站备用", group=""),
            make_source("https://b.com", "乙站"),          # 跨站点
            make_source("https://a.com/other", "甲站另一条",     # 同站但规则不同
                        rules={**RULES, "ruleSearch": {"bookList": ".other"}}),
        ])
        return st

    def _tags(self, st: Store, url: str) -> list:
        src = st.get_source(url)
        from core.tags import extract_user_tags_from_group
        return sorted(extract_user_tags_from_group(src.get("bookSourceGroup") or ""))

    def _comment(self, st: Store, url: str) -> str:
        return str(st.get_source(url).get("bookSourceComment") or "")

    def _snapshot_records(self) -> list:
        p = data_path("backups", "deleted.jsonl")
        if not os.path.exists(p):
            return []
        with open(p, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]


class RejectionTests(_MergeCase):
    def test_cross_site_is_rejected(self):
        with self._seed() as st:
            with self.assertRaises(MergeRejected) as ctx:
                merge(st, "https://a.com", ["https://b.com"])
        self.assertIn("跨站点", str(ctx.exception))

    def test_different_rules_are_rejected(self):
        with self._seed() as st:
            with self.assertRaises(MergeRejected) as ctx:
                merge(st, "https://a.com", ["https://a.com/other"])
        self.assertIn("规则不同", str(ctx.exception))

    def test_same_site_different_port_is_rejected(self):
        """同 host 不同端口是两个站点（`site_key` 的硬边界）。"""
        with Store(self.db) as st:
            st.upsert_sources([make_source("https://c.com:8080", "丙站8080")])
            with self.assertRaises(MergeRejected):
                merge(st, "https://c.com", ["https://c.com:8080"])


class DryRunTests(_MergeCase):
    def test_dry_run_plans_without_writing(self):
        with self._seed() as st:
            st.add_user_tags(["https://a.com#🎃"], ["R18"])
            before = st.count_sources()
            res = merge(st, "https://a.com", ["https://a.com#🎃"], dry_run=True)
            self.assertTrue(res["dry_run"])
            self.assertEqual(res["drop"], ["https://a.com#🎃"])
            self.assertEqual(res["tags_added"], ["R18"])
            self.assertEqual(st.count_sources(), before, "干跑不能写库")
            self.assertEqual(st.count_deleted(), 0)
            self.assertEqual(self._tags(st, "https://a.com"), [])
            self.assertEqual(self._snapshot_records(), [], "干跑不该写删除快照")


class MergeTests(_MergeCase):
    def test_tags_union_reports_only_new(self):
        with self._seed() as st:
            st.add_user_tags(["https://a.com"], ["精排"])
            st.add_user_tags(["https://a.com#🎃"], ["精排", "R18"])
            res = merge(st, "https://a.com", ["https://a.com#🎃"])
            tags_after = self._tags(st, "https://a.com")
        self.assertEqual(res["tags_added"], ["R18"], "只装真正新增的")
        self.assertEqual(tags_after, ["R18", "精排"])

    def test_dropped_source_goes_to_trash_with_reason(self):
        with self._seed() as st:
            res = merge(st, "https://a.com", ["https://a.com#🎃"])
            self.assertEqual(res["merged"], 1)
            self.assertEqual(st.count_sources(), 3)
            self.assertEqual(st.count_deleted(), 1)
        recs = self._snapshot_records()   # 快照文件不受 with 影响
        self.assertEqual(len(recs), 1)
        self.assertIn("合并到", recs[0]["reason"])
        self.assertIn("https://a.com", recs[0]["reason"])

    def test_comment_not_merged_by_default(self):
        with self._seed() as st:
            res = merge(st, "https://a.com", ["https://a.com#🎃"])
        self.assertEqual(res["prev_comment"], "")
        self.assertEqual(res["comment"], "")

    def test_comment_merge_joins_and_dedups(self):
        with Store(self.db) as st:
            st.upsert_sources([
                make_source("https://d.com", "丁站", comment="甲备注"),
                make_source("https://d.com#1", "丁站1", comment="乙备注"),
                make_source("https://d.com#2", "丁站2", comment="甲备注"),   # 完全重复
            ])
            res = merge(st, "https://d.com", ["https://d.com#1", "https://d.com#2"],
                        merge_comment=True)
            self.assertEqual(res["prev_comment"], "甲备注")
            self.assertEqual(res["comment"], "甲备注\n\n乙备注")
            self.assertEqual(self._comment(st, "https://d.com"), "甲备注\n\n乙备注")


class UndoTests(_MergeCase):
    def test_undo_restores_trash_tags_and_comment(self):
        with self._seed() as st:
            st.add_user_tags(["https://a.com"], ["精排"])
            st.add_user_tags(["https://a.com#🎃"], ["精排", "R18"])
            res = merge(st, "https://a.com", ["https://a.com#🎃"], merge_comment=True)
            self.assertEqual(st.count_deleted(), 1)

            back = undo(st, res["keep"], res["restore_urls"],
                        res["tags_added"], res["prev_comment"])
            self.assertEqual(back["restored"], 1)
            self.assertEqual(st.count_deleted(), 0)
            tags_after = self._tags(st, "https://a.com")
            comment_after = self._comment(st, "https://a.com")
        self.assertEqual(tags_after, ["精排"], "真增的标签要摘掉")
        self.assertEqual(comment_after, "自写", "备注要还原")

    def test_undo_without_changes_is_safe(self):
        """备注没改过、标签没并过时，撤销只恢复被删条，不碰别的。"""
        with self._seed() as st:
            res = merge(st, "https://a.com", ["https://a.com#🎃"])
            back = undo(st, res["keep"], res["restore_urls"])
        self.assertEqual(back["restored"], 1)
        self.assertEqual(back["tags_removed"], 0)
        self.assertFalse(back["comment_restored"])


# ---------------------------------------------------------------- 变异记录
# 以下为实测（照项目惯例：改坏 → 跑 → 确认变红 → 改回）。
#
#  M1  `tags_added` 去掉「只装真正新增的」那层过滤（原样回传 union）
#        → MergeTests.test_tags_union_reports_only_new / UndoTests 红 2 条
#          （撤销会把用户本来就有的标签一起摘掉）


if __name__ == "__main__":
    unittest.main()
