# -*- coding: utf-8 -*-
"""连 App 调试：事件流 → steps[] 的聚合，以及 WS 客户端。

**不连真 App**：
  - 聚合逻辑做成纯函数 ``build_steps``，直接喂事件文本（本文件大半用例）
  - WS 客户端用一个**标准库手写的假服务端**（只做握手 + 推文本帧 + close），
    钉住「请求体长什么样」「事件怎么收」「分帧/ping/close 怎么处理」

样本取自实测抓下来的真实事件流（含 ``[mm:ss.SSS]`` 前缀）。
"""

import json
import socket
import struct
import threading
import unittest
from unittest.mock import patch

from core import quality as Q
from core.app_debug import (
    MAX_MATCHED_CHARS, MAX_PAGES, PAGE_IDS, build_steps, collect_debug_events,
    fetch_debug_pages, matched_map, run_app_debug,
)
from core.fetch import CacheMiss, Fetched

#: 实测样本：关键字模式，走完 搜索 → 详情 → 目录 → 正文
SAMPLE = [
    "[00:00.120]⇒开始搜索关键字:我",
    "[00:00.180]︾开始解析搜索页",
    "[00:00.640]≡获取成功:https://www.52shuku.net/so/search.php?q=我",
    "[00:00.660]┌获取书籍列表",
    "[00:00.700]└列表大小:20",
    "[00:00.720]┌获取书名",
    "[00:00.740]└孤悬_虞渊【完结+番外】",
    "[00:00.760]◇书籍总数:20",
    "[00:00.780]︽搜索页解析完成",
    "[00:00.800]︾开始解析详情页",
    "[00:01.100]≡获取成功:https://www.52shuku.net/bjUIK.html",
    "[00:01.120]┌获取书名",
    "[00:01.140]└孤悬_虞渊【完结+番外】",
    "[00:01.160]︽详情页解析完成",
    "[00:01.200]︾开始解析目录页",
    "[00:01.900]└列表大小:108",
    "[00:01.920]◇目录总数:108",
    "[00:01.940]︽目录页解析完成",
    "[00:02.000]︾开始解析正文页",
    "[00:02.900]≡获取成功:https://www.52shuku.net/bjUIK_2.html",
    "[00:02.920]┌获取正文下一页链接",
    "[00:02.940]└https://www.52shuku.net/bjUIK_3.html",
    "[00:02.960]◇本章总页数:1",
    "[00:02.980]┌获取章节名称",
    "[00:03.000]└第1页",
    "[00:03.020]┌获取正文内容",
    "[00:03.040]└\n　　《孤悬》作者：虞渊",
    "[00:03.060]︽正文页解析完成",
]

#: 发现模式的样本（key = ``发现::<URL>``，如 ``发现::http://m.qudushu.com/sort/1/1.html``）。
#: 事件序列依据 Legado ``Debug.kt:246-250``（``⇒开始访问发现页``）与 ``:281-293``
#: （``︾开始解析发现页`` / ``≡获取成功`` / ``└列表大小`` / ``︽发现页解析完成``）：
#: 发现页解析完取第 0 条，继续走 详情页 → 目录页 → 正文页。
EXPLORE_URL = "http://m.qudushu.com/sort/1/1.html"
EXPLORE_SAMPLE = [
    "[00:00.100]⇒开始访问发现页:http://m.qudushu.com/sort/1/1.html",
    "[00:00.200]︾开始解析发现页",
    "[00:00.900]≡获取成功:http://m.qudushu.com/sort/1/1.html",
    "[00:00.950]┌获取书籍列表",
    "[00:01.000]└列表大小:20",
    "[00:01.050]︽发现页解析完成",
    "[00:01.100]︾开始解析详情页",
    "[00:01.500]≡获取成功:https://www.52shuku.net/bjUIK.html",
    "[00:01.520]┌获取书名",
    "[00:01.540]└孤悬_虞渊【完结+番外】",
    "[00:01.560]︽详情页解析完成",
    "[00:01.600]︾开始解析目录页",
    "[00:02.100]◇目录总数:108",
    "[00:02.120]︽目录页解析完成",
    "[00:02.200]︾开始解析正文页",
    "[00:02.900]≡获取成功:https://www.52shuku.net/bjUIK_2.html",
    "[00:02.920]┌获取正文内容",
    "[00:02.940]└\n　　正文全文",
    "[00:02.960]︽正文页解析完成",
]

#: 服务端推来的正文全文（断言 values 保留全文、不被截断）
CONTENT_URL = "https://www.52shuku.net/bjUIK_2.html"
TOC_URL = "https://www.52shuku.net/bjUIK.html"
SEARCH_URL = "https://www.52shuku.net/so/search.php?q=我"

PAGES = {
    SEARCH_URL: "<html>搜索页</html>",
    TOC_URL: "<html>详情页</html>",
    CONTENT_URL: "<html>正文页</html>",
    EXPLORE_URL: "<html>发现页</html>",
}


def fake_fetch(url, timeout=15, headers=None, charset="", proxy="", source=None,
               cache="auto", method="GET", body=""):
    # 返回 Fetched 而不是字符串：补抓现在要拿「刚抓的还是缓存里的」填 pages[]
    return Fetched(PAGES.get(url, ""), False, "")



