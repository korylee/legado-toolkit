# -*- coding: utf-8 -*-
"""clean_source 对 concurrentRate 的处理。

这个字段曾经被当成 int 处理，于是 `"1/2"` 走 int(float(...)) 抛异常、被兜底成 **0**
——0 在 App 里等于「不限速」。也就是说我们不但不遵守用户的限速设置，还在导入/保存时
把它**静默抹掉**，再导出回 App；而这类破坏会连证据一起抹掉（库里存量的 `"1/2"` 已
无法反推有多少）。

一手语义：Legado 的字段是 String（`BaseSource.kt:35`），两种写法都合法——
`"次数/毫秒"`（如 `"1/2000"`）或纯毫秒数（`"1000"`），见
`ConcurrentRateLimiter.kt:82` 的注释「并发控制为 次数/毫秒，非并发实际为 1/毫秒」。
"""

from __future__ import annotations

import unittest

from core.sanitize import clean_source


def clean(value):
    source = {"concurrentRate": value}
    clean_source(source)
    return source["concurrentRate"]


class ConcurrentRateTests(unittest.TestCase):
    def test_slash_form_is_preserved(self):
        # 「次数/毫秒」写法必须原样留住——这是本次修的那个静默破坏
        for value in ("1/2", "1/2000", "3/500", "1/0"):
            with self.subTest(value=value):
                self.assertEqual(clean(value), value)

    def test_plain_milliseconds_are_kept_intact(self):
        self.assertEqual(clean("1000"), 1000)
        self.assertEqual(clean(1000), 1000)
        self.assertEqual(clean("0"), 0)
        self.assertEqual(clean(0), 0)

    def test_float_is_truncated_to_int(self):
        self.assertEqual(clean(1000.5), 1000)

    def test_none_stays_none(self):
        # None 表示「没设过这个字段」，不要凭空造一个 0 出来
        self.assertIsNone(clean(None))

    def test_bool_is_not_mistaken_for_int(self):
        # bool 是 int 的子类：只写 isinstance(v, int) 会让 true 原样漏过去
        self.assertEqual(clean(True), 0)
        self.assertEqual(clean(False), 0)

    def test_container_becomes_zero(self):
        # list/dict 在任何一边都解析不了，清成 0 是对的
        self.assertEqual(clean([]), 0)
        self.assertEqual(clean({}), 0)

    def test_dirty_book_source_type_is_normalized(self):
        """`bookSourceType` 只允许 Legado 的 0/1/2/3。

        实测库里有过 6 条 `4`（`BookSourceType.kt` 的 `@IntDef` 里没有 4），
        而 `clean_source` 原来**根本不看这个字段**——于是导入外部源时，对方带个
        4（或 99）会被原样收下、再原样导出回 App。UI 保存时本来就会归 0，
        这里补上是为了**导入那条路**（`backend/api/imports.py` 也调 clean_source）。
        """
        for dirty in (4, 99, -1, "4", None, "", [], {}):
            with self.subTest(value=dirty):
                src = {"bookSourceType": dirty}
                clean_source(src)
                self.assertIn(src["bookSourceType"], (0, 1, 2, 3),
                              "脏值必须归一：%r" % (dirty,))

    def test_legal_types_are_untouched(self):
        """反向断言：0/1/2/3 一个都不能动——归一化写成恒 0 就会全绿。"""
        for ok in (0, 1, 2, 3):
            with self.subTest(value=ok):
                src = {"bookSourceType": ok}
                clean_source(src)
                self.assertEqual(src["bookSourceType"], ok)

    def test_sibling_int_fields_untouched(self):
        # 改 concurrentRate 不能顺带改坏同一批处理的其它 int 字段
        source = {"customOrder": "5", "respondTime": "abc", "weight": 3,
                  "concurrentRate": "1/2"}
        clean_source(source)
        self.assertEqual(source["customOrder"], 5)
        self.assertEqual(source["respondTime"], 0)   # 非数字仍按原逻辑清零
        self.assertEqual(source["weight"], 3)
        self.assertEqual(source["concurrentRate"], "1/2")


# ---------------------------------------------------------------- 变异记录
# 以下为实测。
#
#  M1  把 concurrentRate 放回 int 字段清单（还原成修复前的写法）
#        → ConcurrentRateTests.test_slash_form_is_preserved 红（"1/2" 变 0）
#  M2  去掉 `isinstance(v, bool)` 那个先行分支
#        → ConcurrentRateTests.test_bool_is_not_mistaken_for_int 红（True 原样漏过）
