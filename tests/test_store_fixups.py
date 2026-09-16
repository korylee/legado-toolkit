# -*- coding: utf-8 -*-
"""两处存量脏数据的一次性摆正。

## `enabledExplore` 必须与发现配置一致

**它是个算得出来的状态，却一直被交给用户手工维护**，于是必然会漂。
实测全库 3861 条里 **885 条不一致**：

  - 739 条「开关开着，却完全没配发现规则」→ App 的发现页里是点了没反应的死项
  - 146 条「配了发现，开关却是关的」     → 功能被静默关掉

编辑弹窗已改成保存时推导（没配一律关，配了则遵从「隐藏」那个例外）；
这里守的是**存量**：只有用户手动保存过的源才会被弹窗修正。

## `bookSourceType` 的脏值必须归 0

Legado 的 `@IntDef` 只有 0/1/2/3，库里有过 `4`（实测 5 条）。`clean_source` 已
补上归一（导入与保存都走它），这里守的是存量。
"""

from __future__ import annotations

import os
import shutil
import unittest
import uuid

from core.loader import fingerprint
from core.store import Store

_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
os.makedirs(_ROOT, exist_ok=True)


def make_source(url: str, **over) -> dict:
    src = {
        "bookSourceName": "测试源",
        "bookSourceUrl": url,
        "bookSourceType": 0,
        "bookSourceGroup": "",
        "enabled": True,
        "ruleSearch": {"bookList": ".book"},
        # 默认**不带** enabledExplore：让每条用例自己声明它想要的状态
    }
    src.update(over)
    return src


class EnabledExploreMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = os.path.join(_ROOT, "tmp_explore_" + uuid.uuid4().hex[:8])
        os.makedirs(self.root)
        self.db = os.path.join(self.root, "sources.sqlite3")

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _flag(self, st: Store, url: str):
        """直接读库里的 raw_json，不走 export（要验的就是落盘的那份）。"""
        import json
        row = st.conn.execute(
            "SELECT raw_json FROM sources WHERE source_url = ?", (url,)).fetchone()
        return json.loads(row["raw_json"]).get("enabledExplore")

    def test_flag_on_without_any_explore_config_is_turned_off(self) -> None:
        """**739 条那一类**：开关开着却没配置 → 关掉。

        不关的话 App 的发现页会把它列出来，点进去什么都没有。
        """
        with Store(self.db) as st:
            st.conn.execute("DELETE FROM meta WHERE key='enabled_explore_fixed_at'")
            st.conn.commit()
            st.upsert_sources([make_source("https://a.com", enabledExplore=True)])
            st.fix_enabled_explore_once()
            self.assertFalse(self._flag(st, "https://a.com"))

    def test_flag_off_but_configured_is_turned_on(self) -> None:
        """**146 条那一类**：配了发现却被关着 → 打开（功能被静默关掉了）。"""
        with Store(self.db) as st:
            st.conn.execute("DELETE FROM meta WHERE key='enabled_explore_fixed_at'")
            st.conn.commit()
            st.upsert_sources([make_source(
                "https://b.com", enabledExplore=False,
                exploreUrl="https://b.com/discover")])
            st.fix_enabled_explore_once()
            self.assertTrue(self._flag(st, "https://b.com"))

    def test_consistent_rows_are_left_alone(self) -> None:
        """已经一致的**不写**——否则每次升级都会把所有源的 updated_at 洗一遍。"""
        with Store(self.db) as st:
            st.conn.execute("DELETE FROM meta WHERE key='enabled_explore_fixed_at'")
            st.conn.commit()
            st.upsert_sources([
                make_source("https://c.com", enabledExplore=True,
                            exploreUrl="https://c.com/discover"),
                make_source("https://d.com", enabledExplore=False),
            ])
            before = st.conn.execute(
                "SELECT source_url, updated_at FROM sources ORDER BY source_url").fetchall()
            changed = st.fix_enabled_explore_once()
            after = st.conn.execute(
                "SELECT source_url, updated_at FROM sources ORDER BY source_url").fetchall()
        self.assertFalse(changed, "没有不一致的行时不该报告「改过」")
        self.assertEqual([tuple(r) for r in before], [tuple(r) for r in after])

    def test_it_runs_only_once(self) -> None:
        """带 guard：跑过就不再扫表——**之后新出现的不一致也不再管**。

        guard 的用处是「别每次启动都全表扫一遍」。光断言「第二次返回 False」
        **证明不了它有 guard**：没有 guard 时第二次同样会因为「已经一致」而返回
        False——造 M34 时实测如此。所以这里**故意再制造一条不一致**，看它动不动手。
        """
        with Store(self.db) as st:
            st.conn.execute("DELETE FROM meta WHERE key='enabled_explore_fixed_at'")
            st.conn.commit()
            st.upsert_sources([make_source("https://e.com", enabledExplore=True)])
            self.assertTrue(st.fix_enabled_explore_once(), "第一次该动手")

            # 摆正之后再塞一条不一致的进来
            st.upsert_sources([make_source("https://h.com", enabledExplore=True)])
            self.assertFalse(st.fix_enabled_explore_once(), "第二次不该再动手")
            self.assertTrue(self._flag(st, "https://h.com"),
                            "guard 生效后，新出现的不一致不再被这次迁移修")

    def test_rule_explore_alone_also_counts_as_configured(self) -> None:
        """判据是「**有任一**发现配置」——只有 `ruleExplore` 也算配了。"""
        with Store(self.db) as st:
            st.conn.execute("DELETE FROM meta WHERE key='enabled_explore_fixed_at'")
            st.conn.commit()
            st.upsert_sources([make_source(
                "https://f.com", enabledExplore=False,
                ruleExplore={"bookList": ".item"})])
            st.fix_enabled_explore_once()
            self.assertTrue(self._flag(st, "https://f.com"))

    def test_fingerprint_is_untouched(self) -> None:
        """**改它不能动指纹**——指纹变了会让这条源的校验缓存整体作废。

        `fingerprint` 只覆盖 name/url/searchUrl/ruleSearch/ruleToc/ruleContent/exploreUrl
        （见 `core.loader`），`enabledExplore` 不在其中。这条断言把这个事实钉住：
        将来谁把 enabledExplore 加进指纹，就会在这里看见代价。
        """
        src = make_source("https://g.com", enabledExplore=True)
        fp_before = fingerprint(src)
        with Store(self.db) as st:
            st.conn.execute("DELETE FROM meta WHERE key='enabled_explore_fixed_at'")
            st.conn.commit()
            st.upsert_sources([src])
            st.fix_enabled_explore_once()
            row = st.conn.execute(
                "SELECT fingerprint FROM sources WHERE source_url='https://g.com'").fetchone()
        self.assertEqual(row["fingerprint"], fp_before)