#: 正文规则为空的形态（`WebBook.getContentAwait`：规则空 → 只打这一行就返回
#: 章节链接，**不发请求**）：目录段有 URL，正文段没有 `≡获取成功`。
EMPTY_CONTENT_TOC_URL = "https://a.com/book/1/"
EMPTY_CONTENT_SAMPLE = [
    "[00:00.100]⇒开始访目录页:https://a.com/book/1/",
    "[00:00.200]︾开始解析目录页",
    "[00:00.500]≡获取成功:https://a.com/book/1/",
    "[00:00.700]◇目录总数:10",
    "[00:00.900]︽目录页解析完成",
    "[00:01.000]︾开始解析正文页",
    "[00:01.100]⇒正文规则为空,使用章节链接:/book/1/c1.html",
    "[00:01.200]︽正文页解析完成",
]

EMPTY_CONTENT_CHAPTER_URL = "https://a.com/book/1/c1.html"

class MatchedHtmlTests(unittest.TestCase):
    """第三期 `matched_html` 回填（TODO §一点八）：**按每段自己的 url + 段名取**。

    引擎那侧只记 `{url: {step: html}}`（Kotlin 不认"段"这个语义，分段逻辑只有一份、
    在 Python 这边），所以这两条钉的是：填对了段、以及**段名对不上的不许乱填**。
    """

    #: SAMPLE 里搜索段与目录段各自的页面（见文件头的夹具）
    SEARCH_URL = "https://www.52shuku.net/so/search.php?q=我"
    TOC_URL = "https://www.52shuku.net/bjUIK.html"

    def test_fills_the_segment_that_owns_that_url_and_step(self):
        steps = build_steps(SAMPLE, matched={self.SEARCH_URL: {"search": "<div class='so'></div>"}})
        self.assertEqual(step_of(steps, "search")["matched_html"], "<div class='so'></div>")
        self.assertEqual(step_of(steps, "toc")["matched_html"], "", "别的段不许被填")

    def test_step_name_must_match_the_segment(self):
        """同一页可能既是详情页又是目录页（PAGE_IDS 里两者共用 detail）：段名对不上就不填。

        乱填的后果不是"少显示"，而是**把另一段的 DOM 当成这一段命中给用户看**——
        照着它改规则会改错地方（与 §六十三 那类"看着正常、答的不是你问的"同族）。
        """
        steps = build_steps(SAMPLE, matched={self.TOC_URL: {"content": "<p>正文</p>"}})
        self.assertEqual(step_of(steps, "toc")["matched_html"], "")

    def test_bad_shape_is_dropped_whole(self):
        """形状不对整块丢掉**并留日志**（AGENTS #22：跨语言字段要在入口有显式闸门）。

        它只是抽屉里的一块证据，不该把整个结果带走；也不该静默——静默的后果是
        「看规则命中了什么」永远空白而没人知道为什么。
        """
        with patch("sys.stderr") as err:
            self.assertEqual(matched_map("nope"), {})
            self.assertEqual(matched_map([{"a": "b"}]), {})
            self.assertEqual(matched_map(None), {})
            self.assertIn("形状不对", err.write.call_args_list[0][0][0])
        # 半个形状也不行：url 对、值不是 dict → 丢掉那一条
        self.assertEqual(matched_map({"u": "not-a-dict"}), {})

    def test_over_long_html_is_truncated_with_a_note(self):
        got = matched_map({"u": {"s": "a" * (MAX_MATCHED_CHARS + 5)}})["u"]["s"]
        self.assertLess(len(got), MAX_MATCHED_CHARS + 100)
        self.assertIn("已截断", got)

    def test_blank_html_is_dropped(self):
        self.assertEqual(matched_map({"u": {"s": "   ", "t": "<i></i>"}}), {"u": {"t": "<i></i>"}})


def step_of(steps, name):
    return next(s for s in steps if s["name"] == name)


# ------------------------------------------------------------------ 分段

class TestSegmenting(unittest.TestCase):
    def test_sample_splits_into_four_steps_in_order(self):
        """实测样本 → 四段，顺序与阅读路径一致。"""
        steps = build_steps(SAMPLE)
        self.assertEqual([s["name"] for s in steps],
                         ["search", "bookUrl", "toc", "content"])

    def test_verdicts_follow_done_markers(self):
        """有 ︽X页解析完成 → pass（样本里四段都跑完了）。"""
        steps = build_steps(SAMPLE)
        self.assertEqual([s["verdict"] for s in steps],
                         ["pass", "pass", "pass", "pass"])
        self.assertTrue(all(s["ok"] for s in steps))

    def test_prefix_is_stripped_from_values(self):
        """values 去掉 [mm:ss.SSS] 前缀，但保留 ┌/└ 成对结构与原文。"""
        search = step_of(build_steps(SAMPLE), "search")
        # values[0] 是入口事件（⇒开始搜索关键字），段起始行排在其后
        self.assertEqual(search["values"][1], "︾开始解析搜索页")
        self.assertIn("┌获取书籍列表", search["values"])
        self.assertIn("└列表大小:20", search["values"])
        self.assertIn("︽搜索页解析完成", search["values"])
        self.assertFalse([v for v in search["values"] if v.startswith("[00:")])

    def test_entry_event_before_first_segment_joins_first_step(self):
        """首个 ︾ 之前的 ⇒开始搜索关键字 归入第一段（它描述的就是入口）。"""
        search = step_of(build_steps(SAMPLE), "search")
        self.assertEqual(search["values"][0], "⇒开始搜索关键字:我")

    def test_url_comes_from_fetch_success_line(self):
        steps = build_steps(SAMPLE)
        self.assertEqual(step_of(steps, "search")["url"], SEARCH_URL)
        self.assertEqual(step_of(steps, "bookUrl")["url"], TOC_URL)
        self.assertEqual(step_of(steps, "content")["url"], CONTENT_URL)
        # 目录段没有 ≡ 行 → 空串（不是 None）
        self.assertEqual(step_of(steps, "toc")["url"], "")

    def test_notes_carry_stat_lines(self):
        steps = build_steps(SAMPLE)
        self.assertEqual(step_of(steps, "search")["notes"], ["◇书籍总数:20"])
        self.assertEqual(step_of(steps, "toc")["notes"], ["◇目录总数:108"])
        self.assertEqual(step_of(steps, "content")["notes"], ["◇本章总页数:1"])
        self.assertTrue(step_of(steps, "toc")["has_notes"])
        # 没有 ◇ 的段 notes 为空，不该被塞进空串
        self.assertEqual(step_of(steps, "bookUrl")["notes"], [])
        self.assertFalse(step_of(steps, "bookUrl")["has_notes"])

    def test_page_ids_match_drawer_pages(self):
        """page_id 用段名映射出的页 id（search/detail/chapter），抽屉据此对页面。"""
        steps = build_steps(SAMPLE)
        self.assertEqual(step_of(steps, "search")["page_id"], "search")
        self.assertEqual(step_of(steps, "bookUrl")["page_id"], "detail")
        self.assertEqual(step_of(steps, "toc")["page_id"], "detail")   # 目录页即详情页
        self.assertEqual(step_of(steps, "content")["page_id"], "chapter")

    def test_matched_html_is_always_empty(self):
        """App 只推文本，不给 HTML——这个字段恒为空串（抽屉据此显示空状态）。"""
        for s in build_steps(SAMPLE):
            self.assertEqual(s["matched_html"], "")

    def test_segment_without_done_marker_is_unknown(self):
        """段的解析没跑完（缺 ︽X页解析完成）→ unknown，不是 fail。"""
        events = SAMPLE[:5]        # 切在搜索页中间
        search = step_of(build_steps(events), "search")
        self.assertEqual(search["verdict"], "unknown")
        self.assertTrue(search["ok"])          # unknown 仍算 ok（verify 的旧语义）
        self.assertEqual(search["reason"], "该段没有解析完成信号")


