# -*- coding: utf-8 -*-
"""名称清洗的三个接口：preview（只读）→ apply（改名）→ undo（回写旧名）。

守三件事：
  1. **改名必须同时改两处**（`sources.name` 列与 `raw_json["bookSourceName"]`）——
     只改一处的话，界面上改了名、导出的 JSON 里还是旧的；
  2. **改名必须重算 `fingerprint` 列**——它把 `bookSourceName` 算在内，不重算会让
     导入去重把「改过名」误判成「规则冲突」；
  3. undo 拿 apply 返回的 prev 就能回到原状（不需要另存快照）。
"""

from __future__ import annotations

import json
import os
import shutil
import unittest
import uuid

from core.loader import fingerprint
from core.store import Store

_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
os.makedirs(_ROOT, exist_ok=True)


def make_source(url: str, name: str) -> dict:
    return {"bookSourceName": name, "bookSourceUrl": url, "bookSourceType": 0,
            "bookSourceGroup": "", "enabled": True,
            "ruleSearch": {"bookList": ".book"}}


class _StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = os.path.join(_ROOT, "tmp_name_api_" + uuid.uuid4().hex[:8])
        os.makedirs(self.root)
        self.db = os.path.join(self.root, "sources.sqlite3")

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _seed(self) -> Store:
        st = Store(self.db)
        st.upsert_sources([
            make_source("https://ok.com", "  去读书🎃  "),      # 首尾空白 + 装饰 → 会改名
            make_source("https://dead.com", "已经干净的名字"),   # 不变
        ])
        return st


class StoreNameWriteTests(_StoreCase):
    def test_rename_updates_both_column_and_raw_json(self):
        with self._seed() as st:
            old = st.set_source_name("https://ok.com", "去读书")
            self.assertEqual(old, "  去读书🎃  ")
            row = st.conn.execute(
                "SELECT name, raw_json FROM sources WHERE source_url = ?",
                ("https://ok.com",)).fetchone()
        self.assertEqual(row["name"], "去读书")
        self.assertEqual(json.loads(row["raw_json"])["bookSourceName"], "去读书")

    def test_rename_refreshes_fingerprint(self):
        """改名后 fingerprint 列必须等于「照当前 raw_json 重算一遍」的结果。

        这一列在导入去重里当「规则有没有变」的判据（`fingerprint` 含
        `bookSourceName`）。不重算的话，同一个源改名后再导入一次会被判成
        「规则冲突」——而它其实一点没变。
        """
        with self._seed() as st:
            st.set_source_name("https://ok.com", "去读书")
            row = st.conn.execute(
                "SELECT fingerprint, raw_json FROM sources WHERE source_url = ?",
                ("https://ok.com",)).fetchone()
        self.assertEqual(row["fingerprint"], fingerprint(json.loads(row["raw_json"])))

    def test_unknown_url_returns_none(self):
        with self._seed() as st:
            self.assertIsNone(st.set_source_name("https://nope.com", "x"))

    def test_name_pairs_honors_url_subset(self):
        with self._seed() as st:
            self.assertEqual(len(st.name_pairs()), 2)
            self.assertEqual([r["url"] for r in st.name_pairs(["https://ok.com"])],
                             ["https://ok.com"])
            self.assertEqual(st.name_pairs([""]), [])


class PreviewTests(_StoreCase):
    def test_preview_only_lists_changed_and_writes_nothing(self):
        from backend.api.sources import preview_names
        from backend.schemas import NamePreviewIn
        with self._seed() as st:
            res = preview_names(NamePreviewIn(), st=st)
            after = st.conn.execute(
                "SELECT name FROM sources WHERE source_url = ?",
                ("https://ok.com",)).fetchone()["name"]
        self.assertEqual(res["total"], 2)
        self.assertEqual([i["url"] for i in res["items"]], ["https://ok.com"])
        self.assertEqual(res["items"][0]["new_name"], "去读书")
        self.assertIn("reason_labels", res)
        self.assertIn("strip_decor", res["reason_labels"])
        self.assertEqual(after, "  去读书🎃  ", "预演是只读的，不能改数据")

    def test_preview_marks_collisions(self):
        """两组不同写法、清洗后同名 → 都要带 collides 标记。

        那正是下一步「同名档」要摊开的东西；不标的话用户改完一片同名也看不出因果。
        """
        from backend.api.sources import preview_names
        from backend.schemas import NamePreviewIn
        with Store(self.db) as st:
            st.upsert_sources([
                make_source("https://a.com", "读书吧🎃"),
                make_source("https://b.com", "读书吧"),
            ])
            res = preview_names(NamePreviewIn(), st=st)
        self.assertEqual(len(res["items"]), 1)
        self.assertTrue(res["items"][0]["collides"])


class ApplyUndoTests(_StoreCase):
    def test_apply_returns_prev_and_undo_restores(self):
        from backend.api.sources import apply_names, undo_names
        from backend.schemas import NameApplyIn, NameChange, NameUndoIn
        with self._seed() as st:
            res = apply_names(NameApplyIn(changes=[
                NameChange(url="https://ok.com", name="去读书")]), st=st)
            self.assertEqual(res["applied"], 1)
            self.assertEqual(res["prev"], [{"url": "https://ok.com", "name": "  去读书🎃  "}])
            self.assertEqual(st.conn.execute(
                "SELECT name FROM sources WHERE source_url = ?",
                ("https://ok.com",)).fetchone()["name"], "去读书")

            undo = undo_names(NameUndoIn(prev=[NameChange(**res["prev"][0])]), st=st)
            self.assertEqual(undo["restored"], 1)
            row = st.conn.execute(
                "SELECT name, raw_json FROM sources WHERE source_url = ?",
                ("https://ok.com",)).fetchone()
        self.assertEqual(row["name"], "  去读书🎃  ")
        self.assertEqual(json.loads(row["raw_json"])["bookSourceName"], "  去读书🎃  ")

    def test_blank_name_is_skipped(self):
        """空名会让列表上出现一条没名字的源，而这是不可逆的观感损失 → 直接跳过。"""
        from backend.api.sources import apply_names
        from backend.schemas import NameApplyIn, NameChange
        with self._seed() as st:
            res = apply_names(NameApplyIn(changes=[
                NameChange(url="https://ok.com", name="   ")]), st=st)
            self.assertEqual(res["applied"], 0)
            self.assertEqual(st.conn.execute(
                "SELECT name FROM sources WHERE source_url = ?",
                ("https://ok.com",)).fetchone()["name"], "  去读书🎃  ")

    def test_missing_url_is_counted_not_raised(self):
        from backend.api.sources import apply_names
        from backend.schemas import NameApplyIn, NameChange
        with self._seed() as st:
            res = apply_names(NameApplyIn(changes=[
                NameChange(url="https://nope.com", name="x")]), st=st)
        self.assertEqual(res["applied"], 0)
        self.assertEqual(res["missing"], 1)


# ---------------------------------------------------------------- 变异记录
# 以下为实测（照项目惯例：改坏 → 跑 → 确认变红 → 改回）。
#
#  M1  `set_source_name` 的 UPDATE 去掉 `fingerprint = ?`（不重算指纹）
#        → StoreNameWriteTests.test_rename_refreshes_fingerprint 等 3 条红


if __name__ == "__main__":
    unittest.main()
