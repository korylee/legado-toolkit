# -*- coding: utf-8 -*-
"""两条校验路的结果形状必须**一模一样**（十-2：单条校验切引擎）。
"""

from __future__ import annotations

import unittest

from backend.api.check_summary import (CHANGED_ITEMS_LIMIT, ITEM_KEYS,
                                       check_items_from_checks,
                                       check_items_from_records,
                                       summarize_transitions)
from core.loader import _normalize_url
from core.models import Health, build_record

#: 一条真实的 checks 行（从库里取的最小切片：`Store.checks_map()` 的取值形状）
CHECKS_ROW = {
    "url": "https://a.com", "health": Health.OK, "error": "", "toc_complete": True,
    "content_ok": True, "search_hit": "绍宋", "checked_at": "2026-09-21 10:00:00",
    "engine": "jvm", "probe_depth": 3,
}


class ShapeParityTests(unittest.TestCase):

    def test_both_builders_emit_exactly_the_same_keys(self):
        """**同形状**不是靠注释保证的：逐键比对。"""
        rec = build_record({"bookSourceUrl": "https://A.com/",
                            "bookSourceName": "example"}, 0)
        rec.health = Health.OK
        from_record = check_items_from_records([rec])[0]
        from_checks = check_items_from_checks({"https://a.com": CHECKS_ROW})[0]
        self.assertEqual(sorted(from_record), sorted(from_checks))
        self.assertEqual(sorted(from_record), sorted(ITEM_KEYS))

    def test_checks_row_maps_validation_facts(self):
        """checks 行直接映射为健康、搜索、目录和正文事实。"""
        it = check_items_from_checks({"https://a.com": CHECKS_ROW})[0]
        self.assertEqual(it["health"], Health.OK)
        self.assertTrue(it["toc_complete"])
        self.assertTrue(it["content_ok"])
        self.assertEqual(it["search_hit"], "绍宋")

    def test_url_is_normalized_and_name_comes_from_the_payload(self):
        """checks 行里不存源名（「变成 X」的明细要显示它），由调用方按 url 传进来。"""
        it = check_items_from_checks(
            {"https://a.com": CHECKS_ROW}, names={"https://a.com": "某源"})[0]
        self.assertEqual(it["url"], "https://a.com")
        self.assertEqual(it["name"], "某源")

    def test_items_are_normalized_on_both_paths(self):
        rec = build_record({"bookSourceUrl": "https://A.com/",
                            "bookSourceName": "example"}, 0)
        rec.health = Health.OK
        self.assertEqual(check_items_from_records([rec])[0]["url"], "https://a.com")
        self.assertEqual(
            check_items_from_checks({"x": dict(CHECKS_ROW, url="HTTPS://A.com/")})[0]["url"],
            "https://a.com")


class TransitionsOnItemsTests(unittest.TestCase):
    """`summarize_transitions` 现在收**形状 dict**（两条路共用一份实现）。"""

    def test_jvm_flavored_change_is_counted(self):
        prev = {_normalize_url("https://a.com"): {"health": Health.OK}}
        items = check_items_from_checks({"https://a.com": dict(CHECKS_ROW,
                                                              health=Health.DEAD)})
        out = summarize_transitions(prev, items)
        self.assertEqual(out["changed"], {Health.DEAD: 1})
        self.assertEqual(out["first_checked"], 0)
        self.assertEqual(out["changed_items"][0]["name"], "https://a.com")  # 没传名字就用 url

    def test_first_checked_is_not_a_change(self):
        out = summarize_transitions({}, check_items_from_checks({"https://a.com": CHECKS_ROW}))
        self.assertEqual(out["first_checked"], 1)
        self.assertEqual(out["changed"], {})

    def test_detail_list_is_capped_but_the_count_is_not(self):
        prev = {_normalize_url("https://a.com"): {"health": Health.OK}}
        items = [{"url": "https://a.com", "name": "", "health": Health.DEAD}] * (CHANGED_ITEMS_LIMIT + 5)
        out = summarize_transitions(prev, items)
        self.assertEqual(out["changed"][Health.DEAD], CHANGED_ITEMS_LIMIT + 5)
        self.assertEqual(len(out["changed_items"]), CHANGED_ITEMS_LIMIT)


if __name__ == "__main__":
    unittest.main()