# ------------------------------------------------------------------ 发现段

class TestExploreSegment(unittest.TestCase):
    """key = ``发现::<URL>`` 的链路：发现页 → 详情页 → 目录页 → 正文页。

    发现是 App 调试的 5 个目标之一（``Debug.kt:246-250/281-293``）。我们这边
    原本只认 4 个段名，``︾开始解析发现页`` 会被当成「不是分段信号」而并进上一段
    ——整条链的段名与页 id 就全错位了，所以这一组用例盯的是段名与页 id。
    """

    def test_explore_sample_splits_into_four_steps_in_order(self):
        steps = build_steps(EXPLORE_SAMPLE)
        self.assertEqual([s["name"] for s in steps],
                         ["explore", "bookUrl", "toc", "content"])
        self.assertEqual([s["verdict"] for s in steps], ["pass"] * 4)

    def test_explore_step_keeps_its_own_url_and_entry_event(self):
        explore = step_of(build_steps(EXPLORE_SAMPLE), "explore")
        self.assertEqual(explore["url"], EXPLORE_URL)
        # 入口事件（⇒开始访问发现页）归入发现段
        self.assertEqual(explore["values"][0],
                         "⇒开始访问发现页:http://m.qudushu.com/sort/1/1.html")
        self.assertEqual(explore["values"][1], "︾开始解析发现页")
        self.assertIn("└列表大小:20", explore["values"])
        self.assertIn("︽发现页解析完成", explore["values"])

    def test_explore_has_its_own_page_id_not_detail(self):
        """**发现页与详情页是不同的 URL，绝不能共用一个 page_id**。

        共用的话 ``quality.new_page`` 的「先到先得」会让先抓到的发现页 HTML
        顶掉详情页（或反过来），抽屉里两页都失真。这条用例同时钉住「不与
        detail 共用」和「映射就是 explore 本身」两层。
        """
        self.assertEqual(PAGE_IDS["explore"], "explore")
        self.assertNotEqual(PAGE_IDS["explore"], PAGE_IDS["bookUrl"])
        steps = build_steps(EXPLORE_SAMPLE)
        self.assertEqual(step_of(steps, "explore")["page_id"], "explore")
        self.assertEqual(step_of(steps, "bookUrl")["page_id"], "detail")
        self.assertEqual(step_of(steps, "toc")["page_id"], "detail")
        self.assertEqual(step_of(steps, "content")["page_id"], "chapter")
        # 发现段不会去挤详情段：这一段链上页面 id 恰好是三个
        self.assertEqual({s["page_id"] for s in steps},
                         {"explore", "detail", "chapter"})

    def test_explore_entry_event_alone_still_names_the_segment(self):
        """连 ︾/︽ 都没推时，凭 ``⇒开始访问发现页`` 也能定段名。

        注意是「访问发现页」而不是「访目录页」——正则里 ``问`` 必须可选，
        否则这个兜底会把发现段错标成 content。
        """
        steps = build_steps(["⇒开始访问发现页:http://a.com/sort/1.html",
                             "└列表大小:20"])
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["name"], "explore")
        self.assertEqual(steps[0]["page_id"], "explore")


# ------------------------------------------------------------------ 单段输入

