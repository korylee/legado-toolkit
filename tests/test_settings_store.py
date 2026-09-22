# -*- coding: utf-8 -*-
"""全局设置：读容错、收敛、patch 语义。

**校验参数那一半（`check.*`）随本地校验链退场**（十-4）：设置里现在只有两段——
`network`（这台机器怎么出去）与 `jvm`（本机引擎的环境）。所以这里的用例也从
「校验参数」换成那两段；**要守的性质没变**：坏文件降级成默认、逐键回落、
未登记的键读不到、patch 只动给出的键、显式 null 回落默认、reset 恢复默认。
"""

from __future__ import annotations

import json
import os
import unittest
import uuid

from core import settings_store as S


_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
os.makedirs(_ROOT, exist_ok=True)


class SettingsStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.path = os.path.join(_ROOT, "tmp_settings_" + uuid.uuid4().hex[:8] + ".json")
        os.environ["LEGADO_SETTINGS"] = self.path

    def tearDown(self) -> None:
        os.environ.pop("LEGADO_SETTINGS", None)
        for p in (self.path, self.path + ".tmp"):
            if os.path.exists(p):
                os.remove(p)

    def _write_raw(self, text: str) -> None:
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(text)

    def _read_raw(self) -> dict:
        with open(self.path, "r", encoding="utf-8") as f:
            return json.load(f)

    # ---------------------------------------------------------------- 读取容错

    def test_missing_file_returns_defaults(self) -> None:
        self.assertEqual(S.load()["network"], S.DEFAULTS["network"])
        self.assertEqual(S.load()["jvm"], S.DEFAULTS["jvm"])

    def test_broken_json_falls_back_to_defaults_without_raising(self) -> None:
        """设置坏了不该让功能起不来——降级方向只能是「用默认」。"""
        self._write_raw("{ 这不是 json")
        self.assertEqual(S.load()["jvm"], S.DEFAULTS["jvm"])

    def test_non_dict_json_falls_back_to_defaults(self) -> None:
        for text in ("[1, 2, 3]", '"x"', "null", "123"):
            with self.subTest(text=text):
                self._write_raw(text)
                self.assertEqual(S.load()["jvm"], S.DEFAULTS["jvm"])

    def test_partial_file_keeps_defaults_for_missing_keys(self) -> None:
        """逐键回落，不是整段替换：只写了一半的键也要能起来。"""
        self._write_raw('{"jvm": {"app_repo": "X:/repo"}}')
        got = S.load()["jvm"]
        self.assertEqual(got["app_repo"], "X:/repo")
        self.assertEqual(got["timeout"], S.DEFAULTS["jvm"]["timeout"],
                         "没写的那几个键要拿到默认值")

    def test_unknown_keys_are_hidden_on_read(self) -> None:
        """未登记的键不该出现在读出来的结果里（它是「谁能改」的白名单）。"""
        self._write_raw('{"jvm": {"app_repo": "X:/repo", "没这个键": 1}}')
        self.assertNotIn("没这个键", S.load()["jvm"])

    def test_load_does_not_leak_mutable_defaults(self) -> None:
        """`DEFAULTS` 是模块级共享对象——读出来的那份改了不能污染它。"""
        got = S.load()
        got["jvm"]["app_repo"] = "改坏了"
        self.assertEqual(S.DEFAULTS["jvm"]["app_repo"], "")
        self.assertNotEqual(S.load()["jvm"]["app_repo"], "改坏了")

    # ---------------------------------------------------------------- 收敛

    def test_int_is_clamped_to_limits(self) -> None:
        lo, hi = S.LIMITS["jvm_timeout"]
        self.assertEqual(S.coerce("jvm", "timeout", hi + 1000), hi)
        self.assertEqual(S.coerce("jvm", "timeout", lo - 1000), lo)

    def test_bool_is_not_accepted_as_int(self) -> None:
        """`True` 是 int 的子类——不挡的话 `concurrency=True` 会当成 1 悄悄生效。"""
        self.assertEqual(S.coerce("jvm", "concurrency", True),
                         S.DEFAULTS["jvm"]["concurrency"])

    def test_timeout_rejects_nan_and_inf(self) -> None:
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(bad=bad):
                self.assertEqual(S.coerce("jvm", "timeout", bad),
                                 S.DEFAULTS["jvm"]["timeout"])

    def test_path_is_stripped(self) -> None:
        self.assertEqual(S.coerce("jvm", "app_repo", "  X:/repo  "), "X:/repo")

    def test_proxy_rejects_non_http_schemes(self) -> None:
        """上游拿正则匹配代理串：`https://` 匹配不到会直接抛异常，留着比丢掉更糟。"""
        for bad in ("socks5://127.0.0.1:1080", "https://p:8080", "怪东西"):
            with self.subTest(bad=bad):
                self.assertEqual(S.coerce("network", "proxy", bad), "")

    def test_proxy_bare_host_port_gets_http(self) -> None:
        self.assertEqual(S.coerce("network", "proxy", "127.0.0.1:7890"),
                         "http://127.0.0.1:7890")

    def test_unknown_key_coerce_returns_none(self) -> None:
        """没登记的键一律 None（由 update 丢弃）——别让手改的脏键进得来。"""
        self.assertIsNone(S.coerce("jvm", "没这个键", 1))
        self.assertIsNone(S.coerce("没这个段", "app_repo", 1))

    # ---------------------------------------------------------------- patch 语义

    def test_update_only_touches_keys_present_in_patch(self) -> None:
        S.update({"jvm": {"app_repo": "X:/repo"}})
        S.update({"network": {"proxy": "http://p:1"}})
        got = S.load()
        self.assertEqual(got["jvm"]["app_repo"], "X:/repo", "上一次改的要留着")
        self.assertEqual(got["network"]["proxy"], "http://p:1")

    def test_update_ignores_unknown_sections_and_keys(self) -> None:
        S.update({"没这个段": {"x": 1}})
        S.update({"jvm": {"没这个键": 1}})
        self.assertNotIn("没这个段", S.load())
        self.assertNotIn("没这个键", S.load()["jvm"])

    def test_update_with_empty_patch_changes_nothing(self) -> None:
        before = S.load()
        S.update({})
        self.assertEqual(S.load(), before)

    def test_update_writes_coerced_values_to_disk(self) -> None:
        S.update({"network": {"proxy": "127.0.0.1:7890"}})
        self.assertEqual(self._read_raw()["network"]["proxy"], "http://127.0.0.1:7890",
                         "落盘的是**收敛过**的值")

    def test_explicit_null_restores_that_key_to_default(self) -> None:
        S.update({"network": {"proxy": "http://p:1"}})
        S.update({"network": {"proxy": None}})
        self.assertEqual(S.load()["network"]["proxy"], S.DEFAULTS["network"]["proxy"])

    def test_reset_restores_defaults(self) -> None:
        S.update({"jvm": {"app_repo": "X:/repo"}})
        S.reset()
        self.assertEqual(S.load()["jvm"], S.DEFAULTS["jvm"])

    def test_atomic_save_leaves_no_tmp_file(self) -> None:
        S.update({"jvm": {"app_repo": "X:/repo"}})
        self.assertFalse(os.path.exists(self.path + ".tmp"))

    # ---------------------------------------------------------------- 代理入口

    def test_resolve_proxy_reads_the_network_section(self) -> None:
        S.update({"network": {"proxy": "http://p:1"}})
        self.assertEqual(S.resolve_proxy(), "http://p:1")
        S.update({"network": {"proxy": ""}})
        self.assertEqual(S.resolve_proxy(), "", "空 = 直连")

    def test_old_check_proxy_is_migrated(self) -> None:
        """老键 `check.proxy` 搬一次（十-3）：它原来只服务本地校验链。"""
        self._write_raw(json.dumps({"check": {"proxy": "http://old:1"}}))
        self.assertEqual(S.resolve_proxy(), "http://old:1")


if __name__ == "__main__":
    unittest.main()
