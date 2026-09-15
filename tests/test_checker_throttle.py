# -*- coding: utf-8 -*-
"""按书源自己声明的 concurrentRate 限速。

为什么要有：源声明这个字段就是在说「这么打我你会被封」。check 一次要连发好几个
请求（域名 → 搜索 ×N → 详情 → 章节），不遵守的话轻则触发反爬被判失效（而失效会
被缓存 7 天），重则整个 IP 被拉黑。

语义照 Legado（``ConcurrentRateLimiter.kt:82`` 的注释「并发控制为 次数/毫秒，
非并发实际为 1/毫秒」）。**这个文件不 stub aiohttp**——要真跑 `_request` 才能验证
限速真的接在请求路径上，而不只是函数自己能用。
"""

from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from core import checker
from core.checker import AsyncChecker
# 解析器住在 quality（checker 与 fetch 共用同一份），测试也直接认它
from core.quality import rate_interval_ms
from core.models import build_record


def make_record(url="https://a.example/", rate=None):
    raw = {"bookSourceUrl": url, "bookSourceName": "A"}
    if rate is not None:
        raw["concurrentRate"] = rate
    return build_record(raw, 0)


class RateIntervalTests(unittest.TestCase):
    """concurrentRate → 最小请求间隔（毫秒）。"""

    def test_unset_or_zero_means_no_limit(self):
        for value in (None, "", "0", 0, "   ", 0.0):
            with self.subTest(value=value):
                self.assertEqual(rate_interval_ms(value), 0)

    def test_plain_number_is_milliseconds(self):
        # 纯数字 = 1 次 / n 毫秒（不是「n 次」！）
        self.assertEqual(rate_interval_ms("1000"), 1000)
        self.assertEqual(rate_interval_ms(1500), 1500)

    def test_slash_form_is_count_per_interval(self):
        # "n/m"：n 次 / m 毫秒
        self.assertEqual(rate_interval_ms("1/2000"), 2000)
        self.assertEqual(rate_interval_ms("2/2000"), 1000)
        self.assertEqual(rate_interval_ms("3/1000"), 333)

    def test_slash_form_never_returns_zero_for_valid_input(self):
        # 间隔算成亚毫秒时取 1，不能归零——归零等于「不限速」，
        # 而用户明明声明了限速
        self.assertEqual(rate_interval_ms("1000/1"), 1)

    def test_malformed_values_mean_no_limit(self):
        # 解析不了就不限速：方向是保守的，宁可不加间隔也不瞎猜一个值
        for value in ("abc", "1/0", "0/5", "-1", "1/0.5", [], {}):
            with self.subTest(value=value):
                self.assertEqual(rate_interval_ms(value), 0)

    def test_slash_form_survives_a_string_that_int_would_eat(self):
        # 这条守的是历史 bug：sanitize 曾把这个字段强转 int，"1/2" 被打回 0，
        # 于是限速信息在入库时就没了。限速解析不能再犯同一个错
        self.assertEqual(rate_interval_ms("1/2"), 2)


class ThrottleTests(unittest.TestCase):
    """_throttle 的行为。用假的 sleep 记录等待时长，避免真的等。"""

    def _run(self, coros):
        """跑一串 _throttle 协程，返回每次 asyncio.sleep 请求的秒数。"""
        slept = []

        async def fake_sleep(seconds):
            slept.append(seconds)

        with mock.patch.object(checker.asyncio, "sleep", side_effect=fake_sleep):
            for coro in coros:
                asyncio.run(coro)
        return slept

    def test_no_declared_rate_never_waits(self):
        ck = AsyncChecker(concurrency=1)
        rec = make_record(rate=None)
        self.assertEqual(self._run([ck._throttle(rec), ck._throttle(rec)]), [])

    def test_first_request_is_not_delayed(self):
        ck = AsyncChecker(concurrency=1)
        self.assertEqual(self._run([ck._throttle(make_record(rate="1/5000"))]), [])

    def test_second_request_waits_the_declared_interval(self):
        ck = AsyncChecker(concurrency=1)
        rec = make_record(rate="1/200")
        slept = self._run([ck._throttle(rec), ck._throttle(rec)])
        self.assertEqual(len(slept), 1)
        self.assertGreater(slept[0], 0)
        self.assertLessEqual(slept[0], 0.2)     # 不会超过声明的间隔

    def test_rate_is_tracked_per_source(self):
        # 限速是「每个源自己的」：A 的节奏不该拖慢 B
        ck = AsyncChecker(concurrency=1)
        a = make_record(url="https://a.example/", rate="1/5000")
        b = make_record(url="https://b.example/", rate="1/5000")
        self.assertEqual(self._run([ck._throttle(a), ck._throttle(b)]), [])


class RequestWiringTests(unittest.TestCase):
    """限速必须真的接在请求路径上——只测 _throttle 本身是不够的。

    **本类只断言「_throttle 被调用了」**，不关心请求成不成功。原因是
    `test_checker_judge.py` 会把 aiohttp 换成空壳模块（`sys.modules` 全局生效），
    全量 discover 跑到这里时 `_request` 里 `except aiohttp.XxxError` 的求值会抛
    AttributeError——那是环境造成的，不是被测行为。限速发生在发请求**之前**，
    所以不管后面怎么炸，只要接上了就一定看得见。
    """

    def test_request_calls_throttle_with_the_record(self):
        ck = AsyncChecker(concurrency=1)
        rec = make_record()
        seen = []

        async def spy(self, record):    # 打在类方法上，所以要收 self
            seen.append(record)

        with mock.patch.object(AsyncChecker, "_throttle", spy):
            try:
                asyncio.run(ck._request(object(), rec, "https://a.example/"))
            except Exception:
                pass
        self.assertEqual(seen, [rec])


# ---------------------------------------------------------------- 变异记录
# 以下为实测（改坏 → `python -B -m unittest tests.test_checker_throttle` → 确认变红 → 还原）。
#
#  M1  **core/quality.py**：rate_interval_ms 去掉 "/" 分支（"1/2000" 走 int() 那条）
#        → test_slash_form_is_count_per_interval
#          test_slash_form_never_returns_zero_for_valid_input
#          test_slash_form_survives_a_string_that_int_would_eat
#          test_second_request_waits_the_declared_interval 红
#  M2  **core/quality.py**：`max(1, ...)` 改成裸 round
#        → test_slash_form_never_returns_zero_for_valid_input 红
#
#  解析器后来搬到了 quality（checker 与 fetch 共用），上面两条已按新位置重跑过。
#  M3  _throttle 里去掉 `await asyncio.sleep(...)`
#        → test_second_request_waits_the_declared_interval 红
#  M4  _request 里去掉 `await self._throttle(record)`
#        → RequestWiringTests.test_request_calls_throttle_with_the_record 红
#        （M3/M4 都用**全量 discover** 复跑过：本文件的用例必须同时在
#          「aiohttp 被打桩」的环境下成立，理由见 RequestWiringTests 的注释）
#  M5  _throttle 用常量 key（不是 record.url）
#        → test_rate_is_tracked_per_source 红