class TestSingleSegment(unittest.TestCase):
    def test_only_content_segment(self):
        """--URL 从正文页开始：只有一个正文段。"""
        events = [
            "[00:00.100]⇒开始访正文页:https://a.com/1.html",
            "[00:00.200]︾开始解析正文页",
            "[00:01.000]≡获取成功:https://a.com/1.html",
            "[00:01.100]┌获取正文内容",
            "[00:01.200]└正文全文",
            "[00:01.300]︽正文页解析完成",
        ]
        steps = build_steps(events)
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["name"], "content")
        self.assertEqual(steps[0]["verdict"], "pass")
        self.assertEqual(steps[0]["url"], "https://a.com/1.html")
        self.assertEqual(steps[0]["page_id"], "chapter")
        # 入口事件归入这一段
        self.assertIn("⇒开始访正文页:https://a.com/1.html", steps[0]["values"])

    def test_only_toc_segment(self):
        """++URL 从目录页开始：只有一个目录段。"""
        events = [
            "[00:00.100]⇒开始访目录页:https://a.com/book/1/",
            "[00:00.200]︾开始解析目录页",
            "[00:01.000]≡获取成功:https://a.com/book/1/",
            "[00:01.100]◇目录总数:2",
            "[00:01.200]︽目录页解析完成",
        ]
        steps = build_steps(events)
        self.assertEqual([s["name"] for s in steps], ["toc"])
        self.assertEqual(steps[0]["page_id"], "detail")
        self.assertEqual(steps[0]["notes"], ["◇目录总数:2"])

    def test_no_segment_marker_falls_back_to_one_step(self):
        """连 ︾/︽ 都没有时全部归一段，段名从入口事件推断。"""
        events = ["⇒开始访目录页:https://a.com/book/1/", "└列表大小:9"]
        steps = build_steps(events)
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["name"], "toc")
        self.assertEqual(steps[0]["verdict"], "unknown")
        self.assertEqual(steps[0]["values"], events)

    def test_no_marker_and_no_entry_defaults_to_content(self):
        """什么都推断不出来时按 content 兜底，而不是丢弃事件。"""
        steps = build_steps(["└不知道是什么", "另一行"])
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["name"], "content")
        self.assertEqual(steps[0]["page_id"], "chapter")


# ------------------------------------------------------------------ 空 / 错误

class TestEmptyAndError(unittest.TestCase):
    def test_empty_events_produce_no_steps(self):
        """空事件流不产段——不能凭空造一个空步（抽屉会显示假的正文步）。"""
        self.assertEqual(build_steps([]), [])
        self.assertEqual(build_steps(None), [])

    def test_error_line_makes_verdict_fail(self):
        """段内出现错误行 → fail，并把错误行当 reason。"""
        events = [
            "︾开始解析搜索页",
            "java.lang.NullPointerException: Cannot invoke ...",
        ]
        search = build_steps(events)[0]
        self.assertEqual(search["verdict"], "fail")
        self.assertFalse(search["ok"])
        self.assertIn("NullPointerException", search["reason"])
        self.assertEqual(search["page_id"], "search")

    def test_error_after_done_marker_still_fails(self):
        """错误行与完成信号同时存在时，错的是错的（fail 优先）。"""
        events = ["︾开始解析正文页", "︽正文页解析完成", "错误：内容为空"]
        self.assertEqual(build_steps(events)[0]["verdict"], "fail")

    def test_app_own_exception_counts_as_error(self):
        """**App 自己的异常也要认**：``io.legado.app.exception.*``。

        实测 2026-09-20（S5-A1）：正文段真抛了 ``ContentEmptyException``，而
        ``ERROR_PREFIXES`` 当时只认 ``java.``/``javax.``——段于是判成 unknown，
        把「真出错」读成「我们没测出来」。这条钉住它。
        """
        events = [
            "︾开始解析正文页",
            "io.legado.app.exception.ContentEmptyException: 内容为空",
        ]
        content = build_steps(events)[0]
        self.assertEqual(content["verdict"], "fail")
        self.assertIn("ContentEmptyException", content["reason"])

    def test_error_line_with_time_prefix(self):
        """事件带 ``[mm:ss.SSS]`` 前缀时，错误判定要**剥完前缀再判**。

        连 App 与我们自己的 JVM 通道（S5-A1）都是这个形态：`Debug.log` 把时间前缀
        加在 message 上。前缀没剥就 startswith，异常行会被读成正常。
        """
        events = [
            "︾开始解析正文页",
            "[00:15.708] io.legado.app.exception.ContentEmptyException: 内容为空",
        ]
        self.assertEqual(build_steps(events)[0]["verdict"], "fail")

    def test_reason_is_truncated_to_200_chars(self):
        """失败原因只取前 200 字符（完整错误行仍在 values 里）。"""
        long_error = "错误：" + "x" * 500
        step = build_steps(["︾开始解析正文页", long_error])[0]
        self.assertEqual(len(step["reason"]), 200)

    def test_failure_word_inside_content_is_not_an_error(self):
        """正文里出现「失败」二字**不能**判 fail——正文以 └ 开头，不匹配行首。"""
        events = [
            "︾开始解析正文页",
            "└他这一次的尝试失败了，但故事还长。",
            "︽正文页解析完成",
        ]
        step = build_steps(events)[0]
        self.assertEqual(step["verdict"], "pass")
        self.assertEqual(step["reason"], "")

    def test_short_content_is_kept_in_full(self):
        """正文全文照收，不做任何长度截断（与 verify_chain 的正文口径一致）。"""
        long_text = "正文" * 1500        # 3000 字符
        step = build_steps(["︾开始解析正文页", "└" + long_text,
                            "︽正文页解析完成"])[0]
        self.assertIn("└" + long_text, step["values"])
        self.assertEqual(step["evidence"]["chars"] > 3000, True)


# ------------------------------------------------------------------ 形状

