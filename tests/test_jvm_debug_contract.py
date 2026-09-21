# -*- coding: utf-8 -*-
"""S5-A1 契约测试：JVM 调试服务（appservice/DebugService.kt）落盘的 NDJSON，Python 侧能不能吃。

**为什么要有这条**：两侧的接口只有一份（NDJSON 行 + args.properties 键表），
但它横跨 Kotlin 与 Python 两个语言。A1 的验收判据就是「`_split_segments` /
`build_steps` **一行不改**能解析」——那条判据必须有个会自动跑的东西钉着，
否则下次谁改了 `DebugService` 的落盘形状，要到 A4 接进产品时才炸。

fixture 是**真实产出**（不是手写的）：2026-09-20 在 JVM 里跑 52shuku 那条源，
`key=斗破苍穹`，由 `DebugService` 落盘、原样拷进来。手写的事件列表只能证明
「我们以为 App 会这么推」，真实产出才能证明**我们推出来的确实是那个形状**
（含 `[mm:ss.SSS]` 前缀、`⇒/︾/︽/◇/≡/┌/└` 标记、App 自家异常的首行）。
"""

import json
import pathlib
import re
import unittest

from core.app_debug import build_steps, _split_segments, _strip_prefix, _PREFIX_RE

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "appservice_debug_52shuku.ndjson"

#: App 的事件 kind（`Debug.EventKind`）。**payload 那四个必须不在**——
#: App 的调试 WS 就是这个过滤（`if (!event.kind.isSourcePayload) session.send(...)`），
#: 所以设备侧从来没有它们，`matched_html` 才恒空。我们跟着丢掉才叫「同构」。
_PAYLOAD_KINDS = {"SearchSource", "InfoSource", "TocSource", "ContentSource"}


def load_events():
    lines = FIXTURE.read_text(encoding="utf-8").splitlines()
    return [json.loads(x) for x in lines if x.strip()]


class TestNdjsonShape(unittest.TestCase):
    """落盘形状本身：行 JSON、三个键、LF 行尾。"""

    def test_every_line_is_json_with_the_three_keys(self):
        evs = load_events()
        self.assertTrue(evs, "fixture 不该是空的")
        for e in evs:
            self.assertEqual(set(e), {"kind", "elapsed_ms", "text"}, e)

    def test_no_payload_kinds(self):
        """payload（HTML）事件按 App 的口径丢弃——落盘里一条都不许有。"""
        kinds = {e["kind"] for e in load_events()}
        self.assertFalse(kinds & _PAYLOAD_KINDS, kinds & _PAYLOAD_KINDS)

    def test_kinds_are_known_app_event_kinds(self):
        known = {"Message", "Error", "Completed"} | _PAYLOAD_KINDS
        self.assertTrue({e["kind"] for e in load_events()} <= known)

    def test_text_carries_the_time_prefix(self):
        """**前缀是 `Debug.log` 自己加的**，我们不许再包一层。

        这条钉的是「别再包一层」：`_strip_prefix` 只剥一层，包两层会剩下一层
        留在正文里，段首匹配（`︾开始解析X页`）就全失效了——而那种失败看起来
        像「App 什么都没推」。
        """
        evs = load_events()
        with_prefix = [e for e in evs if _PREFIX_RE.match(e["text"])]
        self.assertGreater(len(with_prefix), len(evs) // 2,
                           "多数事件应带 [mm:ss.SSS] 前缀")
        for e in with_prefix:
            body = _strip_prefix(e["text"])
            self.assertFalse(_PREFIX_RE.match(body),
                             "剥一层后不该还有前缀: %r" % e["text"][:60])

    def test_file_is_lf_only(self):
        """跨平台确定：Windows 上 `newLine()` 会给 CRLF，那样 fixture 对不上。"""
        raw = FIXTURE.read_bytes()
        self.assertEqual(raw.count(b"\r\n"), 0)
        self.assertEqual(raw.count(b"\n"), len([x for x in raw.split(b"\n") if x.strip()]),
                         "每一行都应以 LF 结束、且没有空行尾巴")