class DirtySourceTypeMigrationTests(unittest.TestCase):
    """`bookSourceType` 的脏值（Legado 里不存在的取值）归 0。"""

    def setUp(self) -> None:
        self.root = os.path.join(_ROOT, "tmp_dirtytype_" + uuid.uuid4().hex[:8])
        os.makedirs(self.root)
        self.db = os.path.join(self.root, "sources.sqlite3")

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _row(self, st: Store, url: str):
        import json
        r = st.conn.execute(
            "SELECT source_type, raw_json FROM sources WHERE source_url=?", (url,)).fetchone()
        return r["source_type"], json.loads(r["raw_json"]).get("bookSourceType")

    def test_dirty_value_is_normalized_in_both_places(self) -> None:
        """**列与 raw_json 都要改**——只改一边会出现「列表按 4 分组、导出的却是 0」。"""
        with Store(self.db) as st:
            st.conn.execute("DELETE FROM meta WHERE key='dirty_source_type_fixed_at'")
            st.conn.commit()
            st.upsert_sources([make_source("https://a.com", bookSourceType=4)])
            self.assertTrue(st.fix_dirty_source_type_once())
            self.assertEqual(self._row(st, "https://a.com"), (0, 0))

    def test_legal_types_are_left_alone(self) -> None:
        """反向断言：0/1/2/3 一个都不能动——归一化写成恒 0 就会全绿。"""
        with Store(self.db) as st:
            st.conn.execute("DELETE FROM meta WHERE key='dirty_source_type_fixed_at'")
            st.conn.commit()
            st.upsert_sources([make_source("https://b.com", bookSourceType=3),
                               make_source("https://c.com", bookSourceType=2)])
            self.assertFalse(st.fix_dirty_source_type_once(), "没有脏值时不该报告改过")
            self.assertEqual(self._row(st, "https://b.com"), (3, 3))
            self.assertEqual(self._row(st, "https://c.com"), (2, 2))


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------- 变异记录
# 以下为实测（改坏 → `python -B -m unittest tests.test_explore_flag` → 确认变红 → 还原）。
#
#  M33  `want` 的判据只看 `exploreUrl`（漏掉 `ruleExplore`）
#         → test_rule_explore_alone_also_counts_as_configured 红
#  M34  去掉 `enabled_explore_fixed_at` 那道 guard（每次都扫全表）
#         → test_it_runs_only_once 红
#         ⚠️ **第一版测试没抓住它**：那时只断言「第二次返回 False」，而没有 guard
#         时第二次同样会因为「已经一致」返回 False——两种情况长得一样。
#         改成「摆正之后再塞一条不一致的，看它动不动手」才拦住。
#         与 M32 同一课：**断言结果相同，不代表守住了过程**。
#  M36  `fix_dirty_source_type_once` 只改 `source_type` 列、不动 `raw_json`
#         → test_dirty_value_is_normalized_in_both_places 红（(0, 4) != (0, 0)）
#         **两处都要改**：列供筛选/统计/分组，raw_json 是导出与指纹的来源；
#         只改一边会出现「列表按 4 分组、导出的却是 0」
#  M35  不跳过已经一致的行（无条件重写 raw_json）
#         → test_consistent_rows_are_left_alone 红
#         （那会把所有源的 updated_at 白洗一遍，而 updated_at 是有用的数据）