class TestStepShape(unittest.TestCase):
    def test_steps_have_same_keys_as_verify_chain(self):
        """与 verify_chain 的 steps[] 同形——抽屉与卡片读的就是这些键。"""
        step = build_steps(SAMPLE)[0]
        for key in ("name", "ok", "detail", "verdict", "has_notes", "notes",
                    "reason", "shape", "evidence", "rule_error", "url",
                    "page_id", "values", "matched_html"):
            self.assertIn(key, step)
        # 抽屉无条件读 evidence.values_total / chars，缺了会直接报错
        self.assertIn("values_total", step["evidence"])
        self.assertIn("chars", step["evidence"])

    def test_detail_prefers_stat_line_over_event_count(self):
        """卡片上那行小字：有 ◇ 统计时用它（比「9 条事件」有信息量）。"""
        self.assertEqual(step_of(build_steps(SAMPLE), "toc")["detail"], "◇目录总数:108")

    def test_all_ok_only_false_on_fail(self):
        """unknown 不是坏（工具测不了），不该让 all_ok 变 False。"""
        ok_steps = build_steps(["︾开始解析正文页"])           # 没有完成信号
        self.assertEqual(ok_steps[0]["verdict"], "unknown")
        self.assertEqual(all(s["ok"] for s in ok_steps), True)


# ------------------------------------------------------------------ 抓页面

class TestFetchPages(unittest.TestCase):
    def _steps(self):
        return build_steps(SAMPLE)

    def test_pages_have_verify_chain_shape(self):
        steps = self._steps()
        with patch("core.app_debug.fetch_ex", side_effect=fake_fetch):
            pages = fetch_debug_pages(steps)
        self.assertEqual([p["id"] for p in pages], ["search", "detail", "chapter"])
        self.assertEqual(len(pages), MAX_PAGES)
        for p in pages:
            for key in ("id", "url", "status", "charset", "html", "len", "truncated",
                        "fetched_at", "cached"):
                self.assertIn(key, p)
        self.assertEqual(pages[1]["url"], TOC_URL)
        self.assertEqual(pages[2]["html"], "<html>正文页</html>")

    def test_pages_use_source_header_and_charset(self):
        """与 verify.py 一致：必须带书源自己的 header / charset 抓。"""
        steps = self._steps()
        seen = {}

        def spy(url, timeout=15, headers=None, charset="", proxy="", source=None,
                   cache="auto", method="GET", body=""):
            seen["headers"] = headers
            seen["charset"] = charset
            seen["source"] = source
            return Fetched("<html>x</html>", False, "")

        src = {"header": '{"Referer":"https://a.com/"}', "charset": "gbk"}
        with patch("core.app_debug.fetch_ex", side_effect=spy):
            fetch_debug_pages(steps, src)
        self.assertEqual(seen["headers"], {"Referer": "https://a.com/"})
        self.assertEqual(seen["charset"], "gbk")
        # source 也要透传：补抓最多 3 页，同样该遵守源声明的 concurrentRate
        self.assertIs(seen["source"], src)

    def test_fetch_failure_is_noted_not_raised(self):
        """抓不到页面：该页不进 pages，在对应 step 的 notes 里说明，判定不受影响。"""
        def boom(url, timeout=15, headers=None, charset="", proxy="", source=None,
                   cache="auto", method="GET", body=""):
            raise OSError("连接超时")

        steps = self._steps()
        with patch("core.app_debug.fetch_ex", side_effect=boom):
            pages = fetch_debug_pages(steps)
        self.assertEqual(pages, [])
        search = step_of(steps, "search")
        self.assertTrue(search["has_notes"])
        self.assertIn("页面抓取失败", search["notes"][-1])
        self.assertIn("连接超时", search["notes"][-1])
        self.assertEqual(search["verdict"], "pass")     # 判定没被牵连

    def test_empty_html_is_not_registered(self):
        """抓回空 HTML 不登记（new_page 的口径），但不报错。"""
        with patch("core.app_debug.fetch_ex", side_effect=lambda *a, **k: Fetched("", False, "")):
            self.assertEqual(fetch_debug_pages(self._steps()), [])

    def test_explore_chain_keeps_four_steps_to_three_pages(self):
        """发现链路（4 个段）仍然只登记 3 页，且**正文页不会被 MAX_PAGES 挤掉**。

        这是 MAX_PAGES == 3 在新链路下的实测依据：发现模式没有搜索段，
        页 id 是 发现/详情/正文 三个（目录页与详情页共用 detail），而
        ``fetch_debug_pages`` 的上限判断在循环开头——少算一个就会丢掉排在最后、
        也最不该丢的正文页。所以「4 个段 → 3 个 id」这件事必须有用例守着。
        """
        steps = build_steps(EXPLORE_SAMPLE)
        with patch("core.app_debug.fetch_ex", side_effect=fake_fetch):
            pages = fetch_debug_pages(steps)
        self.assertEqual([p["id"] for p in pages], ["explore", "detail", "chapter"])
        self.assertEqual(len(pages), MAX_PAGES)
        # 发现页与详情页各自拿到自己的 HTML（没有互相覆盖）
        self.assertEqual(pages[0]["url"], EXPLORE_URL)
        self.assertEqual(pages[0]["html"], "<html>发现页</html>")
        self.assertEqual(pages[1]["url"], TOC_URL)
        self.assertEqual(pages[2]["html"], "<html>正文页</html>")
        self.assertEqual(step_of(steps, "content")["url"], CONTENT_URL)

    def test_cache_mode_reaches_fetch(self):
        """接线验证：只断言签名的话，把 ``cache=cache`` 那行删掉也照样全绿。"""
        seen = {}

        def spy(url, timeout=15, headers=None, charset="", proxy="", source=None,
                cache="auto", method="GET", body=""):
            seen["cache"] = cache
            return Fetched("<html>x</html>", False, "")

        with patch("core.app_debug.fetch_ex", side_effect=spy):
            fetch_debug_pages(self._steps(), cache="only")
        self.assertEqual(seen["cache"], "only")

    def test_cache_miss_is_noted_as_such(self):
        """「我们没去抓」≠「抓不到」：notes 要说清是只读模式，不能报成抓取失败。

        混成一句「页面抓取失败」，用户会去查站点——而问题出在他自己刚选的那档。
        """
        def miss(url, timeout=15, headers=None, charset="", proxy="", source=None,
                 cache="auto", method="GET", body=""):
            raise CacheMiss("这一页没有缓存")

        steps = self._steps()
        with patch("core.app_debug.fetch_ex", side_effect=miss):
            pages = fetch_debug_pages(steps, cache="only")
        self.assertEqual(pages, [])
        note = step_of(steps, "search")["notes"][-1]
        self.assertIn("只读缓存", note)
        self.assertNotIn("抓取失败", note)

    def test_pages_carry_the_html_source(self):
        """命中缓存时页面要带上「抓取时刻 + 来自缓存」——抽屉据此标出来。"""
        def hit(url, timeout=15, headers=None, charset="", proxy="", source=None,
                cache="auto", method="GET", body=""):
            return Fetched("<html>x</html>", True, "2026-09-17 16:20:11")

        with patch("core.app_debug.fetch_ex", side_effect=hit):
            pages = fetch_debug_pages(self._steps())
        self.assertIs(pages[0]["cached"], True)
        self.assertEqual(pages[0]["fetched_at"], "2026-09-17 16:20:11")

    def test_toc_page_shares_detail_id_so_content_is_not_dropped(self):
        """目录页与详情页共用 detail id：3 页上限下正文页不会被挤掉。"""
        steps = build_steps(SAMPLE + [])
        for s in steps:
            if s["name"] == "toc":
                s["url"] = "https://www.52shuku.net/toc.html"
        with patch("core.app_debug.fetch_ex", side_effect=fake_fetch):
            pages = fetch_debug_pages(steps)
        self.assertEqual([p["id"] for p in pages], ["search", "detail", "chapter"])


