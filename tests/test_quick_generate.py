# -*- coding: utf-8 -*-
"""快速生成（`services/add_source.run_add`）的第一条测试：**一条网都不联**，全喂 fixture。

为什么现在才补：这条链（URL → 抓页 → 分析 → 生成 → 落盘）此前**一条测试都没有**，
于是它能长期坏着——书名写成 `[分类]绍宋` 的站点必然失配，而界面上只看到四个字
「生成失败」（lessons §七十七）。这里钉住两件事：

  1. **认得出**：书名带 `[分类]` 前缀时仍能推断出规则，且**判据写进 note**；
  2. **失败必带原因**：`rc != 0` 时 `AddResult.error` 非空——原因不许在中途被压掉。

fixture 是**实测页面**（2026-09-21 抓 `samsbook.com` 的搜索结果，5112 字节，原样存下）：
`<title>` 与 `<b>` 里同样含关键词，所以它还顺带钉着「别把页面标题当书名」。
"""
import contextlib
import json
import pathlib
import tempfile
import unittest
from unittest import mock

from core.analyzer import analyze_search_page
from core.fetch import _page_has_search_results
from services.add_source import run_add

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "samsbook_search_shaosong.html"
MANGA_FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "manhuayu88_search_wo.html"
#: 实测页面（2026-10-08 抓 `m.qudushu.org` 搜索「我」，10873 字节，原样存下）。
#: 它钉的是一条**误判链**：结果页里有 50 个 `p.sone` 结果项，但详情链接长成
#: `/html/1287420/asc-1/`——路径里一个 `DETAIL_LINK_HINTS` 的词都没有，于是
#: `_page_has_search_results` 判否 → `run_add` 静默降级成「仅发现」（见下方用例）。
QUDUSHU_FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "qudushu_search_wo.html"
KEYWORD = "绍宋"
#: 搜索 URL（关键词在 query 里，`extract_keyword` 认得出来）
URL = "http://example.com/search.php?q=" + KEYWORD

#: 详情页 stub：够 `analyze_detail_page` 推断出目录规则就行（不联网）
DETAIL_HTML = """<html><body><div class="row"><ul class="chapter-list">
<li><a href="/book/0/282/1.html">第一章</a></li>
<li><a href="/book/0/282/2.html">第二章</a></li>
<li><a href="/book/0/282/3.html">第三章</a></li>
</ul></div></body></html>"""


def _fake_fetch(html):
    """替身：搜索 URL 给 html，其余（详情页 / 正文页）给 DETAIL_HTML。"""
    def go(url, *a, **kw):
        return html if "q=" in str(url) else DETAIL_HTML
    return go


