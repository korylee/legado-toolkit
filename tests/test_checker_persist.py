# -*- coding: utf-8 -*-
"""校验结果的落库路径（Web 那条链路）。

``backend/api/ops.py`` 构造 AsyncChecker 时传的是 ``use_store=True``、
**不传 cache_dir**。改之前这两个值合起来等于「校验算完从不落库」：

  - ``run()`` 里保存那一段被 ``if self.cache_dir:`` 挡住——cache_dir 是 None，
    整段跳过
  - ``save_cache_append`` 又是先 ``os.makedirs(self.cache_dir)`` 再判 use_store
    ——cache_dir 为 None 时第一步就抛 TypeError

表现是：界面点校验、请求成功、状态不变。**两半各守一个用例**，只修一半仍会坏。

本文件不 stub aiohttp（`test_checker_cache.py` 会 stub，那里跑不了 run()）。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import os
import tempfile
import unittest

from core.checker import AsyncChecker
from core.models import build_record
from core.store import Store

_TMP_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".tmp")
os.makedirs(_TMP_ROOT, exist_ok=True)


def make_source(url: str = "https://a.example/") -> dict:
    return {"bookSourceUrl": url, "bookSourceName": "A",
            "searchUrl": "https://a.example/s?q={{key}}",
            "ruleSearch": {"bookList": "class.item"}}


def _just_checked() -> str:
    """一个「刚校验过」的时刻。

    **别写死日期时间**：``is_cache_item_valid`` 会拒绝**未来**的校验时间
    （防时钟漂移），而写死的值在跑测试时可能还没到——这里就踩过：硬编码
    10:00:00，机器本地时间是 00:57，缓存被判无效，表现成「缓存不复用」。
    """
    return (datetime.now() - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")


def checked(url: str = "https://a.example/", health: str = "ok"):
    rec = build_record(make_source(url), 0)
    rec.health = health
    rec.quality_stars = 4
    rec.checked_at = _just_checked()
    return rec


class StoreBackendSaveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=_TMP_ROOT)
        self._old = os.environ.get("LEGADO_DATA_DIR")
        os.environ["LEGADO_DATA_DIR"] = self.tmp.name

    def tearDown(self):
        if self._old is None:
            os.environ.pop("LEGADO_DATA_DIR", None)
        else:
            os.environ["LEGADO_DATA_DIR"] = self._old
        self.tmp.cleanup()

    def _rows(self):
        st = Store()
        try:
            return [dict(r) for r in st.conn.execute(
                "SELECT source_url, health, stars FROM checks ORDER BY id")]
        finally:
            st.close()

    def test_save_writes_to_store_without_cache_dir(self):
        # 前提：Web 那条链路就是 cache_dir=None + use_store=True。
        # 改之前 save_cache_append 走到 os.makedirs(None) 直接抛 TypeError
        ck = AsyncChecker(concurrency=1, use_store=True)
        self.assertIsNone(ck.cache_dir)
        ck.save_cache_append(checked())
        ck.close()
        self.assertEqual(ck.save_failures, 0)
        self.assertEqual([r["health"] for r in self._rows()], ["ok"])

    def test_run_persists_when_only_the_store_backend_exists(self):
        # 守 run() 里那道门：以前是 `if self.cache_dir:`，整段保存被跳过，
        # 于是请求成功、状态不变
        ck = AsyncChecker(concurrency=1, use_store=True)

        async def fake_check_one(session, record):
            record.health = "ok"
            record.quality_stars = 4
            record.checked_at = _just_checked()
            return record

        ck.check_one = fake_check_one
        asyncio.run(ck.run([build_record(make_source(), 0)]))
        self.assertEqual(len(self._rows()), 1)   # 改之前这里是 0

    def test_store_write_failure_is_counted_not_swallowed(self):
        # 写不进去的表现和「源本来就没变」在界面上无法区分，所以必须能报出来。
        # 改之前这里是 `except Exception: pass`，正好把故障盖住
        class BoomStore:
            def save_checks(self, rows):
                raise RuntimeError("db gone")

        ck = AsyncChecker(concurrency=1, use_store=True)
        ck._store = lambda: BoomStore()
        ck.save_cache_append(checked())
        self.assertEqual(ck.save_failures, 1)

    def test_uncacheable_health_is_not_written(self):
        # 瞬时错误不落库（一次断网不该污染后续校验）——这条是与超时/异常相关的
        # 既有约定，别在改保存条件时顺手破坏
        ck = AsyncChecker(concurrency=1, use_store=True)
        ck.save_cache_append(checked(health="timeout"))
        ck.close()
        self.assertEqual(self._rows(), [])


class CacheReuseTests(unittest.TestCase):
    """缓存复用与「复用了几条」的可见性。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=_TMP_ROOT)
        self._old = os.environ.get("LEGADO_DATA_DIR")
        os.environ["LEGADO_DATA_DIR"] = self.tmp.name

    def tearDown(self):
        if self._old is None:
            os.environ.pop("LEGADO_DATA_DIR", None)
        else:
            os.environ["LEGADO_DATA_DIR"] = self._old
        self.tmp.cleanup()

    def _run(self, records, check_one):
        ck = AsyncChecker(concurrency=1, use_store=True)
        ck.check_one = check_one
        asyncio.run(ck.run(records))
        return ck

    def test_second_run_reuses_cache_and_sends_nothing(self):
        # 先落一条
        ck = AsyncChecker(concurrency=1, use_store=True)
        ck.save_cache_append(checked())
        ck.close()

        sent = []

        async def spy(session, record):
            sent.append(record.url)
            return record

        ck2 = self._run([build_record(make_source(), 0)], spy)
        self.assertEqual(ck2.cached_count, 1)    # 复用了几条要能报出来
        self.assertEqual(sent, [])               # 一条请求都没发

    def test_refresh_cache_forces_a_real_request(self):
        ck = AsyncChecker(concurrency=1, use_store=True)
        ck.save_cache_append(checked())
        ck.close()

        sent = []

        async def spy(session, record):
            sent.append(record.url)
            record.health = "ok"
            return record

        ck2 = AsyncChecker(concurrency=1, use_store=True)
        ck2.refresh_cache = True                  # 忽略缓存
        ck2.check_one = spy
        asyncio.run(ck2.run([build_record(make_source(), 0)]))
        self.assertEqual(ck2.cached_count, 0)
        self.assertEqual(sent, ["https://a.example/"])

    def test_changed_rules_invalidate_the_cache(self):
        ck = AsyncChecker(concurrency=1, use_store=True)
        ck.save_cache_append(checked())
        ck.close()

        sent = []

        async def spy(session, record):
            sent.append(record.url)
            return record

        # 同 URL、规则变了 → 指纹不符 → 必须重校
        changed = make_source()
        changed["ruleSearch"] = {"bookList": "class.CHANGED"}
        ck2 = self._run([build_record(changed, 0)], spy)
        self.assertEqual(ck2.cached_count, 0)
        self.assertEqual(sent, ["https://a.example/"])


# ---------------------------------------------------------------- 变异记录
# 以下为实测（改坏 → `python -B -m unittest tests.test_checker_persist` → 确认变红 → 还原）。
#
#  M1  run() 里的保存条件改回 `if self.cache_dir:`
#        → test_run_persists_when_only_the_store_backend_exists 红
#  M2  save_cache_append 把 os.makedirs 挪回 use_store 分支之前
#        → test_save_writes_to_store_without_cache_dir 红（TypeError）
#  M3  store 写失败的 except 改回 `pass`（不计数）
#        → test_store_write_failure_is_counted_not_swallowed 红
#  M4  cached_count 恒定 0（不统计复用）
#        → test_second_run_reuses_cache_and_sends_nothing 红
