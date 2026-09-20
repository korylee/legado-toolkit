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
        # 深度这一项有一层迁移：没写 schema_version 的旧文件按 v1 处理，
        # 「深度缺省（1）+ 搜索探开」→ 搜索档，正好与新默认值同值（见 _migrate_legacy）
        self.assertEqual(got["probe_depth"], S.DEFAULTS["check"]["probe_depth"])
        self.assertEqual(got["probe_depth"], 2)

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
        """两处收敛必须是同一份常量，不能各写一个四档表。

        连四个档位的**编号含义**（哪一档验到什么）也只在这里定义一次——
        checker 里写的都是 DEPTH_* 常量，不是字面量。
        """
        from core import checker
        self.assertEqual(S.PROBE_DEPTHS, (1, 2, 3, 4))
        self.assertIs(checker.PROBE_DEPTHS, S.PROBE_DEPTHS)
        for name in ("DEPTH_HOME", "DEPTH_SEARCH", "DEPTH_TOC", "DEPTH_CONTENT"):
            with self.subTest(const=name):
                self.assertIs(getattr(checker, name), getattr(S, name))

    def test_home_tier_is_not_offered_but_still_legal(self) -> None:
        """**「不展示」与「不合法」是两件事**（2026-09-20 撤掉主页档）。

        下发给界面渲染下拉的那份从搜索档起；而值 1 必须继续合法、也继续是非法值的兜底：
        ① `_migrate_legacy` 把「没开搜索探测」的 v1 配置映射成它；② 历史
        `checks.probe_depth` 行里存着它；③ 列表页还要靠它把老结论的档位写出来
        （`DEPTH_SHORT[probe_depth]`）。**编号也不许平移**——一平移，整列历史值的含义
        就变了（AGENTS #5b），所以下面把编号一起钉住。
        """
        self.assertEqual(S.PROBE_DEPTH_CHOICES, (2, 3, 4))
        self.assertNotIn(S.DEPTH_HOME, S.PROBE_DEPTH_CHOICES)
        self.assertIs(S.LIMITS["probe_depth"], S.PROBE_DEPTH_CHOICES,
                      "界面渲染下拉读的就是 limits，别让它继续下发四档")
        # 「1 还合法」这件事**没法用 coerce 的返回值来钉**：值与兜底值都是 1，
        # 所以 `coerce(1) == 1` 在「1 被拒绝、走了兜底」时也照样绿。要钉就钉集合成员
        # ——它才是「合法档位」这件事本身（`PROBE_DEPTHS` 一行解包四个常量，
        # 真删掉 1 会当场炸在 import 上，所以这条断言更像一张写着原因的告示）。
        self.assertIn(S.DEPTH_HOME, S.PROBE_DEPTHS, "1 必须继续是合法档位（历史结论/迁移产物）")
        self.assertEqual(S.DEPTH_HOME, 1, "编号不许平移——它一直是 1")
        self.assertEqual(S.coerce("check", "probe_depth", 9), S.DEPTH_HOME,
                         "非法输入的落点仍是主页档（与 AsyncChecker 同一口径）")
        self.assertEqual(S.DEFAULTS["check"]["probe_depth"], S.DEPTH_SEARCH,
                         "默认档位本来就落在搜索档——撤掉主页档不该改默认行为")

    def test_bool_accepts_word_forms(self) -> None:
        self.assertFalse(S.coerce("check", "verify_ssl", "false"))
        self.assertTrue(S.coerce("check", "verify_ssl", "on"))
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
        S.update({"check": {"verify_ssl": True, "proxy": "http://127.0.0.1:7890"}})
        got = S.resolve_check({"verify_ssl": False, "proxy": ""})
        self.assertFalse(got["verify_ssl"])
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