class AnalyzeSearchPageTests(unittest.TestCase):
    """纯函数那一半：认得出 + 判据能看见 + 噪音不当书名。"""

    def setUp(self):
        self.html = FIXTURE.read_text(encoding="utf-8")

    def test_decorated_title_infers_the_rules_and_records_the_evidence(self):
        a = analyze_search_page(self.html, KEYWORD)
        self.assertEqual(a["results"], 1, a)          # 只认那一条 `[历史]绍宋`
        self.assertTrue(a["bookList"], a)
        self.assertTrue(a["name"], a)
        self.assertTrue(a["bookUrl"], a)
        # 判据要能看见：用户要能反驳「你凭什么说这是书名」
        self.assertIn("前缀装饰", a["note"], a["note"])

    def test_sample_book_url_is_the_first_link_the_generated_rule_selects(self):
        """详情页样例 = 生成的 `bookUrl` 规则选中的**第一条**链接。

        `run_add` 拿 `sampleBookUrl` 当详情页样例（不再拿规则重跑一遍）。两者一旦
        漂开，后面的目录/正文规则就是按**另一条**书的页面配的，而界面看不出来
        （AGENTS #11 一族：两个来源必须同源）。

        **有意只取第一条**：`analyze_search_page` 按第一个命中的锚点推断规则，
        详情页样例也只能有一条——所以这里直接钉住 fixture 里那一条。
        """
        a = analyze_search_page(self.html, KEYWORD)
        self.assertEqual(a["sampleBookUrl"], "/book/0/282/", a)
        self.assertTrue(a["bookUrl"].endswith("@href"), a)

    def test_page_title_text_is_not_taken_as_the_book_name(self):
        """`<title>` / `<b>` 里也含关键词，但它们不是链接 → 一个都不许当锚点。"""
        a = analyze_search_page(self.html, "共2条记录")
        self.assertEqual(a["results"], 0, a)
        self.assertEqual(a["bookList"], "")

    def test_decorated_name_is_not_taken_as_the_author(self):
        """书名那段不许被当成作者。

        原来靠 `txt == keyword` 挡（认得出书名时它必然成立），书名带上装饰之后这句
        不再成立——`[历史]绍宋` 会被当成作者名写进规则（AGENTS #12 那一类：判据要跟着改）。
        """
        html = ('<div class="book-list"><div class="item">'
                '<p class="nm"><a href="/book/1">[历史]绍宋</a></p>'
                '<p class="au">作者：榴弹怕水</p></div></div>')
        a = analyze_search_page(html, KEYWORD)
        self.assertNotIn(".nm", a["author"], a)
        self.assertIn(".au", a["author"], a)

    def test_link_with_the_keyword_inside_is_accepted_and_noise_is_not(self):
        """第 3 档（含关键词的链接）既要**能用**、也要**挡住噪音**。

        同一个词同时出现在页面标题、`<b>` 与链接里时，只认链接那一个——去掉
        「必须是链接」这条要求，`<b>` 会被一并当成书名（实测 samsbook 的 `<b>`
        就写着「绍宋-搜索结果(共2条记录)」）。
        """
        html = ('<html><head><title>绍宋-搜索结果</title></head><body>'
                '<b>绍宋 全集</b>'
                '<div class="book-list"><div class="item">'
                '<a href="/book/1/">绍宋 全集</a></div></div></body></html>')
        a = analyze_search_page(html, KEYWORD)
        self.assertEqual(a["results"], 1, a)
        self.assertIn("含关键词", a["note"], a["note"])

    def test_no_match_explains_why(self):
        a = analyze_search_page(self.html, "这本书页面上没有")
        self.assertEqual(a["results"], 0)
        self.assertTrue(a["note"], "认不出时必须给出原因（AGENTS #4）")

    def test_manga_media_cards_do_not_use_search_heading_as_list(self):
        html = MANGA_FIXTURE.read_text(encoding="utf-8")
        a = analyze_search_page(html, "我")
        self.assertEqual(a["results"], 2, a)
        self.assertEqual(a["bookList"], ".columns .media", a)
        self.assertEqual(a["name"], ".media-content .title", a)
        self.assertEqual(a["bookUrl"], ".media-content .title@href", a)
        self.assertNotIn("h1", a["bookList"])

    def test_the_fast_add_path_does_not_load_a_rule_interpreter(self):
        """快速新增源这条路**不许**再依赖一个 Python 规则解释器。

        详情页样例用的是分析时看见的那个 href（`sampleBookUrl`）。谁要是图省事又写一句
        `apply_css_rule(...)` 从规则反算，本用例会红——那是**第二套规则解释器**
        （JS / xpath / 模板都要另算一遍）。`core/rules/replayer.py` 已按这个理由退场，
        这里守的是它别以任何形式回来。
        """
        import subprocess
        import sys

        probe = ("import sys, services.add_source, core.analyzer;"
                 "print('core.rules.replayer' in sys.modules)")
        root = str(pathlib.Path(__file__).resolve().parent.parent)
        p = subprocess.run([sys.executable, "-c", probe], capture_output=True,
                           text=True, cwd=root)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.strip(), "False",
                         "analyze/add 这条路又把 core.rules.replayer 导进来了：%s" % p.stdout)


