# -*- coding: utf-8 -*-
"""JVM 结论的读取规则：**深者胜、同深取新**（S3-3）。

守的是「同一源跑了两次不同深度的批次之后，界面该显示哪一条」：

- 用户为了改个设置只重跑了几条 `depth=search` 的快筛 → 不该把上一批
  目录/正文级的结论盖掉（界面上从「目录 856 章」退回「搜索✓」，而用户什么都没删）
- 反过来，更深的批次覆盖更浅的永远是对的——更深的结论包含更浅的全部信息

这条规则**只有一份实现**（`Store.latest_jvm_conclusions`），`/api/jvm/results`
与列表回填都调它；两处各写一次就会漂（lessons §二十三 已栽过两次）。

`ListFillTypeGateTests` 守的是**回填时的类型闸门**：结论值是 Kotlin 侧一行一个 JSON
产的，形状不对时不许把 `GET /api/sources` 整页带走。见那个类的注释。
"""

from __future__ import annotations

import json
import os
import shutil
import unittest
import uuid

from backend.api.sources import list_sources
from backend.schemas import SourcePage
from core.quality import SHORT_CONTENT_CHARS
from core.store import Store

_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
os.makedirs(_ROOT, exist_ok=True)


class LatestJvmConclusionsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = os.path.join(_ROOT, "tmp_jvm_conc_" + uuid.uuid4().hex[:8])
        os.makedirs(self.root)
        self.db = os.path.join(self.root, "sources.sqlite3")

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _put(self, st: Store, batch: str, url: str, **fields) -> None:
        st.conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
            ("jvm_check:%s:%s" % (batch, url),
             json.dumps({"url": url, **fields}, ensure_ascii=False)))
        st.conn.commit()

    def test_deeper_stage_wins_over_newer_batch(self) -> None:
        """**这条是用例的核心**：后跑的一批更浅，不能盖掉先跑的深结论。"""
        with Store(self.db) as st:
            st.set_meta("schema_version", "1")
            self._put(st, "20260919_010000", "https://a.com",
                      state="ok", stage="content", content_len=4136, toc_count=856)
            self._put(st, "20260919_090000", "https://a.com",
                      state="ok", stage="search", hit=5)
            got = st.latest_jvm_conclusions()["https://a.com"]
        self.assertEqual(got["stage"], "content")
        self.assertEqual(got["content_len"], 4136)
        self.assertEqual(got["_batch"], "20260919_010000")

    def test_same_stage_newer_batch_wins(self) -> None:
        """同深取新：重跑同一档的结果必须生效，否则修好的源永远显示旧结论。"""
        with Store(self.db) as st:
            st.set_meta("schema_version", "1")
            self._put(st, "20260919_010000", "https://a.com", state="no_result", stage="toc")
            self._put(st, "20260919_090000", "https://a.com", state="ok", stage="toc", toc_count=12)
            got = st.latest_jvm_conclusions()["https://a.com"]
        self.assertEqual(got["state"], "ok")
        self.assertEqual(got["_batch"], "20260919_090000")

    def test_missing_stage_counts_as_search(self) -> None:
        """缺 `stage` 的是 S3-1 之前那批（那时只会跑搜索段）——按事实记成 search。

        排成 0（"比任何一段都浅"）会在界面上显示成「没跑到任何一段」，与真相不符。
        """
        with Store(self.db) as st:
            st.set_meta("schema_version", "1")
            self._put(st, "20260919_010000", "https://a.com", state="ok", hit=3)
            got = st.latest_jvm_conclusions()["https://a.com"]
        self.assertEqual(got["stage"], "search")

    def test_urls_are_normalized_on_both_sides(self) -> None:
        """库里的 URL 有脏字符（前导空格/尾斜杠），两侧都要归一才能对上。"""
        with Store(self.db) as st:
            st.set_meta("schema_version", "1")
            self._put(st, "20260919_010000", "  https://b.com/  ", state="ok", stage="toc")
            got = st.latest_jvm_conclusions()
        self.assertIn("https://b.com", got)

    def test_broken_json_is_skipped_not_fatal(self) -> None:
        """一条坏 JSON 不能让整个列表回填失败（那是「静默全空」的另一种形态）。"""
        with Store(self.db) as st:
            st.set_meta("schema_version", "1")
            st.conn.execute("INSERT INTO meta(key, value) VALUES(?, ?)",
                            ("jvm_check:20260919_010000:https://c.com", "{不是 JSON"))
            st.conn.commit()
            self._put(st, "20260919_010000", "https://d.com", state="ok", stage="toc")
            got = st.latest_jvm_conclusions()
        self.assertNotIn("https://c.com", got)
        self.assertIn("https://d.com", got)


