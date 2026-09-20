# -*- coding: utf-8 -*-
"""跑批的范围：**只跑选中的那几条**（S5-A 第二期）。

守三件事，都是「不报错但结果不对」的那一类：

1. **两侧都要归一 URL**（AGENTS #5）：前端给的是库里归一化过的 `source_url`，而导出的是
   源 JSON 里 `bookSourceUrl` 的**原文**——不归一就一条都匹配不上，而表现是「跑完了但
   JVM 列没变」，谁也不报错。
2. **一条都没匹配上要明说**：那种情况下开跑一次空批，界面上会说「完成：0 条结论已入库」，
   读起来像「这些源没问题」。
3. **选了具体几条就不看条数上限**：否则「选了 20 条只跑了 3 条」是一次看不出来的截断。

不跑 Gradle、不联网：`_export_sources_file` 用真的（它只读库 + 写一个临时 JSON），
`_run_gradle` / `selftest` / `Store` 打桩。
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import shutil
import tempfile
import unittest
import uuid
from unittest import mock

from backend.api import jvm as jvm_api
from backend.schemas import JvmRunRequest
from core.loader import _normalize_url
from core.store import Store


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="jvm_scope_"))
        self.addCleanup(shutil.rmtree, str(self.tmp), ignore_errors=True)
        self.db = str(self.tmp / "sources.sqlite3")
        self.probe = self.tmp / "probe"
        self.probe.mkdir()
        (self.probe / "appservice").mkdir()
        (self.probe / "appservice" / "legado-gradle.bat").write_text("@echo off\n",
                                                                    encoding="utf-8")
        self.gradle_calls = 0
        for p in (
            mock.patch.object(jvm_api, "_AGSVC", self.probe / "appservice"),
            mock.patch.object(jvm_api, "data_dir", lambda: self.probe / "data"),
            mock.patch.object(jvm_api, "selftest", lambda repo: {"ok": True, "checks": []}),
            mock.patch.object(jvm_api, "_write_meta", lambda rows: "testbatch"),
            mock.patch.object(jvm_api, "_run_gradle", self._fake_gradle),
            mock.patch.object(jvm_api.settings_store, "load",
                              lambda: {"jvm": {"app_repo": "X:/repo", "keyword": "我",
                                               "timeout": 25, "concurrency": 8,
                                               "limit": 2, "depth": "search"}}),
        ):
            p.start()
            self.addCleanup(p.stop)
        self._put_source("https://A.com/", "大写 + 尾斜杠的原文")
        self._put_source("https://b.com", "普通源")
        self._put_source("https://c.com/", "另一条")

    def _fake_gradle(self) -> int:
        self.gradle_calls += 1
        out = self.probe / "data" / "app_probe" / "jvm_results.jsonl"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"url": "https://a.com", "state": "ok"}),
                       encoding="utf-8")
        return 0

    def _put_source(self, url: str, name: str) -> None:
        # 用真 API 写源（`upsert_sources` 会建表+算 fingerprint）：比手写 INSERT 更接近
        # 生产路径，也不会因为漏 NOT NULL 列而炸
        with Store(self.db) as st:
            st.upsert_sources([{"bookSourceName": name, "bookSourceUrl": url,
                                "enabled": True}])

    def _call(self, urls=None, filt=None) -> dict:
        body = JvmRunRequest(urls=urls or [], filter=filt or {})
        with mock.patch.object(jvm_api, "Store", lambda *a, **kw: Store(self.db)):
            return asyncio.run(jvm_api.jvm_run(body))

    def _batch(self) -> list:
        f = self.probe / "data" / "app_probe" / "jvm_batch.json"
        return json.loads(f.read_text(encoding="utf-8")) if f.exists() else []


class ScopeTests(_Base):
    def test_selector_matches_despite_case_and_trailing_slash(self) -> None:
        """**归一后才比**：库里那条的原文是 `https://A.com/`（大写 + 尾斜杠），
        而列表页给的是归一化过的 `https://a.com`。不归一的话这里会一条都不跑。"""
        self._call(urls=["https://a.com"])
        got = [s["bookSourceUrl"] for s in self._batch()]
        self.assertEqual(got, ["https://A.com/"], "选中的那条要按原文导出（给 JVM 用的是原文）")

    def test_only_selected_sources_are_exported(self) -> None:
        self._call(urls=["https://b.com", "https://c.com"])
        self.assertEqual(sorted(s["bookSourceUrl"] for s in self._batch()),
                         ["https://b.com", "https://c.com/"])

    def test_selection_ignores_the_limit(self) -> None:
        """设置里条数上限 = 2，但**选中 3 条就得跑 3 条**——
        截断会静默：界面上只会看到「选了 3 条」而实际跑了 2 条。"""
        self._call(urls=["https://a.com", "https://b.com", "https://c.com"])
        self.assertEqual(len(self._batch()), 3)
        self.assertEqual(self.gradle_calls, 1)

    def test_unmatched_selection_is_refused_with_a_reason(self) -> None:
        r = self._call(urls=["https://nope.com"])
        self.assertFalse(r["started"])
        self.assertIn("一条都没匹配上", r["reason"])
        self.assertEqual(self.gradle_calls, 0, "匹配不上就不该开跑（空批会被读成「都没问题」）")

    def test_all_sources_still_honours_the_limit(self) -> None:
        """不选（全量）时条数上限照旧生效——那是给「先跑几条看看」用的安全阀。"""
        r = self._call()
        self.assertTrue(r["started"])
        self.assertEqual(len(self._batch()), 2, "limit=2")

    def test_normalized_selector_is_idempotent(self) -> None:
        """再跑一次归一（前端偶尔给已经归一的、也偶尔给原文）结果一样。"""
        self._call(urls=["https://a.com/"])
        first = [s["bookSourceUrl"] for s in self._batch()]
        self.assertEqual(first, ["https://A.com/"])


class FilterScopeTests(_Base):
    """范围也可以给「当前筛选」：一台引擎之后这是最省时间的杠杆。

    全量一次十几分钟，而**站点按 IP 认人**（频繁跑全量会被封）——所以「只重跑待验证
    那一批」不是锦上添花，是主要用法。形状与 `POST /api/export` 的 `filter` 一致，
    前端把列表页那个查询对象原样递过来。
    """

    def _mark_all(self, health: str) -> None:
        with Store(self.db) as st:
            st.save_checks([{"url": u, "health": health,
                             "checked_at": "2026-09-20 10:00:00"}
                            for u in ("https://a.com", "https://b.com", "https://c.com")])

    def test_filter_selects_the_matching_sources(self) -> None:
        self._mark_all("gfw")
        self._call(filt={"health": "gfw"})
        self.assertEqual(sorted(s["bookSourceUrl"] for s in self._batch()),
                         ["https://A.com/", "https://b.com", "https://c.com/"])

    def test_filter_ignores_the_limit(self) -> None:
        """与「选中」同一条：范围既然明确给了，再按条数上限截断就是一次看不出来的截断。"""
        self._mark_all("pending")
        self._call(filt={"health": "pending"})
        self.assertEqual(len(self._batch()), 3, "limit=2 但筛选命中 3 条")

    def test_filter_by_keyword(self) -> None:
        self._call(filt={"q": "普通"})
        self.assertEqual([s["bookSourceUrl"] for s in self._batch()], ["https://b.com"])

    def test_empty_filter_is_refused_with_a_reason(self) -> None:
        r = self._call(filt={"health": "dead"})
        self.assertFalse(r["started"])
        self.assertIn("筛选", r["reason"])
        self.assertEqual(self.gradle_calls, 0, "筛选没命中就不该开跑")

    def test_selection_wins_over_filter(self) -> None:
        """两者都给时**勾选优先**：勾是明确意图，筛选是「这一屏里的」。"""
        self._call(urls=["https://b.com"], filt={"q": "普通"})
        self.assertEqual([s["bookSourceUrl"] for s in self._batch()], ["https://b.com"])


class ExportTests(_Base):
    """导出这一步本身。**这里曾经是个真缺陷**：`export_sources()` 给的是解析好的书源
    对象，而导出还在按「带 raw_json 的包装」读 → `d = None` → 任何有在用源的库都抛异常，
    也就是说 `/api/jvm/run` 从来没在真库上跑通过（测试把这个函数整个打桩了，而历史上的
    批量是走 CLI + 导出文件那条路）。"""

    def test_export_reads_the_view_directly(self) -> None:
        with Store(self.db) as st:
            path = jvm_api._export_sources_file(st)
        rows = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
        self.assertEqual(len(rows), 3)
        # 只导出 JVM 侧要看的那几个键，且**不炸**（原先是 `d = None` → TypeError）
        self.assertTrue({"bookSourceName", "bookSourceUrl", "enabled"} <= set(rows[0]), rows[0])

    def test_export_skips_disabled_sources(self) -> None:
        """`enabled` 的权威在**源 JSON 里**（列是 `upsert_sources` 从它写下来的，
        界面的开关也走 `/save` 改 JSON）——所以这里按真实路径改，不是改列。"""
        with Store(self.db) as st:
            st.upsert_sources([{"bookSourceName": "普通源", "bookSourceUrl": "https://b.com",
                                "enabled": False}], allow_new_tags=True)
        with Store(self.db) as st:
            rows = json.loads(pathlib.Path(jvm_api._export_sources_file(st))
                              .read_text(encoding="utf-8"))
        self.assertEqual(sorted(r["bookSourceUrl"] for r in rows),
                         ["https://A.com/", "https://c.com/"])


if __name__ == "__main__":
    unittest.main()