# ------------------------------------------------------------------ 假 WS 服务端

def _server_frame(payload: bytes, opcode: int = 0x1) -> bytes:
    """服务端发出的帧**不掩码**。"""
    n = len(payload)
    head = struct.pack("!BB", 0x80 | opcode, n) if n < 126 else \
        struct.pack("!BBH", 0x80 | opcode, 126, n)
    return head + payload


def _read_frame(conn: socket.socket):
    """读一帧客户端帧（必掩码），返回 (opcode, payload)。"""
    def _exact(n):
        buf = b""
        while len(buf) < n:
            chunk = conn.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("closed")
            buf += chunk
        return buf

    h = _exact(2)
    opcode = h[0] & 0x0F
    masked = h[1] & 0x80
    n = h[1] & 0x7F
    if n == 126:
        n = struct.unpack("!H", _exact(2))[0]
    mkey = _exact(4) if masked else b""
    data = _exact(n) if n else b""
    if masked:
        data = bytes(b ^ mkey[i % 4] for i, b in enumerate(data))
    return opcode, data


class FakeDebugServer:
    """最小 WS 服务端：握手 → 读请求 → 逐条推文本 → 发 ping → 发 close。

    只实现到「够钉住客户端行为」的程度，不追求 RFC 完整性。
    """

    def __init__(self, messages, ping_first=False):
        self.messages = messages
        self.ping_first = ping_first
        self.request = None                 # 客户端发来的 {"tag","key"}
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.sock.settimeout(10)
        self.port = self.sock.getsockname()[1]
        self.error = None
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        try:
            conn, _ = self.sock.accept()
            conn.settimeout(10)
            with conn:
                head = b""
                while b"\r\n\r\n" not in head:
                    head += conn.recv(1)
                # 客户端只校验首行含 101，Accept 值不必真算
                conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\n"
                             b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                             b"Sec-WebSocket-Accept: dummy\r\n\r\n")
                opcode, data = _read_frame(conn)
                self.request = json.loads(data.decode("utf-8"))
                if self.ping_first:
                    conn.sendall(_server_frame(b"", opcode=0x9))
                    _read_frame(conn)        # 等客户端回 pong
                for msg in self.messages:
                    conn.sendall(_server_frame(msg.encode("utf-8")))
                conn.sendall(_server_frame(b"", opcode=0x8))   # close
        except Exception as e:                 # pragma: no cover - 只在测试自身出错时
            self.error = e

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass
        self._thread.join(timeout=5)


class TestWebSocketClient(unittest.TestCase):
    def test_collects_events_and_sends_raw_tag(self):
        """端到端：tag 原样发出（含尾部斜杠）、事件按序收回、t 用前缀的耗时。"""
        raw_url = "https://www.52shuku.net/"      # 带尾部斜杠的导入原文
        server = FakeDebugServer([
            "[00:00.500]︾开始解析正文页",
            "[00:01.250]└正文",
            "[00:02.000]︽正文页解析完成",
        ])
        try:
            events = collect_debug_events("127.0.0.1", raw_url, "我",
                                          port=server.port, timeout=10)
        finally:
            server.close()
        self.assertIsNone(server.error)
        # tag 必须原样：规范化（去尾部斜杠）过的话 App 查不到源
        self.assertEqual(server.request, {"tag": raw_url, "key": "我"})
        self.assertEqual([e["text"] for e in events],
                         ["[00:00.500]︾开始解析正文页",
                          "[00:01.250]└正文",
                          "[00:02.000]︽正文页解析完成"])
        self.assertEqual([e["t"] for e in events], [0.5, 1.25, 2.0])

    def test_answers_ping_and_stops_on_close(self):
        """ping 必须回 pong（否则 App 判死）；收到 close 就停，不空等超时。"""
        server = FakeDebugServer([
            "[00:00.100]︾开始解析搜索页",
            "[00:00.200]︽搜索页解析完成",
        ], ping_first=True)
        try:
            events = collect_debug_events("127.0.0.1", "https://a.com", "我",
                                          port=server.port, timeout=10)
        finally:
            server.close()
        self.assertIsNone(server.error)
        self.assertEqual(len(events), 2)

    def test_empty_key_falls_back_to_default(self):
        """key 为空时补默认值「我」，不让 App 收到空 key。"""
        server = FakeDebugServer([])
        try:
            collect_debug_events("127.0.0.1", "https://a.com", "",
                                 port=server.port, timeout=10)
        finally:
            server.close()
        self.assertEqual(server.request["key"], "我")

    def test_missing_host_raises_value_error(self):
        with self.assertRaises(ValueError):
            collect_debug_events("", "https://a.com", "我", timeout=1)


