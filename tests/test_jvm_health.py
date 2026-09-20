# -*- coding: utf-8 -*-
"""本机引擎结论 → checks 口径（core/jvm_health）：失败分因、六档、星级、谁判的。

用例里的异常串**全是从真库抄的**（`meta` 表 `jvm_check:` 的 `reason` / `root`）。
手编的串测不出「顺序判错」这一类错——`Socket closed` 与 `Connection reset` 只差
一个词，而一个该落「待验证」、一个该落「需翻墙」；`NoDefinitionFoundException`
的栈里还带着 `Socket…` 字样。

守的是三条边界（为什么这么定写在 `core/jvm_health` 与 TODO §一点九）：

1. 失败分因，但**不把「这次没验成」判成「源坏了」**（传输层一律保守）
2. **DNS 要交叉验证**：两个公共 DNS 都说不存在才判死（probe 注入，测试不联网）
3. **引擎自身没跑成**（Koin / OOM）单列：档位只能是「待验证」，且要说清不是源的问题
"""
from __future__ import annotations

import json
import os
import shutil
import unittest
import uuid

from core import dns_check, jvm_health as H
from core.models import Engine, Health
from core.store import Store

_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
os.makedirs(_ROOT, exist_ok=True)

#: 真库抄来的异常串（`SELECT value FROM meta WHERE key LIKE 'jvm_check:%'`）
REAL_REASONS = [
    ("SocketException: Connection reset", H.CAUSE_RESET),
    ("StreamResetException: stream was reset: PROTOCOL_ERROR", H.CAUSE_RESET),
    ("SocketException: Socket closed", H.CAUSE_OTHER),
    ("SocketTimeoutException: Connect timed out", H.CAUSE_TIMEOUT),
    ("InterruptedIOException: timeout", H.CAUSE_TIMEOUT),
    ("TimeoutCancellationException: Timed out waiting for 25000 ms", H.CAUSE_TIMEOUT),
    ("SSLException: Unable to parse TLS packet header", H.CAUSE_TLS),
    ("SSLHandshakeException: Read error: ssl=00000223B9686408: Failure in SSL library",
     H.CAUSE_TLS),
    ("NoStackTraceException: 搜索url不能为空", H.CAUSE_RULE),
    ('ScriptException: org.mozilla.javascript.EcmaError: TypeError: Cannot read property "1" from null',
     H.CAUSE_RULE),
    ("PathNotFoundException: Expected to find an object with property ['data']", H.CAUSE_RULE),
    ("IllegalArgumentException: json string can not be null or empty", H.CAUSE_RULE),
    ("OutOfMemoryError: Java heap space", H.CAUSE_SELF),
    ("NoDefinitionFoundException: No definition found for type "
     "'io.legado.app.domain.gateway.OtherSettingsGateway' on scope '['_root_']'", H.CAUSE_SELF),
    # 兜底：连接被拒**不是**「确认不可达」的证据（本地口径也是保守的——一次失败
    # 可能只是本机网络抖），所以它落 other → 待验证，不是已失效
    ("ConnectException: Connection refused: getsockopt", H.CAUSE_OTHER),
]


class CauseTests(unittest.TestCase):
    def test_real_reason_strings(self) -> None:
        for reason, want in REAL_REASONS:
            with self.subTest(reason=reason[:40]):
                self.assertEqual(H.classify_cause({"reason": reason}), want)

    def test_root_and_stack_are_searched_too(self) -> None:
        """`reason` 只留了外层，根因与栈在另外两个字段里。"""
        row = {"reason": "ScriptException: WrappedException: Wrapped java.lang.Exception",
               "root": "NoDefinitionFoundException: No definition found for type 'x'"}
        self.assertEqual(H.classify_cause(row), H.CAUSE_SELF)

    def test_unknown_text_is_not_guessed(self) -> None:
        self.assertEqual(H.classify_cause({"reason": "奇怪的一句话"}), "")


