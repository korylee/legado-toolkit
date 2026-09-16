# -*- coding: utf-8 -*-
"""书源「全选筛选结果」与批量删除接口的行为测试。

不起 FastAPI app（沿用 test_settings_api 的惯例：本仓库没有 TestClient 先例，
不为几个纯函数路由引入），而是**直接调端点函数**。

守的是 ② 的两件事：
  1. 「选中全部 N 条筛选结果」拿到的 URL 列表与列表页同口径，且**绝不**包含回收站
  2. 批量删除改走 POST body——旧实现走 URL 查询串，实测约 2000 条（72KB）就会被
     服务端拒绝，而全库 3850 条必然超过它
"""

from __future__ import annotations

import inspect
import os
import shutil
import unittest
import uuid

from backend.api.sources import list_source_urls, soft_delete_sources
from backend.schemas import SourceDeleteIn
from core.store import Store


_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
os.makedirs(_ROOT, exist_ok=True)


def make_source(url: str, name: str = "测试源") -> dict:
    return {
        "bookSourceName": name,
        "bookSourceUrl": url,
        "bookSourceType": 0,
        "bookSourceGroup": "",
        "enabled": True,
        "ruleSearch": {"bookList": ".book"},
    }


def make_check(url: str, health: str) -> dict:
    return {
        "v": 8,
        "url": url,
        "fingerprint": "fp-" + url,
        "name": "测试源",
        "health": health,
        "status_code": 200,
        "response_time_ms": 100,
        "checked_at": "2026-09-15 10:00:00",
    }


class _StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = os.path.join(_ROOT, "tmp_sources_api_" + uuid.uuid4().hex[:8])
        os.makedirs(self.root)
        self.db = os.path.join(self.root, "sources.sqlite3")

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _seed(self) -> Store:
        st = Store(self.db)
        st.upsert_sources([
            make_source("https://ok.com", "可用的"),
            make_source("https://dead.com", "失效的"),
            make_source("https://never.com", "从没校验过的"),
        ])
        st.save_checks([
            make_check("https://ok.com", "ok"),
            make_check("https://dead.com", "dead"),
        ])
        return st


class SourceUrlsTests(_StoreCase):
    def test_all_sources_are_returned_with_a_total(self):
        with self._seed() as st:
            out = list_source_urls(st=st)
        self.assertEqual(out["total"], 3)
        self.assertEqual(sorted(out["urls"]), sorted([
            "https://ok.com", "https://dead.com", "https://never.com"]))

    def test_filter_params_reach_the_store(self):
        """筛选参数必须真的透传到 query_urls。

        只测 ``Store.query_urls`` 的话，「端点忘了传 health」这条照样绿——
        test_settings_api 的开头记过这个形状的坑：测了函数、没测接线。
        """
        with self._seed() as st:
            self.assertEqual(list_source_urls(health="ok", st=st)["urls"],
                             ["https://ok.com"])
            self.assertEqual(list_source_urls(health="none", st=st)["urls"],
                             ["https://never.com"])
            self.assertEqual(list_source_urls(q="失效", st=st)["urls"],
                             ["https://dead.com"])

    def test_recycle_bin_is_never_included(self):
        """「全选」必须只在未删除范围内——回收站里的源是用户特意删掉的。"""
        with self._seed() as st:
            st.soft_delete(["https://dead.com"], "测试")
            out = list_source_urls(st=st)
        self.assertNotIn("https://dead.com", out["urls"])
        self.assertEqual(out["total"], 2)

    def test_endpoint_has_no_include_deleted_hole(self):
        """签名里不该有 include_deleted 这个口子。

        留着它，将来某个调用点顺手透传，回收站就会被一起选进来——而这一步的
        下一步是删除。列表接口有那个参数是它的用途需要，这个接口不需要。
        """
        params = inspect.signature(list_source_urls).parameters
        self.assertNotIn("include_deleted", params)


class BatchDeleteTests(_StoreCase):
    def test_urls_come_from_the_request_body(self):
        with self._seed() as st:
            res = soft_delete_sources(
                SourceDeleteIn(urls=["https://ok.com", "https://dead.com"]), st=st)
            self.assertEqual(res["deleted"], 2)
            self.assertEqual(st.count_deleted(), 2)
            self.assertTrue(res["snapshot"])

    def test_reason_is_recorded_in_the_snapshot(self):
        with self._seed() as st:
            res = soft_delete_sources(
                SourceDeleteIn(urls=["https://never.com"], reason="全选筛选结果"), st=st)
            with open(res["snapshot"], "r", encoding="utf-8") as f:
                import json
                payload = json.load(f)
        self.assertEqual(payload["reason"], "全选筛选结果")
        self.assertEqual(payload["count"], 1)

    def test_blank_entries_are_ignored(self):
        """前端可能把空串混进来，别让它变成一条「删不掉的 URL」。"""
        with self._seed() as st:
            res = soft_delete_sources(
                SourceDeleteIn(urls=["https://ok.com", "", "  "]), st=st)
            self.assertEqual(res["deleted"], 1)


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------- 变异记录
# 以下为实测（改坏 → `python -B -m unittest tests.test_sources_api tests.test_store_query`
# → 确认变红 → 还原）。
#
#  M1  端点调 query_urls 时不传 health（接线断了，函数本身仍正确）
#        → test_filter_params_reach_the_store 红
#  M2  Store.query_urls 里 include_deleted 传 True（回收站被一起选中）
#        → test_recycle_bin_is_never_included /
#          test_store_query.QueryUrlsTests.test_deleted_sources_are_excluded 红