class TestRunAppDebug(unittest.TestCase):
    def test_success_shape_and_pages(self):
        """run_app_debug 端到端：形状与 verify_chain 一致 + 抓到页面。"""
        server = FakeDebugServer(SAMPLE)
        try:
            with patch("core.app_debug.fetch_ex", side_effect=fake_fetch):
                out = run_app_debug("127.0.0.1", "https://www.52shuku.net/", "我",
                                    port=server.port, timeout=10,
                                    source={"header": '{"Referer":"https://a.com/"}'})
        finally:
            server.close()
        self.assertEqual(out["source"], "app")
        self.assertEqual(out["error"], "")
        self.assertTrue(out["all_ok"])
        self.assertEqual(len(out["events"]), len(SAMPLE))
        self.assertEqual([s["name"] for s in out["steps"]],
                         ["search", "bookUrl", "toc", "content"])
        self.assertEqual([p["id"] for p in out["pages"]],
                         ["search", "detail", "chapter"])

    def test_connect_failure_returns_readable_error_not_raise(self):
        """连不上（端口没人听）→ 返回可读 error，不抛异常（接口就不会 500）。"""
        # 先占一个端口再关掉，拿到一个几乎肯定没人监听的端口号
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        out = run_app_debug("127.0.0.1", "https://a.com", "我", port=port, timeout=2)
        self.assertEqual(out["steps"], [])
        self.assertEqual(out["pages"], [])
        self.assertIn("连不上 App", out["error"])
        self.assertIn("Web 服务", out["error"])

    def test_zero_events_explains_tag_mismatch(self):
        """零事件：把「tag 必须用导入原文」这条排查线索写进 error。"""
        server = FakeDebugServer([])
        try:
            out = run_app_debug("127.0.0.1", "https://a.com", "我",
                                port=server.port, timeout=10)
        finally:
            server.close()
        self.assertIn("bookSourceUrl", out["error"])
        self.assertEqual(out["steps"], [])


class TestQualityNewPageShared(unittest.TestCase):
    def test_verify_and_app_debug_share_one_implementation(self):
        """页面登记只有一份实现：verify._new_page 就是 quality.new_page。"""
        from core import verify
        self.assertIs(verify._new_page, Q.new_page)

    def test_new_page_keeps_first_and_flags_truncated(self):
        pages = {}
        big = "x" * (Q.MAX_PAGE_HTML_CHARS + 10)
        self.assertEqual(Q.new_page(pages, "search", "https://a.com", big), "search")
        self.assertTrue(pages["search"]["truncated"])
        self.assertEqual(len(pages["search"]["html"]), Q.MAX_PAGE_HTML_CHARS)
        self.assertEqual(pages["search"]["len"], Q.MAX_PAGE_HTML_CHARS + 10)
        # 同 id 再来一份：保留先登记的那份
        Q.new_page(pages, "search", "https://b.com", "<html>2</html>")
        self.assertEqual(pages["search"]["url"], "https://a.com")
        # 空 html 不登记
        self.assertEqual(Q.new_page(pages, "chapter", "https://c.com", ""), "")
        self.assertNotIn("chapter", pages)


# ------------------------------------------------------------------ 变异测试表
# 关键用例做过变异测试（改坏被它守的那行 → 必须变红；逐个单独验证过）：
#   M1  _split_segments 的 ``_strip_prefix(raw)`` → ``str(raw)``
#       → 16 条变红（前缀没剥掉，行首匹配全废）：test_prefix_is_stripped_from_values、
#         test_url_comes_from_fetch_success_line、test_notes_carry_stat_lines …
#   M2  PAGE_IDS 的 ``"toc": "detail"`` → ``"toc"``
#       → test_page_ids_match_drawer_pages、test_only_toc_segment（2 条）
#   M3  ``_is_error`` 的「行首匹配」 → 「正文里含『失败』也算」
#       → test_failure_word_inside_content_is_not_an_error（1 条，正文被误判成 fail）
#   M4  PAGE_IDS 的 ``"explore": "explore"`` → ``"detail"``（与详情页共用）
#       → 3 条变红：test_explore_has_its_own_page_id_not_detail、
#         test_explore_chain_keeps_four_steps_to_three_pages、
#         test_explore_entry_event_alone_still_names_the_segment
#   M5  ``_START_RE`` / ``_DONE_RE`` 去掉 ``发现``
#       → 4 条变红：TestExploreSegment 全 3 条 + test_explore_chain_…（发现段不再分段）
#   M6  ``_ENTRY_RE`` 的 ``(?:问)?`` 去掉（只认「访」不认「访问」）
#       → 1 条变红：test_explore_entry_event_alone_still_names_the_segment
#   M7  ``MAX_PAGES`` 3 → 4
#       → 2 条变红：test_pages_have_verify_chain_shape、
#         test_explore_chain_keeps_four_steps_to_three_pages（上限两个方向都有用例守）