class _ListFillCase(unittest.TestCase):
    """列表回填两条测试共用的夹具（直调端点函数，不起 TestClient——本仓库惯例）。"""

    def setUp(self) -> None:
        self.root = os.path.join(_ROOT, "tmp_jvm_fill_" + uuid.uuid4().hex[:8])
        os.makedirs(self.root)
        self.db = os.path.join(self.root, "sources.sqlite3")
        # 隔离 data/：与 test_sources_api 同一套（不设的话快照/缓存会写进真目录）
        self._old_data_dir = os.environ.get("LEGADO_DATA_DIR")
        os.environ["LEGADO_DATA_DIR"] = self.root

    def tearDown(self) -> None:
        if self._old_data_dir is None:
            os.environ.pop("LEGADO_DATA_DIR", None)
        else:
            os.environ["LEGADO_DATA_DIR"] = self._old_data_dir
        shutil.rmtree(self.root, ignore_errors=True)

    def _seed(self, url: str, conclusion: dict) -> Store:
        st = Store(self.db)
        st.set_meta("schema_version", "1")
        st.upsert_sources([{
            "bookSourceName": "测试源", "bookSourceUrl": url, "bookSourceType": 0,
            "bookSourceGroup": "", "enabled": True, "ruleSearch": {"bookList": ".b"},
        }])
        self._put(st, "20260920_000000", url, **conclusion)
        return st

    def _put(self, st: Store, batch: str, url: str, **fields) -> None:
        st.conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
            ("jvm_check:%s:%s" % (batch, url),
             json.dumps({"url": url, **fields}, ensure_ascii=False)))
        st.conn.commit()

    @staticmethod
    def _list(st: Store) -> dict:
        """直调端点函数（本仓库惯例：不起 TestClient）；查询参数全显式给值——不给的话
        默认值是 FastAPI 的 Query 对象，会被当值传进 SQL。"""
        return list_sources(type=None, health="", group="", tag="", q="",
                            only_enabled=False, include_deleted=False,
                            limit=50, offset=0, order="id", st=st)