class TestSplitSegmentsEatsIt(unittest.TestCase):
    """**核心判据**：`_split_segments` / `build_steps` 不改一行就能解析。"""

    def test_segments_are_the_expected_four(self):
        steps = build_steps(load_events())
        self.assertEqual([s["name"] for s in steps],
                         ["search", "bookUrl", "toc", "content"])

    def test_steps_have_the_verify_chain_shape(self):
        steps = build_steps(load_events())
        for s in steps:
            for key in ("name", "verdict", "values", "url", "page_id",
                        "notes", "matched_html", "ok"):
                self.assertIn(key, s)
            self.assertEqual(s["matched_html"], "")

    def test_search_segment_verdict_and_notes(self):
        search = build_steps(load_events())[0]
        self.assertEqual(search["verdict"], "pass")
        self.assertTrue(any("书籍总数" in n for n in search["notes"]))

    def test_app_exception_makes_content_fail(self):
        """这条 fixture 的正文段真的抛了 App 自家异常 → 必须判 fail，不是 unknown。

        钉住 `ERROR_PREFIXES` 里的 `io.legado.app.`：漏了它这条会变成 unknown，
        把「真出错」读成「我们没测出来」。
        """
        content = [s for s in build_steps(load_events()) if s["name"] == "content"][0]
        self.assertEqual(content["verdict"], "fail")
        self.assertIn("ContentEmptyException", content["reason"])

    def test_urls_are_extracted(self):
        steps = {s["name"]: s for s in build_steps(load_events())}
        self.assertTrue(steps["search"]["url"].startswith("http"))
        self.assertTrue(steps["toc"]["url"])

    def test_events_round_trip_through_dicts(self):
        """`build_steps` 收 dict 也收裸文本——两种输入必须同结论。"""
        evs = load_events()
        self.assertEqual(build_steps(evs), build_steps([e["text"] for e in evs]))


class TestContractWithKotlinSource(unittest.TestCase):
    """两侧契约的「另一侧」：参数键表与退出码写死在 Kotlin 里，这里跟着钉。

    不去解析 Kotlin 源码（脆弱），而是把**约定**记在这里：改一边就得改另一边，
    测试会提醒你。
    """

    ARGS_KEYS = ("file", "key", "out", "timeout", "cookie")
    EXIT_CODES = {0: "正常", 2: "零事件", 3: "超时", 4: "入参/输入错误", 5: "事件流被截断"}

    def test_args_keys_match_the_launcher(self):
        src = (pathlib.Path(__file__).parent.parent /
               "appservice/test/io/legado/app/service/DebugServiceLauncher.kt")
        text = src.read_text(encoding="utf-8")
        for k in self.ARGS_KEYS:
            self.assertIn('getProperty("%s")' % k, text,
                          "启动器不再读 %s= 了：契约变了，同步改这里与 A4 的调用方" % k)

    def test_exit_codes_match_the_service(self):
        src = (pathlib.Path(__file__).parent.parent /
               "appservice/test/io/legado/app/service/DebugService.kt")
        text = src.read_text(encoding="utf-8")
        for code, label in self.EXIT_CODES.items():
            self.assertIn("const val", text)
            self.assertIn("= %d" % code, text,
                          "退出码 %d（%s）不见了：它是调用方唯一的分派依据" % (code, label))