if __name__ == "__main__":
    unittest.main()


# -------------------------------------------------- 正文规则为空的补抓

class TestEmptyContentFallback(unittest.TestCase):
    """正文规则为空：App 不请求正文页，只打一行「⇒正文规则为空,使用章节链接:」。

    规则为空恰恰是用户**最需要看正文页源码**的时刻（要从零写规则），不补抓
    这一页，抽屉里的整页源码 / 候选 / AI 提议就全部失效。这一组用例钉住
    fallback 的三件事：URL 从哪来、怎么绝对化、怎么应用请求选项。
    """

    def _run(self, sample=EMPTY_CONTENT_SAMPLE, **spy_overrides):
        steps = build_steps(sample)
        seen = {}

        def spy(url, timeout=15, headers=None, charset="", proxy="", source=None,
                cache="auto", method="GET", body=""):
            seen.update(url=url, headers=headers or {}, charset=charset,
                        method=method, body=body)
            return Fetched("<html>正文页</html>", False, "")

        with patch("core.app_debug.fetch_ex", side_effect=spy):
            pages = fetch_debug_pages(steps)
        return steps, pages, seen

    def test_falls_back_to_chapter_link_and_backfills_step_url(self):
        steps, pages, seen = self._run()
        content = step_of(steps, "content")
        # build_steps 本身不认那一行（它不是页面请求）：url 是抓取层回填的
        self.assertEqual(content["url"], EMPTY_CONTENT_CHAPTER_URL)
        self.assertEqual(seen["url"], EMPTY_CONTENT_CHAPTER_URL)
        self.assertEqual([p["id"] for p in pages], ["detail", "chapter"])
        self.assertEqual(pages[-1]["url"], EMPTY_CONTENT_CHAPTER_URL)

    def test_base_prefers_toc_segment_then_book_url(self):
        """目录段没有 URL（如 SAMPLE）时退详情段的 URL 当绝对化 base。"""
        sample = [
            "[00:00.800]︾开始解析详情页",
            "[00:01.100]≡获取成功:%s" % TOC_URL,
            "[00:01.160]︽详情页解析完成",
            "[00:01.200]︾开始解析目录页",
            "[00:01.940]︽目录页解析完成",
            "[00:02.000]︾开始解析正文页",
            "[00:02.100]⇒正文规则为空,使用章节链接:/reader/1.html",
            "[00:02.200]︽正文页解析完成",
        ]
        _steps, pages, seen = self._run(sample)
        self.assertEqual(seen["url"], "https://www.52shuku.net/reader/1.html")
        self.assertEqual([p["id"] for p in pages], ["detail", "chapter"])

    def test_absolute_chapter_link_is_kept_as_is(self):
        """章节链接本来就是绝对 URL 时原样使用。"""
        sample = list(EMPTY_CONTENT_SAMPLE)
        sample[6] = "[00:01.100]⇒正文规则为空,使用章节链接:https://b.com/c1.html"
        _steps, _pages, seen = self._run(sample)
        self.assertEqual(seen["url"], "https://b.com/c1.html")

    def test_url_options_are_applied_to_the_fetch(self):
        """章节链接自带 ``,{...}`` 选项：抓取按选项发（method/body/headers），
        回填的 step.url 保留选项原文——「从此步重跑」要拿它当 App 的 key，
        App 端 AnalyzeUrl 自己会再解析。"""
        sample = list(EMPTY_CONTENT_SAMPLE)
        sample[6] = ("[00:01.100]⇒正文规则为空,使用章节链接:"
                     '/book/1/c1.html,{"method":"POST","body":"id=1",'
                     '"headers":{"X-Token":"t"}}')
        _steps, pages, seen = self._run(sample)
        self.assertEqual(seen["url"], EMPTY_CONTENT_CHAPTER_URL)
        self.assertEqual(seen["method"], "POST")
        self.assertEqual(seen["body"], "id=1")
        # 选项 header 与源声明合并；这里没传源，看默认 UA 与选项共存即可
        self.assertEqual(seen["headers"].get("X-Token"), "t")
        self.assertEqual(step_of(_steps, "content")["url"],
                         EMPTY_CONTENT_CHAPTER_URL + ',{"method":"POST","body":"id=1",'
                         '"headers":{"X-Token":"t"}}')
        self.assertEqual([p["id"] for p in pages], ["detail", "chapter"])

    def test_post_body_template_is_skipped_with_note(self):
        """body 里的模板只有 App（Rhino）求得值：不抓、note 说明，判定不受影响。"""
        sample = list(EMPTY_CONTENT_SAMPLE)
        sample[6] = ("[00:01.100]⇒正文规则为空,使用章节链接:"
                     '/book/1/c1.html,{"method":"POST","body":"kw={{key}}"}')
        steps, pages, seen = self._run(sample)
        content = step_of(steps, "content")
        self.assertTrue(content["has_notes"])
        self.assertIn("连 App", content["notes"][-1])
        self.assertEqual([p["id"] for p in pages], ["detail"])

    def test_no_fallback_line_leaves_content_without_page(self):
        """没有那行（也没有 ≡获取成功）：维持旧行为——无正文页、不报错。"""
        sample = [s for s in EMPTY_CONTENT_SAMPLE if "正文规则为空" not in s]
        steps, pages, _seen = self._run(sample)
        self.assertIsNone(step_of(steps, "content")["url"] or None)
        self.assertEqual([p["id"] for p in pages], ["detail"])
