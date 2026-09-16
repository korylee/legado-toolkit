# -*- coding: utf-8 -*-
"""校验缓存指纹和有效期的测试。"""

from __future__ import annotations

from datetime import datetime
import unittest
from argparse import Namespace

from core import checker
from core.checker import classify_transport_error, is_cache_item_valid, is_transient
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

    def test_transient_network_result_is_never_reused(self) -> None:
        """瞬时错误**照写但绝不复用**。

        原来这条断言的是「不写」（`should_cache_result`）。改成"照写、不复用"之后：
        要防的事没变（一次断网/抖动不能变成源的结论），但那些源不再永久显示
        「未校验」——实测 1222/3861 条卡在这个状态上。

        **两个方向都要断言**：只断言"不复用"的话，把写库那道门加回来照样绿，
        而界面上那些源又会全部变回「未校验」。
        """
        self.assertTrue(is_transient(Health.TIMEOUT))
        self.assertTrue(is_transient(Health.ERROR))
        self.assertFalse(is_transient(Health.OK))
        # 证书问题不是瞬时的（站点证书不会自己变好），所以照常缓存、照常复用
        self.assertFalse(is_transient(Health.CERT))
        source = make_source()
        item = make_cache_item(source, Health.TIMEOUT, "2026-08-21 11:00:00")
        self.assertFalse(is_cache_item_valid(build_record(source, 0), item, now=NOW),
                         "瞬时错误的缓存一定不能复用——它就在 TTL 内也一样")
        item = make_cache_item(source, Health.ERROR, "2026-08-21 11:00:00")
        self.assertFalse(is_cache_item_valid(build_record(source, 0), item, now=NOW))

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


class TransportErrorRoutingTests(unittest.TestCase):
    """底层异常 → 失败原因那一桶。**由 except 子句的顺序决定**，写错了不报错，
    只会把一整类失败归错档（用户看到的是一句没有信息量的中文）。

    证书错误是最容易错的一个：它是「站点可达、只是证书不被信任」，唯一可操作的
    一类（关掉证书校验就能用）。掉进 "other" 就变成了「⚠️异常」。
    """

    class _RaisingSession:
        """`session.request(...)` 一进上下文就抛——正好落在 `_request` 的异常分支里。"""

        def __init__(self, exc):
            self.exc = exc

        def request(self, *_a, **_kw):
            exc = self.exc

            class _CM:
                async def __aenter__(self):
                    raise exc

                async def __aexit__(self, *_a):
                    return False
            return _CM()

    def _call(self, exc):
        from core.checker import AsyncChecker
        ck = AsyncChecker(concurrency=1, use_store=False)
        rec = build_record(make_source(), 0)
        try:
            import asyncio
            return asyncio.run(ck._request(self._RaisingSession(exc), rec,
                                           "https://example.com/"))
        finally:
            ck.close()

    def test_certificate_error_is_its_own_bucket(self):
        import aiohttp
        import ssl
        exc = aiohttp.ClientConnectorCertificateError(
            None, ssl.SSLCertVerificationError("self signed"))
        _status, _body, _cost, err, detail = self._call(exc)
        self.assertEqual(err, "cert")
        self.assertEqual(detail, "ClientConnectorCertificateError")

    def test_tls_handshake_failure_stays_gfw(self):
        """TLS 握手失败（SNI 阻断）与证书问题**不是一回事**，别一起归到 cert。"""
        import aiohttp
        import ssl
        exc = aiohttp.ClientConnectorSSLError(None, ssl.SSLError("handshake"))
        _status, _body, _cost, err, _detail = self._call(exc)
        self.assertEqual(err, "tls")

    def test_other_errors_carry_the_underlying_type_name(self):
        """「网络异常」那一桶实测混着好几种成因，光看中文无从排查。"""
        import aiohttp
        exc = aiohttp.ClientOSError(10054, "连接被重置")
        _status, _body, _cost, err, detail = self._call(exc)
        self.assertEqual(err, "other")
        self.assertEqual(detail, "ClientOSError")

    def test_reset_error_keeps_its_own_bucket(self):
        import aiohttp
        exc = aiohttp.ServerDisconnectedError()
        _status, _body, _cost, err, _detail = self._call(exc)
        self.assertEqual(err, "reset")


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
