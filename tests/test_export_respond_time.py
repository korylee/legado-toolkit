# -*- coding: utf-8 -*-
"""导出回填 respondTime：只认引擎 ok 结论的实测耗时（App 换源排序吃这个字段）。

守的是三条边界：
1. 失败结论的耗时是被截断的数据（timeout 行 cost≈预算），写回去污染排序；
2. 没有引擎结论的源不动——源里自带的 respondTime 是 App 自己测的，原样保留；
3. URL 两侧都要归一（AGENTS #5）：源 JSON 里是原文，checks 键是归一化的；
4. 源规则变化后，旧 fingerprint 的耗时不能写给新源。
"""

from __future__ import annotations

import os
import shutil
import unittest
import uuid

from core.jvm_health import store_checks
from core.store import Store


_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
os.makedirs(_ROOT, exist_ok=True)


class RespondTimeExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = os.path.join(_ROOT, "tmp_export_rt_" + uuid.uuid4().hex[:8])
        os.makedirs(self.root)
        self.db = os.path.join(self.root, "sources.sqlite3")

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    @staticmethod
    def _seed(st: Store, url: str, respond_time: int = 0) -> None:
        src = {"bookSourceName": "甲", "bookSourceUrl": url, "enabled": True,
               "ruleSearch": {"bookList": ".b"}}
        if respond_time:
            src["respondTime"] = respond_time
        st.upsert_sources([src])

    @staticmethod
    def _exported(st: Store, url: str) -> dict:
        return next(s for s in st.export_sources() if s["bookSourceUrl"] == url)

    def test_ok_engine_check_backfills_respond_time(self) -> None:
        with Store(self.db) as st:
            self._seed(st, "https://a.com")
            store_checks([{"url": "https://a.com", "state": "ok", "stage": "search",
                           "hit": 1, "sample": ["x"], "cost_ms": 1234}],
                         batch="b", store=st)
            self.assertEqual(self._exported(st, "https://a.com")["respondTime"], 1234)

    def test_failed_check_does_not_backfill(self) -> None:
        with Store(self.db) as st:
            self._seed(st, "https://a.com", respond_time=500)
            store_checks([{"url": "https://a.com", "state": "timeout", "stage": "search",
                           "cost_ms": 75000, "reason": "搜索超时（每源总预算 75s 用尽）"}],
                         batch="b", store=st)
            self.assertEqual(self._exported(st, "https://a.com")["respondTime"], 500)

    def test_url_is_normalized_on_both_sides(self) -> None:
        """源 JSON 里是原文（大写 host），checks 键是归一化的——不归一一条都对不上。"""
        with Store(self.db) as st:
            self._seed(st, "https://A.com/s")
            store_checks([{"url": "https://a.com/s", "state": "ok", "stage": "search",
                           "hit": 1, "sample": ["x"], "cost_ms": 777}],
                         batch="b", store=st)
            self.assertEqual(self._exported(st, "https://A.com/s")["respondTime"], 777)

    def test_source_without_check_keeps_its_own_value(self) -> None:
        with Store(self.db) as st:
            self._seed(st, "https://b.com", respond_time=321)
            self.assertEqual(self._exported(st, "https://b.com")["respondTime"], 321)

    def test_source_without_any_value_stays_absent(self) -> None:
        with Store(self.db) as st:
            self._seed(st, "https://c.com")
            self.assertNotIn("respondTime", self._exported(st, "https://c.com"))

    def test_changed_source_does_not_reuse_old_check(self) -> None:
        with Store(self.db) as st:
            self._seed(st, "https://changed.com")
            store_checks([{"url": "https://changed.com", "state": "ok", "stage": "search",
                           "hit": 1, "sample": ["x"], "cost_ms": 888}],
                         batch="b", store=st)
            st.upsert_sources([{
                "bookSourceName": "甲新版", "bookSourceUrl": "https://changed.com",
                "enabled": True, "ruleSearch": {"bookList": ".changed"},
            }])
            self.assertNotIn("respondTime", self._exported(st, "https://changed.com"))

    def test_filtered_and_selected_exports_use_same_finalizer(self) -> None:
        with Store(self.db) as st:
            self._seed(st, "https://same.com")
            store_checks([{"url": "https://same.com", "state": "ok", "stage": "search",
                           "hit": 1, "sample": ["x"], "cost_ms": 456}],
                         batch="b", store=st)
            filtered = st.export_by_filter(q="甲")
            self.assertEqual(filtered[0]["respondTime"], 456)
            selected = st.finalize_export([st.get_source("https://same.com")])
            self.assertEqual(selected[0]["respondTime"], 456)

if __name__ == "__main__":
    unittest.main()