class ListFillTypeGateTests(_ListFillCase):
    """回填的**类型闸门**：形状不对的结论值不许把 `GET /api/sources` 整页带走。

    为什么要守这一条：这些值来自**另一个语言的 NDJSON**（Kotlin 一行一个 map），
    而 `SourceOut` 上的类型是给 FastAPI 的 response_model 用的。透传一个字符串
    `"null"` 给 `jvm_content_ok: Optional[bool]`，后果**不是这一行显示不对，是整页
    500**（2026-09-20 实测：`SourcePage.model_validate` 报 `items.0.jvm_content_ok`；
    生产端是把 null 写成 `"null"`，从 S3-2 埋到那天）。

    生产端已修（`ServiceJson`：null → JSON null，且两处共用一份编码器）；
    这条测试守的是**消费端那道闸**——上层形状再漂，这里降级成 None 并打一行日志，
    既不静默，也不让整页挂掉（AGENTS #4 的「显式返回原因」）。
    """

    def test_wrong_typed_values_are_none_and_the_page_still_validates(self) -> None:
        """类型不符 → 该字段 None，**出参校验必须过**（FastAPI 的 response_model 就是这一步）。"""
        with self._seed("https://junk.com", {
            "state": "no_result", "stage": "content",
            "content_ok": "null",     # 曾经的 Bug 形状：字符串 "null"
            "content_len": "0",       # 数字位上是字符串
            "hit": "abc",
            "toc_complete": "yes",    # 布尔位上是别的字符串
            "rendered": "true",
        }) as st:
            out = self._list(st)
            item = out["items"][0]
            for key in ("jvm_content_ok", "jvm_content_len", "jvm_hit",
                        "jvm_toc_ok", "jvm_rendered"):
                self.assertIsNone(item[key], "%s 该降级成 None，实际 %r" % (key, item[key]))
            # 类型对的那几个照常显示：闸门只削不对的形状，不连坐
            self.assertEqual(item["jvm_state"], "no_result")
            self.assertEqual(item["jvm_stage"], "content")
            self.assertEqual(item["jvm_batch"], "20260920_000000")
            # **这条是核心**：出参校验通过（不过就是整页 500）
            page = SourcePage.model_validate(out)
            self.assertIsNone(page.items[0].jvm_content_ok)
            self.assertEqual(page.items[0].jvm_state, "no_result")

    def test_correct_types_are_kept(self) -> None:
        """反向用例：类型对的值一个都不许被这道闸削掉（否则「闸门」就成了「砍字段」）。"""
        with self._seed("https://good.com", {
            "state": "ok", "stage": "content", "hit": 12, "toc_count": 856,
            "toc_complete": True, "content_len": 4123, "content_ok": False,
            "rendered": True, "render_reason": "浏览器渲染后命中",
        }) as st:
            out = self._list(st)
            item = out["items"][0]
            self.assertEqual(item["jvm_hit"], 12)
            self.assertEqual(item["jvm_toc_count"], 856)
            self.assertIs(item["jvm_toc_ok"], True)
            self.assertEqual(item["jvm_content_len"], 4123)
            self.assertIs(item["jvm_content_ok"], False)
            self.assertIs(item["jvm_rendered"], True)
            self.assertEqual(item["jvm_render_reason"], "浏览器渲染后命中")
            page = SourcePage.model_validate(out)
            self.assertIs(page.items[0].jvm_content_ok, False)
            self.assertEqual(page.items[0].jvm_render_reason, "浏览器渲染后命中")


class ContentNoteTests(_ListFillCase):
    """正文偏短的附注（JVM 面）：**附注不改结论**，它只是把「这条通过可疑」说出来。

    判据与措辞都只有一份：阈值是 `core.quality.SHORT_CONTENT_CHARS`，话是
    `core.quality.short_content_note`。JVM 侧只报事实（`content_len` 是几），解释在
    这一侧派生——两个语言各写一句就会漂，而漂的正是用户照着做判断的那句。

    三件事必须分得开：短正文（带附注、结论照旧通过）、够长的正文（不带）、
    **没有内容**（`content_len == 0`：`content_ok` 与 `reason` 已经说清了，
    不许被说成「内容短」）。
    """

    def test_short_passing_content_gets_a_note_and_keeps_the_verdict(self) -> None:
        with self._seed("https://short.com", {
            "state": "ok", "stage": "content", "content_len": 32, "content_ok": True,
        }) as st:
            item = self._list(st)["items"][0]
        self.assertEqual(item["jvm_content_note"],
                         "正文较短（32 字符），建议看一眼全文确认不是错误页")
        # 附注**不改结论**：照样是通过
        self.assertIs(item["jvm_content_ok"], True)

    def test_content_at_or_above_the_threshold_has_no_note(self) -> None:
        for url, chars in (("https://boundary.com", SHORT_CONTENT_CHARS),
                           ("https://long.com", 4718)):
            with self._seed(url, {
                "state": "ok", "stage": "content", "content_len": chars, "content_ok": True,
            }) as st:
                item = self._list(st)["items"][0]
            self.assertEqual(item["jvm_content_note"], "",
                             "%d 字不该带附注（阈值 %d）" % (chars, SHORT_CONTENT_CHARS))

    def test_empty_content_is_not_reported_as_short(self) -> None:
        """`content_len == 0` 是**没有内容**（已由 content_ok/reason 说清），不是「内容短」。"""
        with self._seed("https://empty.com", {
            "state": "no_result", "stage": "content", "content_len": 0, "content_ok": False,
        }) as st:
            item = self._list(st)["items"][0]
        self.assertEqual(item["jvm_content_note"], "")


if __name__ == "__main__":
    unittest.main()