class SearchResultDetectionTests(unittest.TestCase):
    """「这份页面里有没有结果」只有一份判据（`core.analyzer`），且它认得出长尾站点。

    实测（2026-10-08）：`m.qudushu.org` 的搜索页抓回来就是结果页（50 个 `p.sone` 条目、
    书名都在），但详情链接是 `/html/1287420/asc-1/`——词表（`DETAIL_LINK_HINTS`）里
    一个词都没有。当时 `_page_has_search_results` 自己写的那套「详情链接正则」判否，
    `run_add` 于是**静默降级成「仅发现」**、生成 `ruleSearch` 全空的源且 rc=0。
    """

    def setUp(self):
        self.html = QUDUSHU_FIXTURE.read_text(encoding="utf-8")

    def test_real_results_page_is_recognized(self):
        """一个字的短关键词在真站点上必然匹配到变体书名——必须靠结构认出来。"""
        a = analyze_search_page(self.html, "我")
        self.assertGreater(a["results"], 1, a)
        self.assertEqual(a["bookList"], ".searchresult .sone", a)
        self.assertIs(_page_has_search_results(self.html, "我"), True)

    def test_exact_title_still_works(self):
        a = analyze_search_page(self.html, "我门我派我江湖")
        self.assertEqual(a["results"], 1, a)
        self.assertEqual(a["bookList"], ".searchresult .sone", a)

    def test_absent_keyword_is_still_not_a_result(self):
        """反方向同样要钉住：页面上没有的词，不许因为「结构像列表」就判成搜到了。"""
        a = analyze_search_page(self.html, "这本书页面上肯定没有")
        self.assertEqual(a["results"], 0, a)
        self.assertIs(_page_has_search_results(self.html, "这本书页面上肯定没有"), False)

    def test_keyword_missing_from_the_page_is_never_a_result(self):
        """关键词压根不在页面上时，无论什么结构信号都不该判成「搜到了这本书」。"""
        self.assertIs(_page_has_search_results(self.html, "完全不存在的词"), False)

    def test_empty_result_marker_and_short_body_are_negatives(self):
        self.assertIs(_page_has_search_results(
            "<html><body><div>没有搜索到相关内容</div></body></html>", "我"), False)
        self.assertIs(_page_has_search_results("CN", "我"), False)


class FragmentAnalysisTests(unittest.TestCase):
    """HTML **片段**（没有 `<body>` 包一层）也要给出可用规则。

    L4 那条链拿到的是 JSON 信封里的 HTML 片段，`core/net_hunt` 把片段拼起来直接喂
    `analyze_search_page`。片段里 `list_item.parent` 可能是 `None` 甚至 BS4 的
    `[document]`——那时原来会抛 `AttributeError`（`container.name`），
    而把 `[document]` 写进选择器更糟：**那是一条永远选不中的假规则**。
    """

    def test_fragment_with_class_gets_a_usable_single_segment_rule(self):
        frag = ('<div class="item"><a href="/novel/1/">绍宋</a></div>'
                '<div class="item"><a href="/novel/2/">诡秘之主</a></div>')
        a = analyze_search_page(frag, "绍宋")
        self.assertEqual(a["results"], 1, a)
        self.assertEqual(a["bookList"], ".item", a)
        self.assertNotIn("[document]", a["bookList"])

    def test_classless_fragment_declines_instead_of_inventing_a_rule(self):
        """卡片连 class 都没有 → 宁可弃权（空规则），也不给「选中整页链接」的假规则。"""
        a = analyze_search_page("<a href='/novel/1/'>绍宋</a>", "绍宋")
        self.assertEqual(a["bookList"], "", a)
        self.assertNotIn("[document]", a["bookList"])

    def test_detection_survives_a_fragment(self):
        """判据在片段上不许抛（它会喂给 `_page_has_search_results`）。

        片段要够长：那层还有一道「小于 300 字节不算页面」的闸门（它挡的是 2 字节 `CN`
        那种响应），拿 187 字节的片段去测是把闸门当判据——那是两件事。
        """
        head = ('<html><head><title>搜索绍宋</title></head><body>'
                '<div class="head"><a href="/">首页</a><a href="/top">排行</a>'
                '<a href="/full">完本</a></div>')
        items = "".join(
            '<div class="item"><a href="/novel/%d/">绍宋%s</a></div>'
            % (i, "" if i == 1 else "后传%d" % i) for i in range(1, 4))
        frag = head + '<div class="searchresult">' + items + "</div></body></html>"
        self.assertGreater(len(frag.encode("utf-8")), 300)
        self.assertIs(_page_has_search_results(frag, "绍宋"), True)


