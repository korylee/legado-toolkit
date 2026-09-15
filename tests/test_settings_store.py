# -*- coding: utf-8 -*-
"""全局设置存储测试。

隔离手法照 test_llm_store.py：靠环境变量把配置路径指到 data/ 下的临时文件，
不 mock、不动真实的 data/config/settings.json。
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
        self.assertEqual(S.load()["check"], S.DEFAULTS["check"])

    def test_broken_json_falls_back_to_defaults_without_raising(self) -> None:
        """设置坏了不该让校验任务起不来——降级方向只能是「用默认」。"""
        self._write_raw("{ 这不是 json")
        self.assertEqual(S.load()["check"], S.DEFAULTS["check"])

    def test_non_dict_json_falls_back_to_defaults(self) -> None:
        for text in ("[1, 2, 3]", '"x"', "null", "123"):
            with self.subTest(text=text):
                self._write_raw(text)
                self.assertEqual(S.load()["check"], S.DEFAULTS["check"])

    def test_partial_file_keeps_defaults_for_missing_keys(self) -> None:
        """逐键回落，不是整段替换：只写了一半的键也要能起来。"""
        self._write_raw('{"check": {"concurrency": 10}}')
        got = S.load()["check"]
        self.assertEqual(got["concurrency"], 10)
        self.assertEqual(got["timeout"], S.DEFAULTS["check"]["timeout"])
        self.assertEqual(got["probe_depth"], 1)

    def test_unknown_keys_are_hidden_on_read(self) -> None:
        self._write_raw('{"check": {"concurrency": 10, "keyword": "我"}}')
        self.assertNotIn("keyword", S.load()["check"])

    def test_load_does_not_leak_mutable_defaults(self) -> None:
        """load() 返回的字典改了不能影响 DEFAULTS，否则一次误改会污染全进程。"""
        got = S.load()
        got["check"]["concurrency"] = 12345
        self.assertEqual(S.DEFAULTS["check"]["concurrency"], 50)

    # ---------------------------------------------------------------- 类型与范围

    def test_concurrency_is_clamped(self) -> None:
        self.assertEqual(S.coerce("check", "concurrency", 0), 1)
        self.assertEqual(S.coerce("check", "concurrency", 9999), 200)
        self.assertEqual(S.coerce("check", "concurrency", "20"), 20)

    def test_bool_is_not_accepted_as_int(self) -> None:
        """bool 是 int 的子类，不拦住的话 True 会静默变成「并发 1」。"""
        self.assertEqual(S.coerce("check", "concurrency", True), 50)
        self.assertEqual(S.coerce("check", "timeout", False), 8.0)

    def test_timeout_rejects_nan_and_inf(self) -> None:
        """min/max 对 nan 的比较恒为 False，clamp 会静默给出错值。"""
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(bad=bad):
                self.assertEqual(S.coerce("check", "timeout", bad), 8.0)

    def test_timeout_is_clamped(self) -> None:
        self.assertEqual(S.coerce("check", "timeout", 0.1), 1.0)
        self.assertEqual(S.coerce("check", "timeout", 9999), 120.0)

    def test_probe_depth_converges_to_allowed_values(self) -> None:
        """与 core/checker.py 同一口径：不在 1/2/3 里就落 1，不是「就近取整」。"""
        self.assertEqual(S.coerce("check", "probe_depth", 2), 2)
        self.assertEqual(S.coerce("check", "probe_depth", "3"), 3)
        self.assertEqual(S.coerce("check", "probe_depth", 5), 1)
        self.assertEqual(S.coerce("check", "probe_depth", 0), 1)
        self.assertEqual(S.coerce("check", "probe_depth", None), 1)
        self.assertEqual(S.coerce("check", "probe_depth", True), 1)

    def test_checker_and_settings_share_one_probe_depth_rule(self) -> None:
        """两处收敛必须是同一份常量，不能各写一个 (1, 2, 3)。"""
        from core import checker
        self.assertEqual(S.PROBE_DEPTHS, (1, 2, 3))
        self.assertIs(checker.PROBE_DEPTHS, S.PROBE_DEPTHS)

    def test_bool_accepts_word_forms(self) -> None:
        self.assertFalse(S.coerce("check", "verify_ssl", "false"))
        self.assertTrue(S.coerce("check", "probe_search", "on"))
        self.assertEqual(S.coerce("check", "verify_ssl", "听不懂"), True)

    def test_proxy_rejects_non_http_schemes(self) -> None:
        """socks5 在 aiohttp 下连不上（见 core/fetch.py 的既有结论），不能放行。"""
        self.assertEqual(S.coerce("check", "proxy", "socks5://127.0.0.1:1080"), "")
        self.assertEqual(S.coerce("check", "proxy", "ftp://x"), "")
        self.assertEqual(S.coerce("check", "proxy", 123), "")
        self.assertEqual(S.coerce("check", "proxy", None), "")

    def test_proxy_is_stripped_and_http_kept(self) -> None:
        self.assertEqual(S.coerce("check", "proxy", "  http://127.0.0.1:7890  "),
                         "http://127.0.0.1:7890")
        self.assertEqual(S.coerce("check", "proxy", "HTTPS://proxy:8080"),
                         "HTTPS://proxy:8080")

    def test_unknown_key_coerce_returns_none(self) -> None:
        self.assertIsNone(S.coerce("check", "keyword", "我"))
        self.assertIsNone(S.coerce("nope", "concurrency", 10))

    # ---------------------------------------------------------------- 写入

    def test_update_only_touches_keys_present_in_patch(self) -> None:
        S.update({"check": {"concurrency": 10}})
        S.update({"check": {"timeout": 20}})
        got = S.load()["check"]
        self.assertEqual(got["concurrency"], 10)
        self.assertEqual(got["timeout"], 20.0)

    def test_update_ignores_unknown_sections_and_keys(self) -> None:
        S.update({"check": {"concurrency": 10}, "app": {"port": 1}, "bogus": 1})
        self.assertEqual(S.load()["check"]["concurrency"], 10)
        self.assertNotIn("app", self._read_raw())

    def test_update_with_empty_patch_changes_nothing(self) -> None:
        S.update({"check": {"concurrency": 10}})
        before = S.load()["check"]
        S.update({})
        self.assertEqual(S.load()["check"], before)

    def test_update_writes_coerced_values_to_disk(self) -> None:
        """写盘前就收敛：别人 cat 这个文件看到的就是实际生效的值。

        若改成「读的时候才收敛」，磁盘上会留下 9999，而它看起来像生效的配置。
        """
        S.update({"check": {"concurrency": 9999, "probe_depth": 5}})
        raw = self._read_raw()["check"]
        self.assertEqual(raw["concurrency"], 200)
        self.assertEqual(raw["probe_depth"], 1)

    def test_explicit_null_restores_that_key_to_default(self) -> None:
        S.update({"check": {"concurrency": 10}})
        S.update({"check": {"concurrency": None}})
        self.assertEqual(S.load()["check"]["concurrency"], 50)

    def test_reset_restores_defaults(self) -> None:
        S.update({"check": {"concurrency": 10, "proxy": "http://127.0.0.1:7890"}})
        S.reset()
        self.assertEqual(S.load()["check"], S.DEFAULTS["check"])

    def test_atomic_save_leaves_no_tmp_file(self) -> None:
        """原子写的**结果**能守，原子性本身守不了（崩在 write 与 replace 之间
        在单测里复现不出来）——见文件末尾的守不住清单。"""
        S.update({"check": {"concurrency": 10}})
        self.assertFalse(os.path.exists(self.path + ".tmp"))

    # ---------------------------------------------------------------- 取值优先级

    def test_resolve_without_override_equals_global(self) -> None:
        S.update({"check": {"concurrency": 10}})
        self.assertEqual(S.resolve_check(None), S.load()["check"])

    def test_resolve_none_means_fall_back_to_global(self) -> None:
        """None = 没传 -> 用全局值（10），**不是**编译期默认值 50。"""
        S.update({"check": {"concurrency": 10}})
        self.assertEqual(S.resolve_check({"concurrency": None})["concurrency"], 10)

    def test_resolve_keeps_false_and_empty_string_from_override(self) -> None:
        """False / "" 是有效值，不是「没传」。这是整个设计最脆的一环。

        用 `or` 合并会把这三种覆盖静默吃掉，现象只是「参数好像没生效」。
        """
        S.update({"check": {"probe_search": True, "proxy": "http://127.0.0.1:7890"}})
        got = S.resolve_check({"probe_search": False, "proxy": ""})
        self.assertFalse(got["probe_search"])
        self.assertEqual(got["proxy"], "")

    def test_resolve_clamps_override_too(self) -> None:
        """覆盖值同样不可信：传 9999 不能绕过区间检查直接进 Semaphore。"""
        self.assertEqual(S.resolve_check({"concurrency": 9999})["concurrency"], 200)
        self.assertEqual(S.resolve_check({"probe_depth": 9})["probe_depth"], 1)

    def test_resolve_does_not_write_back_to_global(self) -> None:
        """覆盖只作用于本次，不能污染全局——否则两个标签页同时校验会互相干扰。"""
        S.update({"check": {"concurrency": 10}})
        S.resolve_check({"concurrency": 3})
        self.assertEqual(S.load()["check"]["concurrency"], 10)


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------- 变异记录
# 以下为实测（改坏 → `python -B -m unittest tests.test_settings_store` → 确认变红 → 还原）。
#
#  M1  resolve_check 的判定改成 `if value:`（用 or 代替 is not None）
#        → test_resolve_keeps_false_and_empty_string_from_override 红
#  M2  去掉 resolve_check 里对覆盖值的 coerce
#        → test_resolve_clamps_override_too 红
#  M3  _to_int 去掉 isinstance(value, bool) 拦截
#        → test_bool_is_not_accepted_as_int 红
#  M4  _to_float 去掉 math.isfinite 拦截
#        → test_timeout_rejects_nan_and_inf 红
#  M5  update() 不收敛就落盘（raw[key] 直接写）
#        → test_update_writes_coerced_values_to_disk 红
#
# **守不住的**：原子写本身（进程崩在 write 与 replace 之间）在单测里复现不出来，
# test_atomic_save_leaves_no_tmp_file 只能守「tmp 文件没残留」。
# 这一条没有测试护栏，只能靠实现唯一——见 core/settings_store._atomic_save 的注释。