class LegacyDepthMigrationTests(unittest.TestCase):
    """v1 → v2 的深度迁移：两根轴（深度 + 搜索开关）折成一根四档。

    这是这次改动**唯一会动到用户已有设置**的地方，而且改错了不会报错——只会让
    下一次校验悄悄少验或多验。映射规则（旧的有效行为 → 新档位）：

        旧深度 2 / 3          → 3 / 4
        旧深度 1 + 搜索探打开  → 2
        旧深度 1 + 搜索探关闭  → 1
    """

    def setUp(self) -> None:
        self.path = os.path.join(_ROOT, "tmp_settings_" + uuid.uuid4().hex[:8] + ".json")
        os.environ["LEGADO_SETTINGS"] = self.path

    def tearDown(self) -> None:
        os.environ.pop("LEGADO_SETTINGS", None)
        for p in (self.path, self.path + ".tmp"):
            if os.path.exists(p):
                os.remove(p)

    def _depth_from(self, raw: dict) -> int:
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False)
        return S.load()["check"]["probe_depth"]

    def test_v1_two_axes_fold_into_one(self) -> None:
        cases = [
            ({"schema_version": 1, "check": {"probe_depth": 1, "probe_search": True}}, 2),
            ({"schema_version": 1, "check": {"probe_depth": 1, "probe_search": False}}, 1),
            ({"schema_version": 1, "check": {"probe_depth": 2, "probe_search": True}}, 3),
            ({"schema_version": 1, "check": {"probe_depth": 3, "probe_search": True}}, 4),
        ]
        for raw, want in cases:
            with self.subTest(raw=raw):
                self.assertEqual(self._depth_from(raw), want)

    def test_missing_probe_search_counts_as_on(self) -> None:
        """缺键按 True 算——那是它当年的默认值。

        按 False 处理会把搜索探测**静默关掉**（search_hit 全空、星级整体下降），
        而用户什么都没改。
        """
        self.assertEqual(self._depth_from({"schema_version": 1,
                                           "check": {"probe_depth": 1}}), 2)

    def test_missing_schema_version_is_v1(self) -> None:
        """判据是 schema_version，不是「旧键在不在」：手改过的文件可能只写了一半。"""
        self.assertEqual(self._depth_from({"check": {"probe_depth": 2}}), 3)

    def test_v2_file_is_not_migrated_again(self) -> None:
        # 已经迁过的 4 档不能再 +1（幂等：读多少次都是它自己）
        for d in (1, 2, 3, 4):
            with self.subTest(depth=d):
                self.assertEqual(
                    self._depth_from({"schema_version": 2, "check": {"probe_depth": d}}), d)

    def test_migration_is_idempotent_and_legacy_key_is_dropped(self) -> None:
        raw = {"schema_version": 1, "check": {"probe_depth": 2, "probe_search": True}}
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(raw, f)
        self.assertEqual([S.load()["check"]["probe_depth"] for _ in range(3)], [3, 3, 3])
        S.update({"check": {"concurrency": 60}})      # 落盘一次
        saved = self._read_raw()
        self.assertEqual(saved["schema_version"], S.VERSION)
        self.assertNotIn("probe_search", saved["check"])

    def _read_raw(self) -> dict:
        with open(self.path, "r", encoding="utf-8") as f:
            return json.load(f)


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
#  M6  _migrate_legacy 的 `probe_search` 缺省改成 False
#        → LegacyDepthMigrationTests 红 2 条（test_missing_probe_search_counts_as_on
#           + test_v1_two_axes_fold_into_one）
#  M7  迁移判据从 schema_version 改回「`probe_search` 键在不在」
#        → LegacyDepthMigrationTests 红 2 条（test_missing_schema_version_is_v1
#           + test_migration_is_idempotent_and_legacy_key_is_dropped）
#
# **守不住的**：原子写本身（进程崩在 write 与 replace 之间）在单测里复现不出来，
# test_atomic_save_leaves_no_tmp_file 只能守「tmp 文件没残留」。
# 这一条没有测试护栏，只能靠实现唯一——见 core/settings_store._atomic_save 的注释。