class TestLoginMarkerParity(unittest.TestCase):
    """登录墙特征词**两侧必须逐词相同**（A3）。

    为什么要有这条：判定发生在 JVM 里（`SourceCookies.loginMarkerOf`，那是产生结论的地方），
    而权威那份在 `core/models.py: LOGIN_MARKERS`（本地回放用的）——跨语言没法共享代码，
    只能靠这条测试把「两边各改一边」拦住。漂了的后果不是报错，是**同一个页面在两个通道
    判出不同结论**（一个说需登录、一个说没结果），而那正是 AGENTS #4 那类静默错。

    对照的是**源码字面量**（不跑 Kotlin）：读 `SourceCookies.kt` 里的那串词，与
    `core.models.LOGIN_MARKERS` 比。加词只改一边就会红。
    """

    KOTLIN = (pathlib.Path(__file__).parent.parent /
              "appservice/test/io/legado/app/service/SourceCookies.kt")

    def test_login_markers_match_the_python_side(self):
        from core.models import LOGIN_MARKERS
        text = self.KOTLIN.read_text(encoding="utf-8")
        block = text.split("val LOGIN_MARKERS = listOf(", 1)
        self.assertEqual(len(block), 2, "SourceCookies.kt 里找不到 LOGIN_MARKERS 字面量")
        body = block[1].split(")", 1)[0]
        kotlin_markers = [s for s in re.findall(r'"([^"]*)"', body)]
        self.assertEqual(kotlin_markers, list(LOGIN_MARKERS),
                         "两侧的登录墙特征词漂了：改一边就得改另一边（权威在 core/models.py）")


class TestMatchedStepNameParity(unittest.TestCase):
    """命中回填（第三期）的**词汇与上限**两侧必须一致。

    为什么要有这条：`matched_html` 的形状是 `{url: {段名: DOM}}`，**段名由 Kotlin 写、
    由 Python 取**（`core/app_debug.build_steps` 按每段自己的 url + 段名查）。跨语言没法
    共享代码，两边各改一边的后果不是报错，是**那块证据永远空着**——抽屉里显示成「这条
    规则没有选中任何 DOM」，而其实是没人去取（AGENTS #4 / #22⑤ 那一类）。

    对照的是**源码字面量**（不跑 Kotlin），与 `TestLoginMarkerParity` 同一个套路。
    """

    KOTLIN = (pathlib.Path(__file__).parent.parent /
              "appservice/test/io/legado/app/service/DebugService.kt")

    def _names_and_text(self):
        text = self.KOTLIN.read_text(encoding="utf-8")
        block = text.split("private val MATCHED_STEP_NAMES = listOf(", 1)
        self.assertEqual(len(block), 2, "DebugService.kt 里找不到 MATCHED_STEP_NAMES")
        body = block[1].split(")", 1)[0]
        return re.findall(r'"([^"]*)"', body), text

    def test_step_names_are_names_python_actually_asks_for(self):
        """本机侧记的每个段名，Python 都得真的会去取（权威词表在 SEGMENT_NAMES）。"""
        from core.app_debug import SEGMENT_NAMES
        names, _ = self._names_and_text()
        self.assertTrue(names, "MATCHED_STEP_NAMES 不该是空的")
        unknown = [n for n in names if n not in set(SEGMENT_NAMES.values())]
        self.assertEqual(unknown, [], "这些段名 Python 永远不会取：%s" % unknown)
        # 三条列表/正文段一个都不能少：少一个就是「那条规则命中的 DOM 交不回来」
        self.assertLessEqual({"search", "explore", "toc", "content"}, set(names))

    def test_every_name_has_a_rule_branch(self):
        """**记了名字就得有规则**：没有分支的段名恒不命中，而它看起来像「规则没选中」。"""
        names, text = self._names_and_text()
        block = text.split("private fun matchedRuleOf(", 1)
        self.assertEqual(len(block), 2, "DebugService.kt 里找不到 matchedRuleOf")
        body = block[1].split('else -> ""', 1)[0]
        branches = set(re.findall(r'"([a-zA-Z]+)"', body))
        self.assertEqual(set(names) - branches, set(),
                         "这些段名在 matchedRuleOf 里没有分支，永远记不出东西来")

    def test_node_limit_matches_the_python_side(self):
        """每段记几个节点：本地投影那份在 `core/quality.py`（两侧的 DOM 要能对着看）。"""
        from core.quality import MATCHED_NODES_LIMIT
        text = self.KOTLIN.read_text(encoding="utf-8")
        block = text.split("const val MATCHED_NODES_LIMIT = ", 1)
        self.assertEqual(len(block), 2, "DebugService.kt 里找不到 MATCHED_NODES_LIMIT")
        got = int(re.match(r"\d+", block[1]).group(0))
        self.assertEqual(got, MATCHED_NODES_LIMIT, "两侧的命中节点上限漂了")


