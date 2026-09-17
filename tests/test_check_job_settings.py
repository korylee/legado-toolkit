# -*- coding: utf-8 -*-
"""check job 的参数接线测试。

守的是这次改动最主要的回归风险：**设置写对了但 job 没读**。改动前 ops.py 把
concurrency 写死 20、timeout 写死 8.0、probe_depth 写死 1，且**根本没读**
payload 的 verify_ssl——前端给了也不生效。

不 stubbing 网络：AsyncChecker 整个被替身换掉，只用它记录构造参数。
"""

from __future__ import annotations

import asyncio
import os
import unittest
import uuid

from backend.api import ops
from core import settings_store as S


_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
os.makedirs(_ROOT, exist_ok=True)


class FakeChecker:
    """记录构造参数与实例；run() 不发请求、不返回结果。

    实例也要留：``refresh_cache`` 是**构造后赋属性**的（不是构造参数），
    只看 kwargs 根本验证不到它。
    """

    last: dict = {}
    instance = None

    def __init__(self, **kwargs):
        FakeChecker.last = kwargs
        FakeChecker.instance = self
        self.cached_count = 0
        self.save_failures = 0
        self.hit_downgrades = []
        self.refresh_cache = False

    async def run(self, records, on_progress=None):
        # 签名必须与 AsyncChecker.run 一致（含 on_progress）——ops.run_check_job
        # 是按关键字传的，桩漏改会直接 TypeError
        #
        # 原样返回：真实 AsyncChecker 返回的也是这一批（校验后的）记录。
        # 返回空列表的话 rebuild_system_tags 会收到 []，就测不到"传了哪些 url"
        return list(records)

    def close(self):
        pass


class FakeStore:
    #: 最近一次 rebuild_system_tags 收到的 urls（None = 全库重建）
    rebuilt = None

    def export_sources(self):
        return [{"bookSourceUrl": "https://a.com", "bookSourceName": "a",
                 "bookSourceType": 0}]

    def get_source(self, url):
        return None

    def checks_map(self):
        # run_check_job 会在跑之前读一次上一版结论（供变化摘要）。
        # 替身返回空 = 没有历史，本次全部算「首次有结论」
        return {}

    def update_job(self, *args, **kwargs):
        pass

    def rebuild_system_tags(self, urls=None):
        # 签名必须跟真 Store 一致（`urls=None` = 全库重建）。替身少一个参数的话，
        # 调用方传了 urls 会直接 TypeError——那是替身与实现脱节，不是被测代码的问题
        FakeStore.rebuilt = urls


class CheckJobSettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.path = os.path.join(_ROOT, "tmp_settings_" + uuid.uuid4().hex[:8] + ".json")
        os.environ["LEGADO_SETTINGS"] = self.path
        # ops.run_check_job 在**函数体内** import AsyncChecker，所以替换
        # core.checker 上的属性就能命中
        import core.checker
        self._orig = core.checker.AsyncChecker
        core.checker.AsyncChecker = FakeChecker
        FakeChecker.last = {}
        FakeChecker.instance = None
        FakeStore.rebuilt = None

    def tearDown(self) -> None:
        import core.checker
        core.checker.AsyncChecker = self._orig
        os.environ.pop("LEGADO_SETTINGS", None)
        for p in (self.path, self.path + ".tmp"):
            if os.path.exists(p):
                os.remove(p)

    def _run(self, payload=None) -> dict:
        return asyncio.run(ops.run_check_job("j1", FakeStore(), payload or {}))

    # ---------------------------------------------------------------- 全局设置

    def test_job_uses_global_settings_when_payload_has_no_override(self) -> None:
        """改动前这里是写死的 20——设置里改成 7 必须真的生效。"""
        S.update({"check": {"concurrency": 7, "timeout": 20.0, "probe_depth": 2}})
        self._run({})
        self.assertEqual(FakeChecker.last["concurrency"], 7)
        self.assertEqual(FakeChecker.last["timeout"], 20.0)
        self.assertEqual(FakeChecker.last["probe_depth"], 2)

    def test_verify_ssl_is_passed_through(self) -> None:
        """改动前 ops.py 不读这个键，Web 端没有任何途径关掉证书校验。"""
        S.update({"check": {"verify_ssl": False}})
        self._run({})
        self.assertFalse(FakeChecker.last["verify_ssl"])

    def test_empty_proxy_in_settings_means_direct_connection(self) -> None:
        """存储层空串 = 直连；aiohttp 认的是 None，空串会被原样递进去。"""
        self._run({})
        self.assertIsNone(FakeChecker.last["proxy"])

    def test_configured_proxy_reaches_checker(self) -> None:
        S.update({"check": {"proxy": "http://127.0.0.1:7890"}})
        self._run({})
        self.assertEqual(FakeChecker.last["proxy"], "http://127.0.0.1:7890")

    # ---------------------------------------------------------------- 本次覆盖

    def test_payload_override_wins_and_does_not_touch_global(self) -> None:
        S.update({"check": {"concurrency": 7}})
        result = self._run({"check": {"concurrency": 3}})
        self.assertEqual(FakeChecker.last["concurrency"], 3)
        self.assertEqual(S.load()["check"]["concurrency"], 7)
        self.assertEqual(result["params"]["concurrency"], 3)

    def test_false_and_empty_overrides_are_not_treated_as_absent(self) -> None:
        """最脆的一条：False / "" 是有效值，不能被当成「没传」而回落全局。

        全局配了代理时，「本次直连」必须真的直连；全局开着证书校验时，
        `verify_ssl=False` 必须真的关掉。
        """
        S.update({"check": {"proxy": "http://127.0.0.1:7890", "verify_ssl": True}})
        self._run({"check": {"proxy": "", "verify_ssl": False}})
        self.assertIsNone(FakeChecker.last["proxy"])
        self.assertFalse(FakeChecker.last["verify_ssl"])

    def test_partial_override_keeps_global_for_other_keys(self) -> None:
        S.update({"check": {"concurrency": 7, "timeout": 30.0}})
        self._run({"check": {"concurrency": 3}})
        self.assertEqual(FakeChecker.last["concurrency"], 3)
        self.assertEqual(FakeChecker.last["timeout"], 30.0)

    def test_override_is_clamped(self) -> None:
        self._run({"check": {"concurrency": 99999, "probe_depth": 9}})
        self.assertEqual(FakeChecker.last["concurrency"], 200)
        self.assertEqual(FakeChecker.last["probe_depth"], 1)

    # ---------------------------------------------------------------- 不进设置的项

    def test_refresh_cache_is_read_from_payload_only(self) -> None:
        """refresh_cache 是每次动作而非默认值，不进设置、也不是构造参数。"""
        self.assertNotIn("refresh_cache", S.DEFAULTS["check"])
        self._run({"refresh_cache": True})
        self.assertNotIn("refresh_cache", FakeChecker.last)   # 不是构造参数
        self.assertTrue(FakeChecker.instance.refresh_cache)   # 构造后赋值
        self._run({})
        self.assertFalse(FakeChecker.instance.refresh_cache)

    def test_rebuild_targets_only_the_checked_urls(self) -> None:
        """分组重建只针对**这次校验过的**源。

        分组只由该源自身的 (类型, 健康度, 星级) 决定，没被重算的源不可能变——
        而全库重建实测 0.55 秒（3774 条）。只校验一条源时传 None 就是白花 0.55 秒。
        """
        self._run({})
        self.assertIsNotNone(FakeStore.rebuilt, "不该用 urls=None 走全库重建")
        self.assertEqual(FakeStore.rebuilt, ["https://a.com"])

    def test_keyword_stays_out_of_settings(self) -> None:
        """keyword 是「测哪个书名」，不是随环境变的参数，本轮不进设置。"""
        self.assertNotIn("keyword", S.DEFAULTS["check"])
        self._run({})
        self.assertEqual(FakeChecker.last["keyword"], "我")
        self._run({"keyword": "剑来"})
        self.assertEqual(FakeChecker.last["keyword"], "剑来")


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------- 变异记录
# 以下为实测（改坏 → `python -B -m unittest tests.test_check_job_settings` → 确认变红 → 还原）。
#
#  M1  ops.py 改回 concurrency=int(payload.get("concurrency", 20) or 20)
#        → test_job_uses_global_settings_when_payload_has_no_override 红
#  M2  去掉 verify_ssl=cfg["verify_ssl"]（写死 True）
#        → test_verify_ssl_is_passed_through 红