class RunAddTests(unittest.TestCase):
    """整条链那一半：rc 与原因**一起**回来。"""

    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="quick_gen_"))
        self.out = self.root / "gen.json"
        self.html = FIXTURE.read_text(encoding="utf-8")

    def _patched(self, side_effect):
        """把所有会发请求的入口一起钉住（**两处 fetch 绑定**：services 与 analyzer 各一份；
        后者是正文推断那一步用的），外加主库去重那一步——它会去扫真 `data/*.json`
        （几千条），而这条测试要钉的不是重库那件事。
        被测函数自己开的依赖要在测试里连它一起钉住（lessons §七十四）。"""
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch("services.add_source.fetch", side_effect=side_effect))
        stack.enter_context(mock.patch("core.analyzer.fetch", side_effect=side_effect))
        stack.enter_context(mock.patch("services.add_source._find_main_sources",
                                       return_value=[]))
        return stack

    def _run(self, side_effect=None):
        with self._patched(side_effect or _fake_fetch(self.html)):
            return run_add(URL, name="测试源", output=str(self.out), no_ask=True,
                           probe=False, verify=False, interactive=False)

    def _case(self, out, rc, why=""):
        """共同约定：**rc 非 0 必须带原因**（错误码单独出现等于把原因丢了）。"""
        self.assertEqual(out.rc, rc, "%s：rc=%s error=%r" % (why, out.rc, out.error))
        if rc != 0:
            self.assertTrue(out.error, "rc=%s 却没有原因（%s）" % (rc, why))
        else:
            self.assertEqual(out.error, "")

    def test_generates_a_candidate_source(self):
        out = self._run()
        self._case(out, 0, "认得出就该成功")
        srcs = json.loads(self.out.read_text(encoding="utf-8"))
        self.assertEqual(len(srcs), 1)
        src = srcs[0]
        self.assertEqual(src["bookSourceName"], "测试源")
        self.assertTrue(src["ruleSearch"]["bookList"])
        self.assertTrue(src["ruleSearch"]["bookUrl"])
        # 详情页那一步也走通了（stub 页面）——目录规则跟着生成
        self.assertTrue(src.get("ruleToc", {}).get("chapterList"), src.get("ruleToc"))

    def test_incomplete_search_rules_are_not_saved(self):
        incomplete = {
            "results": 1, "bookList": ".columns .media", "name": ".title",
            "bookUrl": "", "coverUrl": "", "author": "", "intro": "",
            "note": "详情链接没有找到",
        }
        with mock.patch("services.add_source.analyze_search_page",
                        return_value=incomplete):
            out = self._run()
        self._case(out, 1, "缺少 bookUrl 不应落盘")
        self.assertIn("详情链接规则", out.error)
        self.assertFalse(self.out.exists(), "搜索规则不完整时不许写成功源")

    def test_existing_source_reports_rc2_with_a_reason(self):
        self._case(self._run(), 0)
        self._case(self._run(), 2, "第二次是同 URL 已存在")

    def test_no_result_page_falls_back_to_discover_mode(self):
        """搜不到结果**不是失败**：自动降级成「仅发现」模式（rc=0，searchUrl 留空）。

        实测确认过的既有行为，钉住它免得下次把它改成「失败」（那会让一批本来能用的
        站点直接生成不出来）。
        """
        blank = "<html><body><p>页面上没有那本书</p></body></html>"
        out = self._run(_fake_fetch(blank))
        self._case(out, 0, "搜不到 → 仅发现")
        src = json.loads(self.out.read_text(encoding="utf-8"))[0]
        self.assertEqual(src["searchUrl"], "")
        self.assertIn("仅发现", src["bookSourceGroup"])

    def test_unreachable_page_fails_with_a_reason(self):
        """真正的失败（抓不到页面）**必须带原因**——原来这里只有「生成失败」四个字。"""
        out = self._run(RuntimeError("连不上"))
        self._case(out, 1, "抓不到搜索页")
        # 原因要是**能读懂**的一句话，不是「生成失败」四个字
        self.assertNotEqual(out.error.strip(), "生成失败")
        self.assertGreater(len(out.error), 6, out.error)

    def test_two_byte_body_is_not_a_page_and_asks_the_engine(self):
        """**「有字节」不等于「拿到了页面」**：2 字节的 `CN` 不许被当成「站点不能搜」。

        实测（2026-09-22）：`18read.net` 回的是 2 字节的 `CN`，而拦截页判据要 28KB 那份
        材料才判得出——于是这条路**没走到引擎**就降级成「仅发现」，落一条 rc=0 的空源，
        原因一路被压掉（lessons §八十九）。这条测试钉住：小得不像页面时**去问引擎**，
        引擎说还是拦截页就**带原因失败**，不许静默降级。
        """
        with mock.patch("core.jvm_debug.page_from_engine",
                        return_value="<html><title>请稍候…</title>安全验证</html>") as eng:
            out = self._run(_fake_fetch("CN"))
        self._case(out, 1, "2 字节不是页面 → 要人工过验证")
        self.assertTrue(eng.called, "小得不像页面时必须去问引擎（判据在那一侧）")
        # 原因要走到用户眼前：说清是**拦截页**，并给出可执行的下一步
        self.assertIn("验证", out.error)
        self.assertIn("jvm_login", out.error)
        self.assertFalse(self.out.exists(), "拦截页不许落成源")

    def test_engine_error_reaches_add_result_without_zero_event_mask(self):
        with mock.patch("core.jvm_debug.page_from_engine",
                        side_effect=RuntimeError("IllegalStateException: profile locked")):
            out = self._run(_fake_fetch("CN"))
        self._case(out, 1, "引擎异常必须原样到达快速生成结果")
        self.assertIn("IllegalStateException: profile locked", out.error)
        self.assertNotIn("一条事件都没收到", out.error)

    def test_engine_html_is_used_when_ours_is_not_a_page(self):
        """引擎取回**真页面**时照常往下走（不是一遇到小材料就失败）。

        用真 fixture 当引擎的那份（它是有结果的搜索页），这样断言的是「换了材料之后
        确实拿它推了规则」，而不是「跑到了某个分支」。
        """
        with mock.patch("core.jvm_debug.page_from_engine", return_value=self.html):
            out = self._run(_fake_fetch("CN"))
        self._case(out, 0, "引擎那份是真页面 → 照常生成")
        src = json.loads(self.out.read_text(encoding="utf-8"))[0]
        self.assertTrue(src["ruleSearch"]["bookList"], src["ruleSearch"])
        self.assertTrue(src["ruleSearch"]["bookUrl"], src["ruleSearch"])


