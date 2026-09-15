# -*- coding: utf-8 -*-
"""verify_chain 的证据结构、向后兼容性与失败链。

本文件里**每条关键用例都做过变异测试**（把被它守的那行代码改坏 → 必须变红）。
变异表见文件末尾：改坏了什么 → 哪条红了。没有这张表的用例，等于没有区分力。
"""

import unittest
from unittest.mock import patch

from core import quality as Q
from core.verify import _cap_evidence, verify_chain

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
#: 正文恰好 200 字符 —— test_content_values_are_full_text 钉的就是这个数
CONTENT_TEXT = "正文内容" * 50
CONTENT_HTML = '<div id="content">' + CONTENT_TEXT + "</div>"

SEARCH_URL = "https://site/search?q=%E6%88%91"

PAGES = {
    SEARCH_URL: SEARCH_HTML,
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


def source_with(**overrides):
    """按 key 覆盖 SOURCE，嵌套 dict 浅拷一层，避免用例之间互相污染。

    不直接改 SOURCE 的嵌套对象：一旦某条用例改了它，后面的用例会跟着变，
    而失败会出现在「凶手」之外的地方。
    """
    src = dict(SOURCE)
    for key, value in overrides.items():
        src[key] = dict(value) if isinstance(value, dict) else value
    return src


def step_of(result, name):
    return [s for s in result["steps"] if s["name"] == name][0]


class StructureTests(unittest.TestCase):
    def test_steps_have_new_fields(self):
        r = run_chain()
        self.assertTrue(r["steps"])
        for s in r["steps"]:
            for key in ("name", "ok", "verdict", "has_notes", "notes", "values",
                        "evidence", "url", "page_id"):
                self.assertIn(key, s, "%s 缺字段 %s" % (s["name"], key))

    def test_pages_have_one_entry_per_page_id(self):
        """happy path 恰好登记 3 页；搜索页被 search 与 bookUrl **引用**同一 id。

        旧写法断言 ``len(ids) == len(set(ids))`` 是结构性恒真的：pages 是 dict，
        ``list(values())`` 的 id 天然唯一，「同 id 二次登记」当前根本不可达。
        真正要守的是「搜索页只登记一次、两步共用同一个 page_id」。
        """
        r = run_chain()
        self.assertEqual(len(r["pages"]), 3)
        self.assertEqual(
            sorted(p["id"] for p in r["pages"]), ["chapter", "detail", "search"])
        self.assertEqual(step_of(r, "search")["page_id"], "search")
        self.assertEqual(step_of(r, "bookUrl")["page_id"], "search")

    def test_truncated_flag_reflects_page_size(self):
        """超过 MAX_PAGE_HTML_CHARS 才是 truncated=True。

        旧写法只查键存在，而 fixture 页面才 150 字符——``truncated`` 恒置 False
        也全绿，这个字段等于没人守。
        """
        huge = SEARCH_HTML + ("x" * (Q.MAX_PAGE_HTML_CHARS + 16))
        pages = dict(PAGES)
        pages[SEARCH_URL] = huge
        with patch("core.verify.fetch",
                   side_effect=lambda url, **kw: pages.get(url, "")):
            r = verify_chain(dict(SOURCE), "我")
        search_page = [p for p in r["pages"] if p["id"] == "search"][0]
        self.assertIs(search_page["truncated"], True)
        # 存的是截断后的内容，但 len 记的是**原始**长度
        self.assertEqual(len(search_page["html"]), Q.MAX_PAGE_HTML_CHARS)
        self.assertEqual(search_page["len"], len(huge))
        # 短页面必须仍是 False（否则就是恒置 True，与恒置 False 一样没意义）
        for p in r["pages"]:
            if p["id"] != "search":
                self.assertIs(p["truncated"], False)
                self.assertEqual(p["len"], len(p["html"]))

    def test_content_values_are_full_text(self):
        """对齐 BookContent.kt:194-205：正文全文要能拿到。

        阈值 ``> 100`` 太松：任何 ≥101 字符的截断都放得过去，而 fixture 恰好
        是 200 字符，所以这里钉死精确长度与内容。
        """
        r = run_chain()
        content = step_of(r, "content")
        self.assertTrue(content["values"])
        self.assertEqual(len(content["values"][0]), 200)
        self.assertEqual(content["values"][0], CONTENT_TEXT)


class CompatibilityTests(unittest.TestCase):
    def test_ok_only_false_on_fail(self):
        """``ok`` 只在 fail 时为 False —— 用**含 unknown 步**的链来钉。

        happy path 上 4 步全是 pass，``ok`` 与 ``verdict == "pass"`` 无法区分；
        真正能区分的是 unknown：下载源（type=3）的 toc / content 两步判 unknown，
        此时 ``ok`` 必须仍为 True，否则整条链会被误判成失败。
        """
        src = source_with(bookSourceType=3)
        r = run_chain(src)
        toc = step_of(r, "toc")
        self.assertEqual(toc["verdict"], Q.VERDICT_UNKNOWN)
        self.assertIs(toc["ok"], True)
        self.assertEqual(step_of(r, "content")["verdict"], Q.VERDICT_UNKNOWN)
        for s in r["steps"]:
            self.assertEqual(s["ok"], s["verdict"] != "fail", s["name"])
        # unknown 不算失败：链上没有任何 fail，all_ok 必须是 True
        self.assertTrue(all(s["ok"] for s in r["steps"]))
        self.assertIs(r["all_ok"], True)

    def test_all_ok_is_false_when_content_step_fails(self):
        """有 fail 步时 all_ok 必须为 False。

        旧写法 ``assertEqual(r["all_ok"], all(s["ok"] for s in r["steps"]))``
        就是实现自身的表达式，happy path 上恒真——把它改成 ``len(steps) > 0``
        全仓仍然全绿。而**内容步失败**是唯一「不早退仍返回」的失败路径，
        也是最常见的单点失败模式，``all_ok`` 又是三个消费方唯一的判据。
        """
        src = source_with(ruleContent={"content": "id.nonexistent@text"})
        r = run_chain(src)
        last = r["steps"][-1]
        self.assertEqual(last["name"], "content")
        self.assertEqual(last["verdict"], Q.VERDICT_FAIL)
        self.assertIs(last["ok"], False)
        self.assertIs(r["all_ok"], False)
        # 内容步失败不早退：前 3 步的证据必须都还在
        self.assertEqual(len(r["steps"]), 4)


class TocUrlBranchTests(unittest.TestCase):
    def test_toc_url_skips_detail_page(self):
        """ruleBookInfo.tocUrl 非空时跳过详情页。对齐 Debug.kt:318-322。

        **tocUrl 必须与搜索结果里的详情链接不同**：若两者相同（比如都写
        "/book/1"），分支生效与否断言都成立，这条用例等于没测。同理
        page_id 恒为 "detail"，断言 pages 里有没有 "detail" 也是恒真的，
        要断言的是那个页面的 **url**。
        """
        src = source_with(ruleBookInfo={"tocUrl": "https://site/toc-page"})
        r = run_chain(src)
        toc = step_of(r, "toc")
        self.assertEqual(toc["url"], "https://site/toc-page")
        detail = [p for p in r["pages"] if p["id"] == "detail"]
        self.assertEqual(len(detail), 1)
        self.assertEqual(detail[0]["url"], "https://site/toc-page")


class FileTypeTests(unittest.TestCase):
    def test_file_type_toc_is_unknown(self):
        src = source_with(bookSourceType=3)
        r = run_chain(src)
        toc = step_of(r, "toc")
        self.assertEqual(toc["verdict"], "unknown")
        self.assertTrue(toc["ok"])      # unknown 不是 fail


class EmptyRuleRegressionTests(unittest.TestCase):
    """A-1 回归防线：**列表规则为空 = 源的配置错误 → fail**，不是 unknown。

    `_extract` 曾对空规则返回 ``"空规则"`` 哨兵，被 ``judge_list_step`` 一律
    当成「规则回放不了」判 unknown → ``all_ok=True``。旧实现（
    ``ok1 = len(extract_all(html, "")) > 0``）判的是 fail，所以这是一次真回归：

      - ``services/add_source.py:279`` 会打印「🎉 全链路通过」
      - ``core/repair/loop.py:153`` 判它 ``already_ok`` →
        **AI 修复循环永远不会去修一个搜索规则为空的源**
    """

    def test_empty_book_list_rule_fails_search(self):
        src = source_with(ruleSearch={"bookList": "", "name": "tag.a@text",
                                      "bookUrl": "tag.a@href"})
        r = run_chain(src)
        self.assertEqual([s["name"] for s in r["steps"]], ["search"])
        search = step_of(r, "search")
        self.assertEqual(search["verdict"], Q.VERDICT_FAIL)
        self.assertIs(search["ok"], False)
        self.assertIs(r["all_ok"], False)
        self.assertIn("为空", search["detail"])
        # 哨兵若回来，rule_error 会变成 "空规则"（这才是本次回归的唯一入口）
        self.assertEqual(search["rule_error"], "")

    def test_empty_chapter_list_rule_fails_toc(self):
        src = source_with(ruleToc={"chapterList": "",
                                   "chapterUrl": "tag.a@href"})
        r = run_chain(src)
        # 搜索两步照常通过，失败停在 toc
        self.assertEqual([s["name"] for s in r["steps"]],
                         ["search", "bookUrl", "toc"])
        toc = step_of(r, "toc")
        self.assertEqual(toc["verdict"], Q.VERDICT_FAIL)
        self.assertIs(toc["ok"], False)
        self.assertIs(r["all_ok"], False)

    def test_unreplayable_rule_still_unknown_not_fail(self):
        """反向保护：JS 规则回放不了是**我们的能力边界**，仍须 unknown。

        空规则分支若被写成「只要 rule_error 非空就 fail」，这条会红。
        """
        src = source_with(ruleSearch={"bookList": "class.item@text@js:return 1"})
        with patch("core.verify.extract_all_nodes",
                   return_value=([], [], "JS 规则（@js）需要 Rhino 引擎，无法离线回放")):
            r = run_chain(src)
        search = step_of(r, "search")
        self.assertEqual(search["verdict"], Q.VERDICT_UNKNOWN)
        self.assertIs(search["ok"], True)
        self.assertIs(r["all_ok"], False)   # 链断在这里，但断的原因不是 fail


class EarlyExitShapeTests(unittest.TestCase):
    """早退路径的返回体形状必须完整——三个消费方读的就是这三个键。"""

    def test_search_extract_fail_early_exit(self):
        src = source_with(ruleSearch={"bookList": ""})
        r = run_chain(src)
        self.assertEqual(sorted(r.keys()), ["all_ok", "pages", "steps"])
        self.assertIsInstance(r["pages"], list)
        self.assertIs(r["all_ok"], False)

    def test_search_fetch_raises_early_exit(self):
        def boom(url, timeout=15, headers=None, charset="", proxy=""):
            raise OSError("连接被拒绝")

        with patch("core.verify.fetch", side_effect=boom):
            r = verify_chain(dict(SOURCE), "我")
        search = step_of(r, "search")
        self.assertEqual(search["verdict"], Q.VERDICT_FAIL)
        self.assertIn("抓取失败", search["detail"])
        self.assertIs(r["all_ok"], False)
        self.assertEqual(r["pages"], [])

    def test_discovery_mode_without_detail_url_early_exit(self):
        src = source_with(searchUrl="")
        r = run_chain(src)
        self.assertEqual([s["name"] for s in r["steps"]], ["search", "bookUrl"])
        self.assertEqual(step_of(r, "search")["verdict"], Q.VERDICT_UNKNOWN)
        self.assertIs(step_of(r, "bookUrl")["ok"], False)
        self.assertIs(r["all_ok"], False)


class FetchPassthroughTests(unittest.TestCase):
    """proxy / headers / charset 必须透传到 fetch。

    spec 明确要求的样板，改坏任意一个都等于「看源码改规则」失去地基
    （不带 header 抓回来的 HTML 是失真的）。
    """

    def test_headers_charset_proxy_reach_fetch(self):
        seen = []
        next_url = [u for u in (SEARCH_URL, "https://site/book/1",
                                "https://site/read/1.html")]

        def spy(url, timeout=15, headers=None, charset="", proxy=""):
            seen.append({"url": url, "headers": headers,
                         "charset": charset, "proxy": proxy})
            return PAGES.get(url, "")

        src = source_with(header='{"User-Agent":"UA/1.0"}', charset="gbk")
        with patch("core.verify.fetch", side_effect=spy):
            verify_chain(src, "我", proxy="http://127.0.0.1:7890")

        self.assertEqual([c["url"] for c in seen], next_url)
        for call in seen:
            self.assertEqual(call["headers"], {"User-Agent": "UA/1.0"})
            self.assertEqual(call["charset"], "gbk")
            self.assertEqual(call["proxy"], "http://127.0.0.1:7890")


class MisconfigNoteTests(unittest.TestCase):
    """静态错配附注必须**落进某一步的 notes**，失败链上也一样。

    A-2 之前附注只在函数**末尾**追加，而 6 处早退全在中途：`bookList` 为空时
    search 步 ``notes=[]``——恰恰在出问题时看不到那条提示。
    """

    @staticmethod
    def misconfigured(**overrides):
        # webJs 已配置但 URL 规则未开 webView → 在 Legado 中不生效
        overrides.setdefault("ruleContent",
                             {"content": "id.content@text", "webJs": "return 1"})
        return source_with(**overrides)

    def test_note_lands_on_search_step(self):
        r = run_chain(self.misconfigured())
        search = step_of(r, "search")
        self.assertTrue(any("webJs" in n for n in search["notes"]))
        self.assertIs(search["has_notes"], True)

    def test_note_survives_search_early_exit(self):
        """出问题时更要看得到——这正是 A-2 修的那件事。"""
        r = run_chain(self.misconfigured(ruleSearch={"bookList": ""}))
        self.assertEqual([s["name"] for s in r["steps"]], ["search"])
        self.assertTrue(any("webJs" in n for n in step_of(r, "search")["notes"]))

    def test_note_survives_fetch_exception_early_exit(self):
        def boom(url, timeout=15, headers=None, charset="", proxy=""):
            raise OSError("连接被拒绝")

        with patch("core.verify.fetch", side_effect=boom):
            r = verify_chain(self.misconfigured(), "我")
        self.assertTrue(any("webJs" in n for n in step_of(r, "search")["notes"]))

    def test_header_why_appears_exactly_once_in_discovery_mode(self):
        """发现模式曾把 header_why 拼进 skip_notes，收口后又由 _done() 追加一次
        ——结果是同一个原因写出**两份**。"""
        src = source_with(header="@js:return {}", searchUrl="")
        r = run_chain(src, detail_url="https://site/book/1")
        notes = step_of(r, "search")["notes"]
        self.assertEqual(len([n for n in notes if "header 含 JS" in n]), 1)

    def test_header_why_appears_exactly_once_in_search_mode(self):
        src = source_with(header="@js:return {}")
        r = run_chain(src)
        notes = step_of(r, "search")["notes"]
        self.assertEqual(len([n for n in notes if "header 含 JS" in n]), 1)


class CapEvidenceTests(unittest.TestCase):
    """证据总量兜底：values / matched_html / pages[].html **三处都要处理**。

    这是唯一一道与调用方无关的保险——ops.py 若将来改漏，它兜住，
    避免正文全文与整页 HTML 撑爆 job 的 result_json 与 SSE 流。
    """

    @staticmethod
    def _fixture():
        steps = [{"name": "search", "notes": [], "has_notes": False,
                  "values": ["v" * 50], "matched_html": "<p>x</p>"}]
        pages = {"p1": {"id": "p1", "url": "u", "html": "y" * 4096,
                        "len": 4096, "truncated": False}}
        return steps, pages

    def test_under_limit_is_untouched(self):
        steps, pages = self._fixture()
        _cap_evidence(steps, pages)
        self.assertEqual(steps[0]["values"], ["v" * 50])
        self.assertEqual(steps[0]["matched_html"], "<p>x</p>")
        self.assertEqual(len(pages["p1"]["html"]), 4096)
        self.assertIs(pages["p1"]["truncated"], False)
        self.assertEqual(steps[0]["notes"], [])

    def test_over_limit_truncates_all_three_places(self):
        steps, pages = self._fixture()
        with patch.object(Q, "MAX_EVIDENCE_TOTAL_CHARS", 10):
            _cap_evidence(steps, pages)
        self.assertEqual(steps[0]["values"], [])
        self.assertEqual(steps[0]["matched_html"], "")
        self.assertEqual(len(pages["p1"]["html"]), 1000)
        self.assertIs(pages["p1"]["truncated"], True)
        self.assertIs(steps[0]["has_notes"], True)
        self.assertIn("证据体积超出上限，已省略", steps[0]["notes"])

    def test_second_call_does_not_duplicate_note(self):
        steps, pages = self._fixture()
        with patch.object(Q, "MAX_EVIDENCE_TOTAL_CHARS", 10):
            _cap_evidence(steps, pages)
            _cap_evidence(steps, pages)
        self.assertEqual(
            steps[0]["notes"].count("证据体积超出上限，已省略"), 1)

    def test_cap_is_wired_into_the_return_path(self):
        """兜底必须真的接在出口上，否则它只是个可被直调的孤儿函数。

        搜索页放大到 5000 字符以上：`_cap_evidence` 的 ``p["html"][:1000]``
        只有在页面**本来就超过 1000** 时才看得出被截过，否则断言是恒真的。
        """
        pages = dict(PAGES)
        pages[SEARCH_URL] = SEARCH_HTML + ("x" * 5000)
        with patch.object(Q, "MAX_EVIDENCE_TOTAL_CHARS", 10):
            with patch("core.verify.fetch",
                       side_effect=lambda url, **kw: pages.get(url, "")):
                r = verify_chain(dict(SOURCE), "我")
        self.assertEqual(step_of(r, "content")["values"], [])
        self.assertEqual(step_of(r, "content")["matched_html"], "")
        search_page = [p for p in r["pages"] if p["id"] == "search"][0]
        self.assertEqual(len(search_page["html"]), 1000)
        self.assertEqual(search_page["len"], len(SEARCH_HTML) + 5000)
        for p in r["pages"]:
            self.assertIs(p["truncated"], True)


class DirtySourceTypeTests(unittest.TestCase):
    """bookSourceType 来自外部 JSON，脏值不能让试跑整体失败。

    裸 int() 会让 'abc' 抛 ValueError、[1] 抛 TypeError、inf 抛 OverflowError——
    而 rules.py 会把它变成 HTTP 400、ops.py 的任务会整体失败。
    """

    def test_dirty_types_do_not_raise(self):
        for bad in ("abc", "3.5", [1], {"a": 1}, float("inf"), None, "", []):
            with self.subTest(bad=repr(bad)):
                src = source_with(bookSourceType=bad)
                r = run_chain(src)          # 不抛即通过
                self.assertIn("steps", r)


# ---------------------------------------------------------------------------
# 变异表
# ---------------------------------------------------------------------------
# 方法：改坏源码的一行 → 跑**全量**用例 → 记录哪些用例变红。
# 全绿 = 该变异存活 = 没有用例守这件事。全部 28 条都已被抓住。
#
#  变异（改坏了什么）                                        | 专门守住它的用例
#  ----------------------------------------------------------|------------------
#  M1  Judgement.ok → `verdict == VERDICT_PASS`               | test_ok_only_false_on_fail；
#                                                             | test_quality.test_ok_only_fail_is_false
#  M2  _done() 的 all_ok → `len(steps) > 0`                   | test_all_ok_is_false_when_content_step_fails
#  M3  _done() 的 all_ok → `True`                             | 同 M2
#  M4  _new_page 的搜索页 id → "search2"                      | test_pages_have_one_entry_per_page_id
#  M5  bookUrl 步的 page_id → ""（不再共用）                  | test_pages_have_one_entry_per_page_id
#  M6  _new_page 的 truncated → False                         | test_truncated_flag_reflects_page_size
#  M7  _new_page 的 truncated → True                          | 同 M6（短页面那半段）
#  M8  _new_page 的 html[:MAX] → html[:100_000]               | 同 M6（len(html) 不符）
#  M9  _extract 把 values 截到 101 字符（模拟自创阈值）       | test_content_values_are_full_text
#  M10 删掉 judge_list_step 的空规则分支                      | test_quality.test_empty_rule_beats_stale_sentinel_rule_error
#  M11 _extract 恢复 `"空规则"` 哨兵                          | test_empty_book_list_rule_fails_search
#                                                             | （靠 rule_error == "" 那条断言）
#  M12 空规则分支挪到 rule_error 分支之后                     | test_quality.test_empty_rule_beats_stale_sentinel_rule_error
#  M13 删掉下载源豁免（= 排到空规则之后）                     | test_quality.ListStepEmptyRuleTests 多条
#  M14 空规则分支扩张成「rule_error 非空即 fail」             | test_unreplayable_rule_still_unknown_not_fail
#  M15 _fetch 去掉 proxy=proxy                                | test_headers_charset_proxy_reach_fetch
#  M16 _fetch 的 charset 写死成 ""                            | 同 M15
#  M17 _fetch 的 headers 写死成 None                          | 同 M15
#  M18 _done() 里 `steps[:] = _cap_evidence(...)` 删掉        | test_cap_is_wired_into_the_return_path
#  M19 _cap_evidence 去掉 `s["values"] = []`                  | test_over_limit_truncates_all_three_places
#  M20 _cap_evidence 去掉 `p["html"] = p["html"][:1000]`      | test_over_limit_truncates_all_three_places
#  M21 _cap_evidence 去掉 notes 去重判断                      | test_second_call_does_not_duplicate_note
#  M22 _done() 的附注循环整段删掉                             | MisconfigNoteTests 全 5 条
#  M23 附注改挂 `s["name"] == "content"`                      | MisconfigNoteTests 全 5 条
#  M24 恢复发现模式的 skip_notes.append(header_why)           | test_header_why_appears_exactly_once_in_discovery_mode
#  M25 把附注块从 _done() 挪回**函数末尾**（A-2 之前的写法）  | test_note_survives_search_early_exit
#                                                             | test_note_survives_fetch_exception_early_exit
#  M26 早退的 return _done() 改回手写字典（丢掉 pages）       | EarlyExitShapeTests.test_search_extract_fail_early_exit
#  M27 searchUrl 取空时不再走发现模式                         | test_discovery_mode_without_detail_url_early_exit
#  M28 空列表规则 + 旧哨兵（rule="" 且 rule_error="空规则"）  | test_quality.test_empty_rule_beats_stale_sentinel_rule_error
#
# 对照组（证明被替换掉的 6 条旧断言确实没有区分力）：
# 把旧用例的**原样**放回来，在同样的变异下单独跑——
#   M1（ok 映射）、M2（all_ok 恒真）、M6（truncated 恒 False）、
#   M9（values 截到 101）、M15（proxy 不透传）、
#   M19（cap 漏 values）、M22 / M23（附注接线）  → 旧用例**全绿**
# 只有 M4 被旧用例的 `assertIn("search", ids)` 抓住，但它的另一半
# `len(ids) == len(set(ids))` 是结构性恒真的（pages 是 dict）。
#
# 两条自我更正（记下来给后来人）：
#  - 本文件早期版本的 test_cap_is_wired_into_the_return_path 断言
#    `len(p["html"]) == 1000`，但 fixture 页面只有 150 字符——该断言恒假，
#    是新写的假测试。已改成用 5000+ 字符的搜索页，截断才看得出来。
#  - 断言「A 步的附注落在 A 步」时，必须选**只有 A 步**或「A 步必然存在」的
#    链，否则附注落在哪一步取决于失败位置（正是 A-2 要消灭的那种不确定）。

if __name__ == "__main__":
    unittest.main()
