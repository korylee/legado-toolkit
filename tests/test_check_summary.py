# -*- coding: utf-8 -*-
"""两条校验路的结果形状必须**一模一样**（十-2：单条校验切引擎）。

前端读 items / transitions 的是**同一段代码**（`parseCheckResult` /
`applyCheckResults`）——一条源点「校验」走的是本地引擎还是本机引擎，界面上不该
看得出来。两处各写一份形状就会漂，而漂的表现是「某几条源校验完列表不更新」：
不报错、只是那一格永远不变（lessons §五、§二十三）。
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
    "url": "https://a.com", "health": Health.OK, "quality_stars": 5,
    "star_basis": "search_hit", "error": "", "toc_complete": True,
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

    def test_checks_row_maps_to_the_frontend_names(self):
        """`quality_stars` → `stars`：前端回填读的是后者（列表列名）。"""
        it = check_items_from_checks({"https://a.com": CHECKS_ROW})[0]
        self.assertEqual(it["stars"], 5)
        self.assertEqual(it["health"], Health.OK)
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
