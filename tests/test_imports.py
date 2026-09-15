# -*- coding: utf-8 -*-
"""Web 导入接口（/api/import）的安全导入三分类测试。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

_TMP_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".tmp")
os.makedirs(_TMP_ROOT, exist_ok=True)


def make_source(url: str, search_url: str = "https://example.com/search?q={{key}}") -> dict:
    return {
        "bookSourceName": "示例书源",
        "bookSourceUrl": url,
        "bookSourceType": 0,
        "searchUrl": search_url,
        "ruleSearch": {"bookList": ".book"},
    }


class ImportApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(dir=_TMP_ROOT)
        self._old_data_dir = os.environ.get("LEGADO_DATA_DIR")
        os.environ["LEGADO_DATA_DIR"] = self.temp_dir.name

        from core.store import Store
        self.store = Store()

    def tearDown(self) -> None:
        self.store.close()
        if self._old_data_dir is None:
            os.environ.pop("LEGADO_DATA_DIR", None)
        else:
            os.environ["LEGADO_DATA_DIR"] = self._old_data_dir
        self.temp_dir.cleanup()

    def _import(self, sources: list[dict]) -> dict:
        from backend.schemas import ImportBody
        from backend.api.imports import import_sources
        return import_sources(ImportBody(content=json.dumps(sources), source="t.json"), self.store)

    def test_new_url_is_marked_pending(self) -> None:
        result = self._import([make_source("https://new.example")])
        self.assertEqual(result["new_count"], 1)
        self.assertEqual(result["conflict_count"], 0)
        source = self.store.get_source("https://new.example")
        self.assertIn("待验证", source.get("bookSourceGroup", ""))

    def test_import_preserves_health_status_and_user_tags(self) -> None:
        # 先种一个源让库非空，触发用户标签过滤分支
        self.store.upsert_sources([make_source("https://seed.example")])
        source = make_source("https://tagged.example")
        source["bookSourceGroup"] = "📖小说,✅可用,精排,R18"
        result = self._import([source])
        self.assertEqual(result["new_count"], 1)
        stored = self.store.get_source("https://tagged.example")
        group = stored.get("bookSourceGroup", "")
        self.assertIn("可用", group)
        self.assertIn("精排", group)
        self.assertIn("R18", group)

    def test_same_url_same_rule_is_duplicate(self) -> None:
        self._import([make_source("https://same.example")])
        result = self._import([make_source("https://same.example")])
        self.assertEqual(result["duplicate_count"], 1)
        self.assertEqual(result["new_count"], 0)

    def test_changed_rule_is_conflict_and_not_overwritten(self) -> None:
        self._import([make_source("https://conflict.example",
                                 "https://conflict.example/search?q={{key}}")])
        result = self._import([make_source("https://conflict.example",
                                           "https://conflict.example/find?q={{key}}")])
        self.assertEqual(result["conflict_count"], 1)
        self.assertTrue(result["conflict_file"])
        source = self.store.get_source("https://conflict.example")
        self.assertEqual(source.get("searchUrl"), "https://conflict.example/search?q={{key}}")