class RunAddJobReasonTests(unittest.TestCase):
    """job 那一层：`AddResult.error` 要**原样**进 result_json。

    这一层原来把它压成固定文案「生成失败」——于是「工具认不出这个页面」在界面上与
    「源坏了」一模一样（lessons §七十七）。**这条测试就是为那句压掉而写的。**
    """

    def test_reason_from_run_add_reaches_the_job_result(self):
        import asyncio

        from backend.api.ops import run_add_job
        from services.add_source import AddResult

        class FakeStore:
            def update_job(self, *a, **kw):
                pass

        with mock.patch("services.add_source.run_add",
                        return_value=AddResult(1, "抓不到搜索页 HTML（探测也失败了）")):
            r = asyncio.run(run_add_job("job-test", FakeStore(), {"url": URL}))

        self.assertFalse(r["ok"])
        self.assertEqual(r["return_code"], 1)
        self.assertIn("抓不到搜索页", r["error"], r)
        self.assertNotEqual(r["error"], "生成失败")


class VerifyWithEngineTests(unittest.TestCase):
    """生成完的验证走**本机引擎**（十-5），不再是本地回放器。

    两条要钉的：① 验的是引擎的结论（`source: "jvm"`，steps/all_ok 原样带回来）；
    ② **引擎不可用不推翻生成结果**——源照样生成，只是把「没验成 + 为什么」带回去
    （AGENTS #4：原因要走到用户眼前）。
    """

    def setUp(self) -> None:
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="quick_verify_"))
        self.out = self.root / "gen.json"
        self.html = FIXTURE.read_text(encoding="utf-8")
        self.calls = []

    def _run_job(self, engine_result):
        import asyncio

        from backend.api import ops
        from services.add_source import AddResult

        class FakeStore:
            def update_job(self, *a, **kw):
                pass

        def fake_engine(source, **kw):
            self.calls.append({"source": source, **kw})
            return engine_result

        with mock.patch("services.add_source.run_add",
                        return_value=AddResult(0)),                 mock.patch("services.add_source.fetch", side_effect=_fake_fetch(self.html)),                 mock.patch("core.analyzer.fetch", side_effect=_fake_fetch(self.html)),                 mock.patch("services.add_source._find_main_sources", return_value=[]),                 mock.patch("core.jvm_debug.run_jvm_debug", side_effect=fake_engine),                 mock.patch("core.build.load_sources",
                           return_value=[{"bookSourceName": "测试源",
                                          "bookSourceUrl": "http://example.com",
                                          "searchUrl": "http://example.com/search.php?q={{key}}"}]):

            return asyncio.run(ops.run_add_job("job-v", FakeStore(),
                                               {"url": URL, "verify": True}))

    def test_engine_is_called_and_its_steps_come_back(self):
        engine = {"source": "jvm",
                  "steps": [{"name": "search", "verdict": "pass", "values": ["甲"]}],
                  "pages": [{"id": "search", "html": "<html>整页</html>"}],
                  "all_ok": True, "events": [{"t": 0, "text": "x"}], "error": ""}
        r = self._run_job(engine)
        self.assertTrue(r["ok"])
        self.assertEqual(r["verify"]["source"], "jvm")
        self.assertEqual([s["name"] for s in r["verify"]["steps"]], ["search"])
        self.assertTrue(r["verify"]["all_ok"])
        # 打的是同一份源、关键词从 URL 里取
        self.assertEqual(self.calls[0]["key"], "绍宋")
        # 体积：事件流不进结果、证据原文被剥掉（它会写进 jobs 表 + 走 SSE）
        self.assertNotIn("events", r["verify"])
        self.assertEqual(r["verify"]["pages"], [], "页面列表整份是证据，剥掉")
        self.assertEqual(r["verify"]["steps"][0]["values"], [])

    def test_engine_unavailable_keeps_the_generated_source(self):
        """没配引擎 / 另一个任务在跑 / 零事件：**源照样给**，但要说清没验成。"""
        r = self._run_job({"source": "jvm", "steps": [], "pages": [], "all_ok": None,
                           "events": [], "error": "本机引擎不可用：先在设置里填 App 源码目录"})
        self.assertTrue(r["ok"], "生成结果不该被验证失败带走")
        self.assertTrue(r["verify"].get("skipped"))
        self.assertIn("本机引擎不可用", r["verify"]["error"])


if __name__ == "__main__":
    unittest.main()
