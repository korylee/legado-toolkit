# -*- coding: utf-8 -*-
"""JVM 结论的读取规则：**深者胜、同深取新**（S3-3）。

守的是「同一源跑了两次不同深度的批次之后，界面该显示哪一条」：

- 用户为了改个设置只重跑了几条 `depth=search` 的快筛 → 不该把上一批
  目录/正文级的结论盖掉（界面上从「目录 856 章」退回「搜索✓」，而用户什么都没删）
- 反过来，更深的批次覆盖更浅的永远是对的——更深的结论包含更浅的全部信息

这条规则**只有一份实现**（`Store.latest_jvm_conclusions`），`/api/jvm/results`
与列表回填都调它；两处各写一次就会漂（lessons §二十三 已栽过两次）。
"""

from __future__ import annotations

import json
import os
import shutil
import unittest
import uuid

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


if __name__ == "__main__":
    unittest.main()
