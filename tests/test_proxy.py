# -*- coding: utf-8 -*-
"""十-3：代理搬到全局（`network.proxy`）并写进**交给 App 的源 header**。

判据是上游的事实：`AnalyzeUrl` 的 init 从 `source.getHeaderMap()` 取 `proxy` 键当这次请求的
OkHttp 代理——所以「配了代理」这件事的落地形态就是**源 header 里多一行**。这里钉四件事：
写对形态、空值一个字不动、规范化（`host:port` → `http://`）、以及**只收 http**（https/socks
会被上游正则漏掉并抛异常，而不是「没生效」）。
"""
from __future__ import annotations

import json
import pathlib
import tempfile
import unittest
from unittest import mock

from core.jvm_debug import apply_proxy_header


class HeaderTests(unittest.TestCase):
    """header 是 **JSON**（App 用 `GSONStrict.fromJsonObject<Map<String,String>>` 解它）——
    行式串在 App 里会被整块跳过，所以「写对形态」本身就是判据。"""

    def test_proxy_lands_in_the_json_header(self):
        got = apply_proxy_header({"header": '{"User-Agent": "x"}'}, "http://127.0.0.1:7890")
        parsed = json.loads(got["header"])
        self.assertEqual(parsed["proxy"], "http://127.0.0.1:7890")
        self.assertEqual(parsed["User-Agent"], "x", "原有 header 不许丢")

    def test_empty_proxy_changes_nothing(self):
        src = {"header": '{"User-Agent": "x"}'}
        self.assertEqual(apply_proxy_header(src, "")["header"], '{"User-Agent": "x"}')
        self.assertEqual(apply_proxy_header(src, "   ")["header"], '{"User-Agent": "x"}')

    def test_the_callers_copy_is_not_touched(self):
        src = {"header": '{"User-Agent": "x"}'}
        apply_proxy_header(src, "http://p:1")
        self.assertEqual(src["header"], '{"User-Agent": "x"}', "别就地改调用方那一份")

    def test_an_old_proxy_value_is_replaced(self):
        got = apply_proxy_header({"header": '{"proxy": "http://old:1"}'}, "http://new:2")
        self.assertEqual(json.loads(got["header"])["proxy"], "http://new:2")

    def test_no_header_at_all_still_gets_one(self):
        self.assertEqual(json.loads(apply_proxy_header({}, "http://p:1")["header"]),
                         {"proxy": "http://p:1"})

    def test_a_legacy_line_header_is_converted_so_it_finally_applies(self):
        """行式历史（库里 32 条）：转成 JSON 等于让它按作者的原意生效——那行 UA 以前白写。"""
        got = apply_proxy_header({"header": "User-Agent: x"}, "http://p:1")
        parsed = json.loads(got["header"])
        self.assertEqual(parsed, {"User-Agent": "x", "proxy": "http://p:1"})

    def test_a_script_header_is_left_alone(self):
        """`@js:` / `<js>` 是**脚本**，合并会改变它的语义——宁可不注入（这一次就没有代理）。"""
        for raw in ("@js:xxx", "<js>xxx</js>"):
            with self.subTest(raw=raw):
                self.assertEqual(apply_proxy_header({"header": raw}, "http://p:1")["header"], raw)


class CoercionTests(unittest.TestCase):
    """输入规范化与拒绝理由（拒绝是静默存空串，所以**理由必须在界面上**，见面板那句提示）。"""

    def coerced(self, raw):
        from core.settings_store import coerce
        return coerce("network", "proxy", raw)

    def test_bare_host_port_gets_http_scheme(self):
        self.assertEqual(self.coerced("127.0.0.1:7890"), "http://127.0.0.1:7890")

    def test_http_is_kept_as_is(self):
        self.assertEqual(self.coerced("http://p:8080"), "http://p:8080")

    def test_https_and_socks_are_rejected(self):
        # https：上游正则 `(http|socks4|socks5)://…` 漏掉它，然后 ms.first() 直接抛异常
        # ——留着比丢掉更糟（那不是「没生效」，是把这次请求炸掉）
        self.assertEqual(self.coerced("https://p:8080"), "")
        self.assertEqual(self.coerced("socks5://127.0.0.1:1080"), "")

    def test_garbage_is_rejected(self):
        self.assertEqual(self.coerced("怪东西"), "")
        self.assertEqual(self.coerced(""), "")


class MigrationTests(unittest.TestCase):
    """老的 `check.proxy` 要搬过来（它原来只服务本地校验链，现在两条引擎路都读新的那个）。"""

    def test_old_value_is_carried_over_once(self):
        import core.settings_store as S
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / "settings.json"
            path.write_text(json.dumps({"check": {"proxy": "http://old:1"}}), encoding="utf-8")
            with mock.patch.object(S, "settings_path", return_value=str(path)):
                self.assertEqual(S.resolve_proxy(), "http://old:1")

    def test_the_new_key_wins_when_both_exist(self):
        import core.settings_store as S
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / "settings.json"
            path.write_text(json.dumps({"check": {"proxy": "http://old:1"},
                                        "network": {"proxy": "http://new:2"}}),
                            encoding="utf-8")
            with mock.patch.object(S, "settings_path", return_value=str(path)):
                self.assertEqual(S.resolve_proxy(), "http://new:2")


class ArgsFileTests(unittest.TestCase):
    """浏览器那一侧的代理走 args.properties（`_write_args`）。

    **为什么是那个文件而不是环境变量**：常驻 daemon 早就起来了、环境变量它读不到；
    而这个文件每次运行都重写，Kotlin 侧 `AppserviceEnv.loadArgs()` 现读现用。
    """

    def _written(self, proxy):
        import core.jvm_debug as J
        with tempfile.TemporaryDirectory() as d:
            args = pathlib.Path(d) / "args.properties"
            with mock.patch.object(J, "ARGS", args):
                J._write_args("src.json", "我", "out.ndjson", 60, "", proxy)
            return args.read_text(encoding="utf-8")

    def test_proxy_is_written_when_set(self):
        text = self._written("http://127.0.0.1:7890")
        self.assertIn("proxy=http://127.0.0.1:7890", text)

    def test_no_proxy_line_when_empty(self):
        self.assertNotIn("proxy=", self._written(""))


class EngineWiringTests(unittest.TestCase):
    """两条引擎路与生成链都要走它——「我们走代理、App 不走」是查不出来的不一致。"""

    def test_the_debug_endpoint_passes_the_global_proxy(self):
        src = (pathlib.Path(__file__).parent.parent / "backend/api/rules.py").read_text(
            encoding="utf-8")
        self.assertIn("resolve_proxy()", src, "/rules/jvm-debug 没把全局代理交下去")

    def test_the_batch_exporter_applies_it_per_source(self):
        src = (pathlib.Path(__file__).parent.parent / "backend/api/jvm.py").read_text(
            encoding="utf-8")
        self.assertIn("apply_proxy_header(row, proxy)", src,
                      "跑批导出的源没带代理（那就会「跑批没走、调试走了」）")

    def test_the_generation_chain_reads_it_too(self):
        src = (pathlib.Path(__file__).parent.parent / "core/jvm_debug.py").read_text(
            encoding="utf-8")
        self.assertGreaterEqual(src.count("resolve_proxy()"), 2,
                                "生成链取页（page_from_engine）与验证（verify_generated）都要读")


if __name__ == "__main__":
    unittest.main()
