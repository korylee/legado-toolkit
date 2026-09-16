# -*- coding: utf-8 -*-
"""校验缓存指纹和有效期的测试。"""

from __future__ import annotations

from datetime import datetime
import unittest
from argparse import Namespace

from core import checker
from core.checker import classify_transport_error, is_cache_item_valid, should_cache_result
from core.loader import fingerprint
from core.models import Health, build_record
from cli.main import _resolve_check_cache, build_parser


NOW = datetime(2026, 8, 21, 12, 0, 0)


def make_source(search_url: str = "https://example.com/search?q={{key}}") -> dict:
    return {
        "bookSourceName": "示例书源",
        "bookSourceUrl": "https://example.com",
        "searchUrl": search_url,
        "ruleSearch": {"bookList": ".book"},
    }


def make_cache_item(source: dict, health: str, checked_at: str) -> dict:
    # 版本号取当前值，而不是写死 5：写死会让版本一升，本文件的用例就集体走
    # 「版本不符」这条捷径——指纹比对和有效期这两条真实断言会被恒定短路。
    return {
        "v": checker.CACHE_VERSION,
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

    # ------------------------------------------------------ 探测深度（本轮新增）

    def test_shallow_cache_is_not_reused_when_depth_raised(self) -> None:
        """把深度从 1 调到 3 之后，浅缓存必须重验。

        只比版本/指纹/有效期的话，浅缓存照样命中、深度验证一条都不跑，
        而界面上显示的是「校验完成」——「看起来跑了其实没跑」。
        """
        source = make_source()
        item = make_cache_item(source, Health.OK, "2026-08-21 10:00:00")
        item["probe_depth"] = 1

        self.assertFalse(is_cache_item_valid(build_record(source, 0), item,
                                             now=NOW, min_depth=3))
        self.assertFalse(is_cache_item_valid(build_record(source, 0), item,
                                             now=NOW, min_depth=2))

    def test_deep_enough_cache_is_reused(self) -> None:
        source = make_source()
        item = make_cache_item(source, Health.OK, "2026-08-21 10:00:00")
        item["probe_depth"] = checker.DEPTH_TOC
        # 深度到目录档本身就意味着那一轮验过搜索——够深的缓存必然带着这个事实；
        # 不写的话它会被「本轮要验搜索而你没验过」作废，与深度无关
        item["search_probed"] = True

        self.assertTrue(is_cache_item_valid(build_record(source, 0), item,
                                            now=NOW, min_depth=checker.DEPTH_TOC))

    def test_shallow_cache_of_failed_source_is_still_reused(self) -> None:
        """非 OK 的源按 fail-fast 根本走不到深度验证，强制重验只是白打请求。

        缓存里 probe_depth=1 不是因为「当初探得浅」，而是因为它在搜索阶段就失败了。
        """
        source = make_source()
        item = make_cache_item(source, Health.AUTH, "2026-08-15 12:00:00")
        item["probe_depth"] = 1

        self.assertTrue(is_cache_item_valid(build_record(source, 0), item,
                                            now=NOW, min_depth=3))

    def test_legacy_item_without_probe_depth_is_reused_at_default_depth(self) -> None:
        """v6 早期写下的缓存项没有 probe_depth 字段，按浅探测看待即可。"""
        source = make_source()
        item = make_cache_item(source, Health.OK, "2026-08-21 10:00:00")

        self.assertTrue(is_cache_item_valid(build_record(source, 0), item, now=NOW))
        self.assertFalse(is_cache_item_valid(build_record(source, 0), item,
                                             now=NOW, min_depth=2))

    # ------------------------------------------------------ 探测能力：搜索（本轮新增）

    def test_search_unprobed_cache_is_not_reused_when_search_required(self) -> None:
        """本次要验搜索，而缓存是「没验搜索」时写下的 → **不可复用**。

        这是 1.2 那个静默失效的核心：先只测域名快速体检一遍，再验到搜索档跑一遍，
        第二遍会直接复用第一遍的缓存——搜索探测根本没跑，而界面显示
        「校验完成：全部命中缓存」。更糟的是第一遍给每个可达源都写了一星
        （calc_stars 要求 has_search 且 search_response_ms > 0 才给 2★），
        而这个错误结论会在 TTL 内一直命中。

        深度给到「搜索」档（= 本次要验搜索），所以作废的是 search_probed 那一轴。
        """
        source = make_source()          # 有 searchUrl → has_search=True
        item = make_cache_item(source, Health.OK, "2026-08-21 10:00:00")
        item["probe_depth"] = checker.DEPTH_SEARCH
        item["search_probed"] = False

        self.assertFalse(is_cache_item_valid(build_record(source, 0), item,
                                             now=NOW, min_depth=checker.DEPTH_SEARCH))

    def test_search_probed_cache_is_reused(self) -> None:
        """成对的另一条：验过搜索的缓存照常复用。

        **必须与上一条同时存在**——单独任何一条都拦不住「方向写反」（把
        「本次要求更高才作废」写成「缓存要求更高才作废」），两条一起才看得出来。
        """
        source = make_source()
        item = make_cache_item(source, Health.OK, "2026-08-21 10:00:00")
        item["probe_depth"] = checker.DEPTH_SEARCH
        item["search_probed"] = True

        self.assertTrue(is_cache_item_valid(build_record(source, 0), item,
                                            now=NOW, min_depth=checker.DEPTH_SEARCH))

    def test_cache_without_search_requirement_still_reused(self) -> None:
        """反向：本次只到「主页」档时，没验过搜索的缓存照常复用。

        作废的方向只能是「本次要求更高」。缓存比本次更「强」（验过搜索、本次只要
        主页档）时也必须照常复用，否则把深度调低会顺带把缓存全部打回重验。
        """
        source = make_source()
        item = make_cache_item(source, Health.OK, "2026-08-21 10:00:00")
        item["probe_depth"] = 1
        item["search_probed"] = False

        self.assertTrue(is_cache_item_valid(build_record(source, 0), item,
                                            now=NOW, min_depth=checker.DEPTH_HOME))

    def test_source_without_search_rule_is_unaffected(self) -> None:
        """没有搜索规则的源，即使深度给到搜索档也不作废。

        它本来就走不到搜索（check_one 的条件是 health == OK and self.wants_search
        and record.has_search）。若把它也算作废，这些源**永远命中不了缓存**，
        每次校验都白打一遍请求。
        """
        source = make_source("")        # 无 searchUrl → has_search=False
        item = make_cache_item(source, Health.OK, "2026-08-21 10:00:00")
        item["probe_depth"] = checker.DEPTH_SEARCH
        item["search_probed"] = False

        self.assertTrue(is_cache_item_valid(build_record(source, 0), item,
                                            now=NOW, min_depth=checker.DEPTH_SEARCH))

    def test_unreachable_source_is_unaffected(self) -> None:
        """health 非 OK 的缓存同理不受「要验搜索」影响（同样走不到搜索）。"""
        source = make_source()
        item = make_cache_item(source, Health.AUTH, "2026-08-15 12:00:00")
        item["probe_depth"] = 1
        item["search_probed"] = False

        self.assertTrue(is_cache_item_valid(build_record(source, 0), item,
                                            now=NOW, min_depth=checker.DEPTH_SEARCH))


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


# ---------------------------------------------------------------- 变异记录
# 以下为实测（改坏 → `python -B -m unittest tests.test_checker_cache` → 确认变红 → 还原）。
#
#  M1  is_cache_item_valid 去掉 probe_depth 判定（直接 `return True`）
#        → test_shallow_cache_is_not_reused_when_depth_raised 红
#  M2  深度判定不设 health == OK 前提（对非 OK 的源也要求深度）
#        → test_shallow_cache_of_failed_source_is_still_reused 红
#  M3  搜索判定去掉 `and record.has_search` 出口（对无搜索规则的源也要求验过搜索）
#        → test_source_without_search_rule_is_unaffected 红
#  M4  搜索判定方向写反（写成「缓存验过搜索、本次不验 → 作废」）
#        → test_search_unprobed_cache_is_not_reused_when_search_required 红
#        （**只红这一条**：test_search_probed_cache_is_reused 在写反时照样绿，
#          这正是两条断言成对存在的理由——单看任何一条都拦不住方向写反）