class HealthTests(unittest.TestCase):
    def _h(self, **row):
        return H.health_for(row)[0]

    def test_states_that_speak_for_themselves(self) -> None:
        self.assertEqual(self._h(state="ok"), Health.OK)
        self.assertEqual(self._h(state="login_wall"), Health.AUTH)
        for state in ("no_result", "timeout", "empty_js_shell", "invalid"):
            self.assertEqual(self._h(state=state), Health.PENDING)

    def test_transport_causes_follow_the_local_table(self) -> None:
        self.assertEqual(self._h(state="error", reason="SocketException: Connection reset"),
                         Health.GFW)
        self.assertEqual(self._h(state="error", reason="SSLException: Unable to parse TLS packet header"),
                         Health.GFW)
        # 证书不被信任**不再单独成档**（2026-09-20 撤）：落「待验证」，原因写进
        # error（App 引擎侧本来就产不出它——要么直接通过，要么报 TLS 阻断）
        cert_row = {"state": "error",
                    "reason": "CertificateException: PKIX path building failed"}
        self.assertEqual(self._h(**cert_row), Health.PENDING)
        self.assertIn("证书", H.health_for(cert_row)[1])
        # 其余传输层失败一律「待验证」——**方向必须是保守的**
        for reason in ("SocketTimeoutException: Connect timed out",
                       "ConnectException: Connection refused: getsockopt",
                       "SocketException: Socket closed"):
            self.assertEqual(self._h(state="error", reason=reason), Health.PENDING, reason)

    def test_engine_self_failure_is_not_the_source_s_fault(self) -> None:
        row = {"state": "error",
               "reason": "NoDefinitionFoundException: No definition found for type 'x'"}
        health, note = H.health_for(row)
        self.assertEqual(health, Health.PENDING)
        self.assertIn("不是源的问题", note)
        self.assertIn("本机引擎", note)

    def test_rule_error_says_the_next_step_is_fixing(self) -> None:
        health, note = H.health_for({"state": "error", "reason": "NoStackTraceException: 搜索url不能为空"})
        self.assertEqual(health, Health.PENDING)
        self.assertIn("修", note)

    def test_dns_needs_two_agreeing_resolvers(self) -> None:
        row = {"state": "error", "reason": "UnknownHostException: Unable to resolve host"}
        gone = lambda h: (dns_check.GONE, "两个公共 DNS 均应答「域名不存在」")
        polluted = lambda h: (dns_check.POLLUTED, "Cloudflare 能解析到 1.2.3.4")
        unknown = lambda h: (dns_check.UNKNOWN, "公共 DNS 无法核实")
        self.assertEqual(H.health_for(row, host="a.com", probe=gone)[0], Health.DEAD)
        self.assertEqual(H.health_for(row, host="a.com", probe=polluted)[0], Health.GFW)
        self.assertEqual(H.health_for(row, host="a.com", probe=unknown)[0], Health.PENDING)
        # 没有域名可交叉验证 → 只能维持待复检
        self.assertEqual(H.health_for(row)[0], Health.PENDING)

    def test_probe_is_cached_per_host_in_a_batch(self) -> None:
        calls = []

        def probe(host):
            calls.append(host)
            return (dns_check.GONE, "两个公共 DNS 均应答「域名不存在」")

        rows = [{"url": "https://a.com/1", "state": "error",
                 "reason": "UnknownHostException: x"},
                {"url": "https://a.com/2", "state": "error",
                 "reason": "UnknownHostException: x"}]
        got = H.checks_rows(rows, batch="b", probe=probe)
        self.assertEqual(calls, ["a.com"], "同一主机只交叉验证一次")
        self.assertTrue(all(it["health"] == Health.DEAD for it in got))


class ChecksRowTests(unittest.TestCase):
    def test_ok_row_fields(self) -> None:
        row = {"url": "https://a.com", "state": "ok", "stage": "search",
               "hit": 10, "sample": ["斗破苍穹", "另一本"], "cost_ms": 4174}
        it = H.checks_row(row, batch="b", checked_at="2026-09-20 19:39:23")
        self.assertEqual(it["url"], "https://a.com")
        self.assertEqual(it["health"], Health.OK)
        self.assertEqual(it["engine"], Engine.JVM, "结论要记是谁判的")
        self.assertEqual(it["probe_depth"], 2, "搜索档 = 本地那根深度轴的 2")
        self.assertEqual(it["search_hit"], "斗破苍穹")
        self.assertTrue(it["search_probed"])
        self.assertGreaterEqual(it["quality_stars"], 1)

    def test_deep_stage_maps_to_the_depth_axis(self) -> None:
        toc = H.checks_row({"url": "https://a.com", "state": "ok", "stage": "toc",
                            "hit": 1, "sample": ["x"], "cost_ms": 900, "toc_count": 856,
                            "toc_complete": True}, batch="b")
        self.assertEqual(toc["probe_depth"], 3)
        self.assertEqual(toc["chapter_count"], 856)
        con = H.checks_row({"url": "https://a.com", "state": "ok", "stage": "content",
                            "hit": 1, "sample": ["x"], "cost_ms": 900, "toc_complete": True,
                            "content_ok": True}, batch="b")
        self.assertEqual(con["probe_depth"], 4)
        self.assertEqual(con["content_ok"], True)
        # 搜索档的行不该声称目录/正文有结论（**没跑** ≠ **跑了没过**）
        search = H.checks_row({"url": "https://a.com", "state": "ok", "stage": "search",
                               "hit": 1, "sample": ["x"], "cost_ms": 900}, batch="b")
        self.assertIsNone(search["toc_complete"])
        self.assertIsNone(search["content_ok"])

    def test_row_without_url_is_skipped(self) -> None:
        self.assertIsNone(H.checks_row({"state": "ok"}, batch="b"))

    def test_rule_tags_come_from_the_static_rules(self) -> None:
        raw = {"bookSourceUrl": "https://a.com", "ruleToc": {"chapterList": ".x"},
               "ruleContent": {"content": ".c"}}
        it = H.checks_row({"url": "https://a.com", "state": "ok", "stage": "search",
                           "hit": 1, "sample": ["x"], "cost_ms": 1}, batch="b", raw=raw)
        self.assertIn("规则完整", it["quality_tags"])


class StoreChecksTests(unittest.TestCase):
    """**让真函数跑起来**：落库那一步不打死结（lessons §六十九 的判据）。"""

    def setUp(self) -> None:
        self.root = os.path.join(_ROOT, "tmp_jvm_health_" + uuid.uuid4().hex[:8])
        os.makedirs(self.root)
        self.db = os.path.join(self.root, "sources.sqlite3")

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_store_checks_writes_health_stars_and_engine(self) -> None:
        with Store(self.db) as st:
            st.upsert_sources([{"bookSourceUrl": "https://a.com", "bookSourceName": "甲",
                                "bookSourceType": 0, "ruleSearch": {"bookList": ".x"}}])
            n = H.store_checks(
                [{"url": "https://a.com", "state": "error",
                  "reason": "SocketException: Connection reset", "stage": "search"}],
                batch="20260920_000000", store=st)
            self.assertEqual(n, 1)
            got = st.checks_map()["https://a.com"]
        self.assertEqual(got["health"], Health.GFW)
        self.assertEqual(got["engine"], Engine.JVM)
        self.assertIn("连接被重置", got["error"])
        self.assertEqual(got["v"], H.CACHE_VERSION)


if __name__ == "__main__":
    unittest.main()