class TestWebViewMarkerParity(unittest.TestCase):
    """「这条源带 webView 标记吗」两侧必须**同一个词**。

    为什么要有这条：Kotlin 用它在侧车里记 `webview_unsupported`（那条诊断说「撞上
    shadow 的三条边界时这几段不可信」），前端用它在定层里判 L2
    （`frontend/src/utils/layers.js`）。两边各改一边的后果不是报错，是**同一个源在两个
    地方被分成两层**——用户照着界面换通道，跑到侧车里却是另一种说法（AGENTS #22⑤）。

    对照的是**源码字面量**（不跑 Kotlin、也不跑 JS），与 `TestLoginMarkerParity` 同一套路。
    """

    KOTLIN = (pathlib.Path(__file__).parent.parent /
              "appservice/test/io/legado/app/service/DebugService.kt")
    JS = (pathlib.Path(__file__).parent.parent /
          "frontend/src/utils/layers.js")

    def test_the_pattern_is_the_same_on_both_sides(self):
        bs = '\\'
        kt = self.KOTLIN.read_text(encoding="utf-8")
        block = kt.split("Regex(", 1)
        self.assertEqual(len(block), 2, "DebugService.kt 里找不到 webViewPattern")
        kt_pat = block[1].split(",", 1)[0].strip().strip('"')
        # 两侧**写法**不同、**判据**要一样，比之前各自归一：
        #   Kotlin 是字符串字面量 → `BS"` 才是引号、`BSBSs` 才是正则的 `BSs`
        #   JS 是正则字面量       → 直接写 `"` 与 `BSs`
        kt_pat = kt_pat.replace(bs + bs, bs).replace(bs + '"', '"')

        js = self.JS.read_text(encoding="utf-8")
        lines = [ln for ln in js.splitlines() if "WEBVIEW_RE = " in ln]
        self.assertEqual(len(lines), 1, "layers.js 里找不到 WEBVIEW_RE")
        body = lines[0].split("WEBVIEW_RE = /", 1)[1]
        js_pat, _, flags = body.rpartition("/")
        js_pat = js_pat.replace(bs + bs, bs)

        self.assertEqual(js_pat, kt_pat, "两侧的 webView 判据漂了")
        self.assertIn("i", flags, "大小写不敏感要写在两边（Kotlin 是 IGNORE_CASE）")
        self.assertIn("IGNORE_CASE", kt)


class TestEngineHtmlKeyParity(unittest.TestCase):
    """侧车里「引擎取回的整页」那个键，两侧必须一致。

    为什么：Kotlin 写（`DebugService.runOnce` 的 `writeMeta`）、Python 读
    （`core.jvm_debug` 的 `meta.get(...)`）。跨语言没法共享常量，写错一边的后果
    **不是报错**——Python 读到 None → `engine_pages(None)` 返回 `{}` → `pages[]`
    静默回落到我们补抓，界面上照样有一页（但那是**另一份材料**），没人看得出来。
    """

    def test_the_key_is_spelled_the_same_on_both_sides(self):
        kt = (pathlib.Path(__file__).parent.parent /
              "appservice/test/io/legado/app/service/DebugService.kt").read_text(encoding="utf-8")
        py = (pathlib.Path(__file__).parent.parent / "core/jvm_debug.py").read_text(encoding="utf-8")
        self.assertIn('"engine_html" to', kt,
                      "DebugService 侧车里没有 engine_html 这个键")
        self.assertIn('meta.get("engine_html")', py,
                      "core/jvm_debug 没在读 engine_html（键名两边要一样）")


if __name__ == "__main__":
    unittest.main()
