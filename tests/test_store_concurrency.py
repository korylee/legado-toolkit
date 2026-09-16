# -*- coding: utf-8 -*-
"""Store 的两条并发前提。

两条都来自**实际报出来的 500**——而且都只在并发时出现，串行请求永远不复现，
是最难查的那类：

  1. **连接必须能在别的线程里用**。FastAPI 的 sync 依赖（`backend/deps.py` 的
     `get_store`）与 sync 端点各自向 anyio 线程池要线程，**不保证是同一个
     worker**；`sqlite3` 默认的 `check_same_thread=True` 于是随机抛
     `ProgrammingError: SQLite objects created in a thread can only be used in
     that same thread`。
  2. **建 schema 是进程级的一次性动作**。`_init_schema` 里有
     `DROP VIEW v_sources` + `CREATE VIEW`，每个请求都跑的话，并发下几个连接会
     互相把视图删掉再建——实测「view v_sources already exists」与
     「no such table: v_sources」交替出现。

复现方式：起服务后并发打 `/api/sources`（实测 20 并发出 1 次）。见
`core/store.py` 的 `_SCHEMA_READY` 与 `check_same_thread=False` 两处注释。
"""

from __future__ import annotations

import os
import shutil
import threading
import unittest
import uuid

from core.store import Store

_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
os.makedirs(_ROOT, exist_ok=True)


class StoreConcurrencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = os.path.join(_ROOT, "tmp_store_cc_" + uuid.uuid4().hex[:8])
        os.makedirs(self.root)
        self.db = os.path.join(self.root, "sources.sqlite3")

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_store_is_usable_from_another_thread(self):
        """本线程建连接、别的线程用——依赖与端点跑在不同 worker 时就是这个形状。"""
        with Store(self.db) as st:
            errors = []

            def use():
                try:
                    st.count_query()
                    st.query(limit=5)
                except Exception as e:      # noqa: BLE001 —— 要把异常原文带出来比对
                    errors.append("%s: %s" % (type(e).__name__, e))

            t = threading.Thread(target=use)
            t.start()
            t.join()
        self.assertEqual(errors, [])

    def test_concurrent_stores_do_not_fight_over_schema(self):
        """并发建 Store 不能互相把 v_sources 删了又建。"""
        Store(self.db).close()          # 先让库存在（建库那次是串行的）
        errors = []

        def worker():
            try:
                with Store(self.db) as st:
                    st.count_query()
                    st.query(limit=5)
            except Exception as e:      # noqa: BLE001
                errors.append("%s: %s" % (type(e).__name__, e))

        threads = [threading.Thread(target=worker) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])


# ---------------------------------------------------------------- 变异记录
# 以下为实测（照项目惯例：改坏 → 跑 → 确认变红 → 改回）。
#
#  M1  去掉 `check_same_thread=False`
#        → test_store_is_usable_from_another_thread 红（ProgrammingError，带出线程号）
#  M2  `_ensure_schema()` 换回 `_init_schema()`（每个连接都建一次 schema）
#        → test_concurrent_stores_do_not_fight_over_schema 红（连跑 3 次都红）
