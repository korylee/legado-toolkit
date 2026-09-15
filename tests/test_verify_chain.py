# -*- coding: utf-8 -*-
"""verify_chain 的证据结构与向后兼容性。"""

import unittest
from unittest.mock import patch

from core.verify import verify_chain

SEARCH_HTML = """
<div class="book-list">
  <li class="item"><h3><a href="/book/1">测试书</a></h3></li>
  <li class="item"><h3><a href="/book/2">第二本</a></h3></li>
</div>
"""
TOC_HTML = """
<div class="chapters">
  <a href="/read/1.html">第1章</a><a href="/read/2.html">第2章</a>
  <a href="/read/3.html">第3章</a>
</div>
"""
CONTENT_HTML = '<div id="content">' + "正文内容" * 50 + "</div>"

PAGES = {
    "https://site/search?q=%E6%88%91": SEARCH_HTML,
    "https://site/book/1": TOC_HTML,
    # `ruleBookInfo.tocUrl` 分支用它：必须与搜索结果里的详情链接**不同**，
    # 否则「分支有没有生效」区分不出来
    "https://site/toc-page": TOC_HTML,
    "https://site/read/1.html": CONTENT_HTML,
}

SOURCE = {
    "bookSourceUrl": "https://site",
    "bookSourceName": "测试源",
    "bookSourceType": 0,
    "searchUrl": "https://site/search?q={{key}}",
    "ruleSearch": {"bookList": "class.item", "name": "tag.a@text",
                   "bookUrl": "tag.a@href"},
    "ruleToc": {"chapterList": "class.chapters@tag.a", "chapterName": "tag.a@text",
                "chapterUrl": "tag.a@href"},
    "ruleContent": {"content": "id.content@text"},
}


def fake_fetch(url, timeout=15, headers=None, charset="", proxy=""):
    return PAGES.get(url, "")


def run_chain(source=None, **kw):
    with patch("core.verify.fetch", side_effect=fake_fetch):
        return verify_chain(source or dict(SOURCE), "我", **kw)


class StructureTests(unittest.TestCase):
    def test_steps_have_new_fields(self):
        r = run_chain()
        self.assertTrue(r["steps"])
        for s in r["steps"]:
            for key in ("name", "ok", "verdict", "has_notes", "notes", "values",
                        "evidence", "url", "page_id"):
                self.assertIn(key, s, "%s 缺字段 %s" % (s["name"], key))

    def test_pages_deduped(self):
        """搜索页被 search 与 bookUrl 两步共用，只应出现一次。"""
        r = run_chain()
        ids = [p["id"] for p in r["pages"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertIn("search", ids)

    def test_page_truncated_flag_present(self):
        r = run_chain()
        for p in r["pages"]:
            self.assertIn("truncated", p)
            self.assertIn("len", p)

    def test_content_values_are_full_text(self):
        """对齐 BookContent.kt:194-205：正文全文要能拿到。"""
        r = run_chain()
        content = [s for s in r["steps"] if s["name"] == "content"][0]
        self.assertTrue(content["values"])
        self.assertGreater(len(content["values"][0]), 100)


class CompatibilityTests(unittest.TestCase):
    def test_ok_only_false_on_fail(self):
        r = run_chain()
        for s in r["steps"]:
            self.assertEqual(s["ok"], s["verdict"] != "fail", s["name"])

    def test_all_ok_semantics(self):
        r = run_chain()
        self.assertEqual(r["all_ok"], all(s["ok"] for s in r["steps"]))

    def test_notes_do_not_break_all_ok(self):
        """有附注的 pass 仍算通过。"""
        r = run_chain()
        for s in r["steps"]:
            if s["verdict"] == "pass":
                self.assertTrue(s["ok"])


class TocUrlBranchTests(unittest.TestCase):
    def test_toc_url_skips_detail_page(self):
        """ruleBookInfo.tocUrl 非空时跳过详情页。对齐 Debug.kt:318-322。

        **tocUrl 必须与搜索结果里的详情链接不同**：若两者相同（比如都写
        "/book/1"），分支生效与否断言都成立，这条用例等于没测。同理
        page_id 恒为 "detail"，断言 pages 里有没有 "detail" 也是恒真的，
        要断言的是那个页面的 **url**。
        """
        src = dict(SOURCE)
        src["ruleSearch"] = dict(SOURCE["ruleSearch"])
        src["ruleBookInfo"] = {"tocUrl": "https://site/toc-page"}
        r = run_chain(src)
        toc = [s for s in r["steps"] if s["name"] == "toc"][0]
        self.assertEqual(toc["url"], "https://site/toc-page")
        detail = [p for p in r["pages"] if p["id"] == "detail"]
        self.assertEqual(len(detail), 1)
        self.assertEqual(detail[0]["url"], "https://site/toc-page")


class FileTypeTests(unittest.TestCase):
    def test_file_type_toc_is_unknown(self):
        src = dict(SOURCE)
        src["bookSourceType"] = 3
        r = run_chain(src)
        toc = [s for s in r["steps"] if s["name"] == "toc"][0]
        self.assertEqual(toc["verdict"], "unknown")
        self.assertTrue(toc["ok"])      # unknown 不是 fail


class DirtySourceTypeTests(unittest.TestCase):
    """bookSourceType 来自外部 JSON，脏值不能让试跑整体失败。

    裸 int() 会让 'abc' 抛 ValueError、[1] 抛 TypeError、inf 抛 OverflowError——
    而 rules.py 会把它变成 HTTP 400、ops.py 的任务会整体失败。
    """

    def test_dirty_types_do_not_raise(self):
        for bad in ("abc", "3.5", [1], {"a": 1}, float("inf"), None, "", []):
            with self.subTest(bad=repr(bad)):
                src = dict(SOURCE)
                src["bookSourceType"] = bad
                r = run_chain(src)          # 不抛即通过
                self.assertIn("steps", r)


if __name__ == "__main__":
    unittest.main()
