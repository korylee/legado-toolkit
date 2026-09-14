# -*- coding: utf-8 -*-
"""校验缓存指纹和有效期的测试。"""

from __future__ import annotations

from datetime import datetime
import sys
import types
import unittest
from argparse import Namespace

# 本测试只覆盖纯缓存策略，不依赖网络客户端；隔离未安装的可选运行时依赖。
sys.modules.setdefault("aiohttp", types.ModuleType("aiohttp"))

from checker import classify_transport_error, is_cache_item_valid, should_cache_result
from loader import fingerprint
from models import Health, build_record
from main import _resolve_check_cache, build_parser


NOW = datetime(2026, 8, 21, 12, 0, 0)


def make_source(search_url: str = "https://example.com/search?q={{key}}") -> dict:
    return {
        "bookSourceName": "示例书源",
        "bookSourceUrl": "https://example.com",
        "searchUrl": search_url,
        "ruleSearch": {"bookList": ".book"},
    }


def make_cache_item(source: dict, health: str, checked_at: str) -> dict:
    return {
        "v": 5,
        "url": source["bookSourceUrl"],
        "fingerprint": fingerprint(source),
        "health": health,
        "checked_at": checked_at,
    }


class CacheValidityTests(unittest.TestCase):
    def test_transport_timeout_is_not_classified_as_dead(self) -> None:
        self.assertEqual(classify_transport_error("timeout"), Health.TIMEOUT)
        self.assertEqual(classify_transport_error("proxy"), Health.TIMEOUT)
        self.assertEqual(classify_transport_error("dns"), Health.TIMEOUT)
        self.assertEqual(classify_transport_error("other"), Health.ERROR)

    def test_transient_network_result_is_not_saved_to_cache(self) -> None:
        self.assertFalse(should_cache_result(Health.TIMEOUT))
        self.assertFalse(should_cache_result(Health.ERROR))
        self.assertTrue(should_cache_result(Health.OK))

    def test_cache_with_changed_rule_fingerprint_is_not_reused(self) -> None:
        source_a = make_source()
        source_b = make_source("https://example.com/find?q={{key}}")
        item = make_cache_item(source_a, Health.OK, "2026-08-21 10:00:00")

        self.assertFalse(is_cache_item_valid(build_record(source_b, 0), item, now=NOW))

    def test_ok_cache_older_than_fourteen_days_is_not_reused(self) -> None:
        source = make_source()
        item = make_cache_item(source, Health.OK, "2026-08-07 11:59:59")

        self.assertFalse(is_cache_item_valid(build_record(source, 0), item, now=NOW))

    def test_auth_cache_within_seven_days_is_reused(self) -> None:
        source = make_source()
        item = make_cache_item(source, Health.AUTH, "2026-08-15 12:00:00")

        self.assertTrue(is_cache_item_valid(build_record(source, 0), item, now=NOW))


class CacheCliTests(unittest.TestCase):
    def test_no_cache_disables_read_and_write(self) -> None:
        cache_dir, refresh = _resolve_check_cache(
            Namespace(cache_dir="custom", no_cache=True, refresh_cache=True)
        )
        self.assertEqual(cache_dir, "")
        self.assertFalse(refresh)

    def test_refresh_cache_skips_old_cache_but_keeps_directory(self) -> None:
        cache_dir, refresh = _resolve_check_cache(
            Namespace(cache_dir="custom", no_cache=False, refresh_cache=True)
        )
        self.assertEqual(cache_dir, "custom")
        self.assertTrue(refresh)

    def test_check_and_run_expose_cache_switches(self) -> None:
        parser = build_parser()
        check = parser.parse_args(["check", "--no-cache"])
        run = parser.parse_args(["run", "--refresh-cache"])
        self.assertTrue(check.no_cache)
        self.assertFalse(check.refresh_cache)
        self.assertFalse(run.no_cache)
        self.assertTrue(run.refresh_cache)
