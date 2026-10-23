# -*- coding: utf-8 -*-
"""A4 契约：本机引擎调试（`core/jvm_debug` + `POST /api/rules/jvm-debug`）。

**不跑 Gradle、不联网**：把「拉 Gradle」与「补抓页面」两步换成假的，只钉组装与降级
那部分逻辑——参数文件怎么写、结果怎么从 NDJSON/侧车拼出来、出错时怎么说。

为什么值得单独钉：这条链的拼装逻辑合在一处（`core/jvm_debug`）就要有东西守着它——
`steps`/`events` 的形状一变，前端抽屉与列表两处都跟着坏，而那种坏看起来像前端 bug。
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import pathlib
import shutil
import tempfile
import threading
import unittest
from unittest import mock

from fastapi import HTTPException

from backend.api.rules import jvm_debug as jvm_debug_endpoint
from backend.jobs import runner as job_runner
from backend.schemas import JvmDebugRequest
from core import jvm_daemon, jvm_debug

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "appservice_debug_52shuku.ndjson"


def fake_source(url: str = "http://example.com") -> dict:
    return {"bookSourceName": "测试源", "bookSourceUrl": url, "bookSourceType": 0,
            "enabled": True, "searchUrl": url, "ruleSearch": {"bookList": ".b"}}


class _ArgsCase(unittest.TestCase):
    """参数文件测试的夹具：把 `ARGS` 指到临时目录（**别写进真 data/**）。"""

    def setUp(self) -> None:
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="jvm_debug_args_"))
        self._old = jvm_debug.ARGS
        jvm_debug.ARGS = self.root / "args.properties"

    def tearDown(self) -> None:
        jvm_debug.ARGS = self._old
        shutil.rmtree(self.root, ignore_errors=True)

    def _text(self) -> str:
        return (self.root / "args.properties").read_bytes().decode("utf-8")


class WriteArgsTests(_ArgsCase):
    def test_keys_and_lf_line_endings(self) -> None:
        jvm_debug._write_args("D:/x/src.json", "我", "D:/x/out.ndjson", 60)
        t = self._text()
        for k in ("file=", "key=", "out=", "timeout="):
            self.assertIn(k, t)
        self.assertNotIn("cookie=", t, "没给 cookie 就不该写这一行")
        self.assertNotIn("\r\n", t, "行尾必须是 LF——这是 git 跟踪的文件")

    def test_cookie_line_when_given(self) -> None:
        jvm_debug._write_args("D:/x/src.json", "我", "D:/x/out.ndjson", 60, cookie="a=1; b=2")
        self.assertIn("cookie=a=1; b=2", self._text())

    def test_free_text_values_are_properties_escaped(self) -> None:
        """读参数文件的是 Java `Properties.load`：反斜杠是转义符——规则里的 `\\d`
        不转义会被读成 `d`（静默改写，2026-10-20 审出）。"""
        jvm_debug._write_args("D:/x/src.json", "我", "D:/x/out.ndjson", 60,
                              extra=[("try_rule", "class.a@text##\\d+")])
        t = self._text()
        # 文件里必须是两个反斜杠（Java Properties 的转义写法）
        self.assertIn("try_rule=class.a@text##\\\\d+", t)
        self.assertNotIn("try_rule=class.a@text##\\d+", t,
                         "单个反斜杠会被 Properties.load 当转义符吃掉")


class WebviewUnsupportedShapeTests(unittest.TestCase):
    def test_keeps_valid_entries_including_empty_url(self) -> None:
        value = [{"code": "unsupported_html_only", "url": "", "phase": "matched"},
                 {"code": "unsupported_is_rule", "url": "https://例.test/a", "phase": "debug"}]
        self.assertEqual(jvm_debug.webview_unsupported(value), value)

    def test_non_list_and_invalid_entries_are_dropped(self) -> None:
        self.assertEqual(jvm_debug.webview_unsupported(True), [])
        self.assertEqual(jvm_debug.webview_unsupported(False), [])
        self.assertEqual(jvm_debug.webview_unsupported({}), [])
        self.assertEqual(jvm_debug.webview_unsupported([
            {"code": "", "url": "x", "phase": "debug"},
            {"code": "ok", "url": "x", "phase": "other"},
            {"code": "ok", "phase": "debug"},
            "not-an-object",
        ]), [])

    def test_preserves_code_and_url_without_coercing_them(self) -> None:
        value = [{"code": " custom-code ", "url": "  ", "phase": "debug"}]
        self.assertEqual(jvm_debug.webview_unsupported(value), value)

    def test_dropped_entries_are_logged_not_silent(self) -> None:
        """丢掉一条原因等于把它从用户眼前拿掉：条数必须进日志（AGENTS #4）。"""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            kept = jvm_debug.webview_unsupported([
                {"code": "unsupported_is_rule", "url": "x", "phase": "debug"},
                {"code": "", "url": "x", "phase": "debug"},
                "not-an-object",
            ])
        self.assertEqual(len(kept), 1)
        self.assertIn("2 条形状不对", out.getvalue())

    def test_absent_key_is_normal_and_silent(self) -> None:
        """侧车没这个键（None）是正常情形，不该刷日志。"""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(jvm_debug.webview_unsupported(None), [])
        self.assertEqual("", out.getvalue())


class _RunCase(unittest.TestCase):
    """跑一次调试的夹具：假 Gradle（写出 NDJSON/侧车）+ 假补抓（不联网）。"""

    def setUp(self) -> None:
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="jvm_debug_run_"))
        self.out = self.root / "out.ndjson"
        self._patch("ARGS", self.root / "args.properties")
        self._patch("_env_error", lambda: "")
        self._patched_pages = []
        self._patch("fetch_debug_pages", self._fetch_pages)
        #: 把「用哪个拉起方式」钉回 `_run_launcher`：它只是常驻不可用时的**回落**，
        #: 而本类里那几处桩打的是它——有 daemon 的机器上 `default_launcher` 会直接
        #: 用 daemon、桩整个被绕过，测试**真的跑一次调试**（实测：3 条断言失败、
        #: 还真的连了 example.com）。选常驻还是选 Gradle 由 `DefaultLauncherTests`
        #: 单独测（它打的是 dump 的桩），这里只关心「拉起之后的组装」。
        self._patch("default_launcher", lambda notes, **kw: jvm_debug._run_launcher)
        self.launcher_calls = []

    def _fetch_pages(self, steps, source, proxy="", cache="auto", engine_html=None):
        # 签名要跟真实现走（多一个 `engine_html`）——**漏了不会报错**：`run_jvm_debug`
        # 把补抓的异常吞掉（那是有意的：证据失败不带走判定），于是这里会静默变成空页
        self._patched_pages.append(cache)
        return [{"id": "detail", "html": "<html></html>"}]

    def _patch(self, name, value):
        old = getattr(jvm_debug, name)
        self.addCleanup(setattr, jvm_debug, name, old)
        setattr(jvm_debug, name, value)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _fake_launcher(self, events=None, meta=None, timeout_min=20):
        """替身：把「另一个进程会写的那两个文件」写出来。"""
        def run(timeout_min=20):
            self.launcher_calls.append(True)
            if events is not None:
                self.out.write_text(
                    "\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n",
                    encoding="utf-8", newline="\n")
            if meta is not None:
                pathlib.Path(str(self.out) + ".meta.json").write_text(
                    json.dumps(meta, ensure_ascii=False), encoding="utf-8", newline="\n")
            return 0, 1.5, "", ""
        return run

    @staticmethod
    def _fixture_events():
        return [json.loads(x) for x in FIXTURE.read_text(encoding="utf-8").splitlines() if x.strip()]

    def _run(self, **kw):
        return jvm_debug.run_jvm_debug(fake_source(), key="我", out_path=str(self.out), **kw)


class RenderAttributionTests(unittest.TestCase):
    """「这一页走没走渲染」判在**页**上，且要认得出带请求选项的页面地址。

    2026-10-21 实测的坑：渲染成功那一页的 `pages[].url` 是
    `…/1.html,{"webView":true}`，而引擎报的渲染地址是**裸地址** `…/1.html`——
    直接比字符串会把「渲染成功」判成「没渲染」，正好说反（AGENTS #4）。
    """

    def test_page_url_with_request_options_still_matches(self) -> None:
        out = {"rendered": 1, "render_url": "https://a.com/ch/1.html", "render_reason": ""}
        page = {"url": 'https://a.com/ch/1.html,{"webView":true}'}
        jvm_debug._annotate_render([page], out)
        self.assertIs(page["rendered"], True)

    def test_failed_render_keeps_the_reason_on_that_page(self) -> None:
        out = {"rendered": 0, "render_url": "https://a.com/ch/1.html",
               "render_reason": "browser_unavailable"}
        hit = {"url": "https://a.com/ch/1.html"}
        other = {"url": "https://a.com/other"}
        jvm_debug._annotate_render([hit, other], out)
        self.assertIs(hit["rendered"], False)
        self.assertEqual(hit["render_reason"], "browser_unavailable")
        self.assertNotIn("rendered", other, "别人的渲染不算到这一页头上")

    def test_failed_last_call_does_not_claim_rendered(self) -> None:
        """前一次渲染成功、最后一次失败：失败页不许被标成「已渲染」。

        `shadow_rendered` 是**成功计数**，`shadow_last_url` 是**最后一次调用**（可能失败）。
        只看计数会把失败页标成 True，界面据此说「已经渲染了」，而材料其实没回来。
        """
        out = {"rendered": 1, "render_url": "https://a.com/ch/1.html",
               "render_reason": "render_timeout"}
        page = {"url": "https://a.com/ch/1.html"}
        jvm_debug._annotate_render([page], out)
        self.assertIs(page["rendered"], False)
        self.assertEqual(page["render_reason"], "render_timeout")

    def test_render_js_lands_only_with_the_fact(self) -> None:
        hit = {"url": "https://a.com/ch/1.html"}
        jvm_debug._annotate_render(
            [hit], {"rendered": 1, "render_url": "https://a.com/ch/1.html",
                    "render_reason": "", "render_js": True})
        self.assertIs(hit["render_js"], True)
        self.assertEqual(hit["material_kind"], jvm_debug.MATERIAL_WEBJS_OUTPUT)
        # 老侧车没有 shadow_last_js_len：不写该键，种类只能判 unknown
        old = {"url": "https://a.com/ch/1.html"}
        jvm_debug._annotate_render(
            [old], {"rendered": 1, "render_url": "https://a.com/ch/1.html",
                    "render_reason": ""})
        self.assertNotIn("render_js", old)
        self.assertEqual(old["material_kind"], jvm_debug.MATERIAL_UNKNOWN)

    def test_every_page_gets_a_material_kind(self) -> None:
        """种类判到**每一页**上；没走渲染的页是 unknown，不许冒充 static_html。"""
        hit = {"url": "https://a.com/ch/1.html"}
        other = {"url": "https://a.com/search"}
        jvm_debug._annotate_render(
            [hit, other], {"rendered": 1, "render_url": "https://a.com/ch/1.html",
                           "render_reason": "", "render_js": False})
        self.assertEqual(hit["material_kind"], jvm_debug.MATERIAL_RENDERED_DOM)
        self.assertEqual(other["material_kind"], jvm_debug.MATERIAL_UNKNOWN)

    def test_absent_sidecar_facts_annotate_nothing(self) -> None:
        """读不到就不写渲染事实：不写 False 冒充「这次没渲染」（AGENTS #12）。"""
        page = {"url": "https://a.com/ch/1.html"}
        jvm_debug._annotate_render([page], {"rendered": None})
        self.assertNotIn("rendered", page)
        self.assertEqual(page["material_kind"], jvm_debug.MATERIAL_UNKNOWN,
                         "读不到 ≠ 静态页：种类只说 unknown")


class PlaceholderRuleTests(_RunCase):
    """正文规则为空时注入「探针规则」：只取材料，结论一律「未验证」。

    上游 `WebBook.getContent` 在 content 为空时**直接返回章节地址、不发请求**，于是正文页
    与运行时形状都拿不到。注入一条非空规则能把材料取回来，但它跑的不是源里的规则——
    所以结论必须改写，且「材料来自占位」这件事要留在结果里。
    """

    def _source_with(self, content: str) -> dict:
        src = fake_source()
        src["ruleContent"] = {"content": content}
        return src

    def _run_key(self, source: dict, key: str):
        return jvm_debug.run_jvm_debug(source, key=key, out_path=str(self.out))

    def test_content_key_with_empty_rule_injects_a_probe_rule(self) -> None:
        self._patch("_run_launcher", self._fake_launcher(
            events=self._fixture_events(), meta={"code": 0}))
        src = self._source_with("")
        before = json.dumps(src, ensure_ascii=False, sort_keys=True)
        got = self._run_key(src, "--https://example.com/ch/1.html")
        # 送给引擎的那份被注入了
        written = json.loads((self.root / "source.json").read_text(encoding="utf-8"))
        self.assertEqual(written[0]["ruleContent"]["content"], jvm_debug.PLACEHOLDER_CONTENT_RULE)
        # 结论改写：占位规则跑出来的判定不许冒充验证
        step = next((s for s in got["steps"] if s.get("name") == "content"), {})
        self.assertEqual(step.get("verdict"), "unknown")
        self.assertFalse(step.get("ok"))
        self.assertIn("临时占位规则", step.get("detail", ""))
        # 事实留在结果里（消费方据此知道这次正文没验证）
        self.assertEqual(got.get("placeholder_rule"), jvm_debug.PLACEHOLDER_CONTENT_RULE)
        # 调用方那份一个字都不许动
        self.assertEqual(json.dumps(src, ensure_ascii=False, sort_keys=True), before)

    def test_non_empty_rule_is_left_alone(self) -> None:
        self._patch("_run_launcher", self._fake_launcher(
            events=self._fixture_events(), meta={"code": 0}))
        got = self._run_key(self._source_with("body@html"), "--https://example.com/ch/1.html")
        written = json.loads((self.root / "source.json").read_text(encoding="utf-8"))
        self.assertEqual(written[0]["ruleContent"]["content"], "body@html")
        self.assertNotIn("placeholder_rule", got)

    def test_other_steps_are_not_injected(self) -> None:
        self._patch("_run_launcher", self._fake_launcher(
            events=self._fixture_events(), meta={"code": 0}))
        got = self._run_key(self._source_with(""), "我")
        written = json.loads((self.root / "source.json").read_text(encoding="utf-8"))
        self.assertEqual(written[0]["ruleContent"]["content"], "")
        self.assertNotIn("placeholder_rule", got)


class MaterialKindTests(unittest.TestCase):
    """材料种类：引擎交回的「整页」是 DOM、还是页面脚本的输出。

    实测（2026-10-21，同一页都声明 webView）：不写 webJs → 218461 字节含 `<html>/<img>`
    的真 DOM；写 webJs → 27150 字节、137 行图片地址、没有任何标签。两者都顶着「整页」
    这个名义进材料队列，所以种类必须由后端判、消费方读。
    """

    def test_kind_follows_render_fact_and_page_script(self):
        # 判据只看页上的引擎事实：这次渲染成没成 + 响应体是不是脚本求值结果
        self.assertEqual(
            jvm_debug.material_kind({"rendered": True, "render_js": True}),
            jvm_debug.MATERIAL_WEBJS_OUTPUT)
        self.assertEqual(
            jvm_debug.material_kind({"rendered": True, "render_js": False}),
            jvm_debug.MATERIAL_RENDERED_DOM)
        # 没渲染 = 浏览器没参与、脚本也没机会跑 → 静态页
        self.assertEqual(jvm_debug.material_kind({"rendered": False}),
                         jvm_debug.MATERIAL_STATIC)

    def test_unknown_when_render_facts_are_missing(self):
        """读不到渲染事实时不许说它是静态页（AGENTS #12）。

        三种「读不到」：整份侧车没有渲染键（老侧车 / 设备通道）、`rendered` 为空、
        以及渲染成功但老侧车没说这次传没传 JS（`render_js` 缺席）。
        """
        self.assertEqual(jvm_debug.material_kind({}), jvm_debug.MATERIAL_UNKNOWN)
        self.assertEqual(jvm_debug.material_kind({"rendered": None}),
                         jvm_debug.MATERIAL_UNKNOWN)
        self.assertEqual(jvm_debug.material_kind({"rendered": True}),
                         jvm_debug.MATERIAL_UNKNOWN)

    def test_our_own_fetch_is_a_static_page(self):
        """我们另抓的一份（`origin == "fetch"`）：浏览器没参与，一定是静态页。

        2026-10-23 实测（正文页截图）：引擎没交回这一页，界面显示「材料：另抓的一份」，
        而快验把 `@js:` 规则的 `result` 当成整页 HTML 用了——那一档必须拦得掉的依据就是它。
        """
        self.assertEqual(
            jvm_debug.material_kind({"origin": "fetch", "url": "https://a.com/1.html"}),
            jvm_debug.MATERIAL_FETCHED)
        # 引擎交回但没有任何渲染事实：那是「读不到」，不是「确认静态」
        self.assertEqual(jvm_debug.material_kind({"origin": "engine"}),
                         jvm_debug.MATERIAL_UNKNOWN)


class StaticNotesTests(_RunCase):
    """源级静态错配接进结果（`static_notes`）：判据在 `core.quality`，这里只钉「接上了」。

    起点是一次真实遗漏：`static_misconfig_notes` 早就写好、有单测，但**没有生产消费者**
    ——「配了 webJs 但 URL 规则没开 webView，这段 JS 不会生效」这句话没人看到（AGENTS #4）。
    """

    def _run_with(self, source: dict):
        return jvm_debug.run_jvm_debug(source, key="我", out_path=str(self.out))

    def test_webjs_without_webview_surfaces_the_note(self) -> None:
        self._patch("_run_launcher", self._fake_launcher(
            events=self._fixture_events(), meta={"code": 0}))
        src = fake_source()
        src["ruleContent"] = {"webJs": "result = params.x.join('\\n')"}
        got = self._run_with(src)
        self.assertTrue(got.get("static_notes"), "配了 webJs 却没开 webView 要说出来")
        self.assertIn("不会生效", " ".join(got["static_notes"]))

    def test_plain_source_has_no_static_notes(self) -> None:
        self._patch("_run_launcher", self._fake_launcher(
            events=self._fixture_events(), meta={"code": 0}))
        self.assertNotIn("static_notes", self._run_with(fake_source()))


class RunJvmDebugTests(_RunCase):
    def test_missing_book_source_url_is_reported(self) -> None:
        r = jvm_debug.run_jvm_debug({"bookSourceName": "没有 URL"}, out_path=str(self.out))
        self.assertIn("bookSourceUrl", r["error"])
        self.assertEqual(r["steps"], [])

    def test_env_error_is_reported_and_launcher_not_called(self) -> None:
        self._patch("_env_error", lambda: "本机引擎不可用：App 源码目录 —— 先在设置里填")
        r = self._run()
        self.assertIn("本机引擎不可用", r["error"])
        self.assertEqual(self.launcher_calls, [], "环境不可用就不该去拉 Gradle")

    def test_busy_when_another_jvm_task_holds_the_lock(self) -> None:
        """批量校验与调试共用参数文件与 Gradle 任务：拿不到锁要**明说**，不排队。"""
        self._patch("_run_launcher", self._fake_launcher())
        jvm_debug.RUN_LOCK.acquire()
        try:
            r = self._run()
        finally:
            jvm_debug.RUN_LOCK.release()
        self.assertIn("另一个本机引擎任务在跑", r["error"])
        self.assertEqual(self.launcher_calls, [])

    def test_assembles_steps_events_and_reports_cookie_len(self) -> None:
        """真 fixture 走一遍：形状必须与设备通道同形（前端零改动接得上）。"""
        self._patch("_run_launcher", self._fake_launcher(
            events=self._fixture_events(),
            meta={"code": 0, "cookie_len": 11, "cookie_note": "no_cookie_for_domain",
                  "hint": "", "error": "",
                  "network_events": 3, "network_types": "Fetch=1,XHR=1",
                  "network_drops": "mime=1"}))
        r = self._run()
        self.assertEqual(r["source"], "jvm")
        self.assertEqual(r["code"], 0)
        self.assertEqual(r["cookie_len"], 11)
        self.assertEqual(r["cookie_note"], "no_cookie_for_domain",
                         "「没带登录态」要说得出为什么（sidecar 一直有 note）")
        self.assertEqual(r["network_events"], 3)
        self.assertEqual(r["network_types"], "Fetch=1,XHR=1")
        self.assertEqual(r["network_drops"], "mime=1")
        self.assertEqual(r["error"], "")
        self.assertTrue(r["steps"], "必须解析出分段")
        self.assertTrue(all({"name"} <= set(s) for s in r["steps"]))
        self.assertTrue(r["events"], "事件流要带上（抽屉的事件页签要用）")
        self.assertTrue(all({"t", "text"} <= set(e) for e in r["events"]),
                        "事件形状要与设备通道一致：[{t: 秒, text: 原文}]")
        self.assertEqual(len(r["pages"]), 1, "补抓要接上（抽屉靠它看源码）")

    def test_render_facts_are_surfaced_and_absence_stays_empty(self) -> None:
        """渲染事实带出来；**侧车没有这几项时留 None，不补 0**。

        判据读的是 `shadow_rendered` / `shadow_last_url`：`origin == "engine"` 只说明 HTML
        是引擎取回来的，源没声明 webView 时它同样是静态页（2026-10-21 实测同一页
        48KB/rendered=0 vs 218KB/rendered=1）。补 0 会把「这份侧车没说」写成「这次没渲染」。
        """
        self._patch("_run_launcher", self._fake_launcher(
            events=self._fixture_events(),
            meta={"code": 0, "hint": "", "error": "", "shadow_rendered": 1,
                  "shadow_webview_calls": 2, "shadow_last_url": "https://a.com/ch/1.html",
                  "shadow_last_reason": ""}))
        got = self._run()
        self.assertEqual(got["rendered"], 1)
        self.assertEqual(got["webview_calls"], 2)
        self.assertEqual(got["render_url"], "https://a.com/ch/1.html")
        self.assertEqual(got["render_reason"], "")
        self.assertIsNone(got["render_js"], "侧车没说 js 长度 → None，不补 False")
        # 侧车带 js 长度：这次渲染的响应体是脚本求值结果（material_kind 的第二个事实）
        self._patch("_run_launcher", self._fake_launcher(
            events=self._fixture_events(),
            meta={"code": 0, "hint": "", "error": "", "shadow_rendered": 1,
                  "shadow_last_url": "https://a.com/ch/1.html",
                  "shadow_last_reason": "", "shadow_last_js_len": 137}))
        self.assertIs(self._run()["render_js"], True)
        # 同一份侧车里去掉渲染那几项：读不到就是读不到，不许变成 0 / 空串冒充事实
        self._patch("_run_launcher", self._fake_launcher(
            events=self._fixture_events(), meta={"code": 0, "hint": "", "error": ""}))
        blank = self._run()
        self.assertIsNone(blank["rendered"])
        self.assertIsNone(blank["webview_calls"])
        self.assertIsNone(blank["render_js"])

    def test_matched_html_from_the_sidecar_reaches_the_right_step(self) -> None:
        """第三期：侧车里的 `matched_html` 按**每段自己的 url + 段名**落到 steps 上。

        形状不对整块丢掉的闸门由 `test_app_debug.MatchedHtmlTests` 管，这里只钉跨模块那
        一段（Kotlin 写侧车 → 这里读 → `build_steps`）。fixture 里**详情页与目录页是同一个
        URL**（真实产出就是这样）：两条段各取自己那段名，不许互相串。
        """
        detail = "https://www.52shuku.net/wuxia/hqOD.html"
        self._patch("_run_launcher", self._fake_launcher(
            events=self._fixture_events(),
            meta={"code": 0, "hint": "", "error": "", "matched_html": {
                detail: {"toc": "<div id='chapter-grid'></div>",
                         "search": "<div class='leak'></div>"},
                "https://www.52shuku.net/so/search.php?q=斗破苍穹": {
                    "search": "<div class='book'></div>"},
            }}))
        r = self._run()
        by_name = {s["name"]: s for s in r["steps"]}
        self.assertEqual(by_name["toc"]["matched_html"], "<div id='chapter-grid'></div>")
        self.assertEqual(by_name["search"]["matched_html"], "<div class='book'></div>",
                         "同 URL 上的另一个段名不许串到搜索段来")
        self.assertEqual(by_name["bookUrl"]["matched_html"], "",
                         "详情段没有对应的取值规则（`bookUrl` 不在 MATCHED_STEP_NAMES 里）")

    def test_timeout_keeps_partial_result(self) -> None:
        """超时/截断：**部分结果不能丢**，状态写进第一条 step 的附注。"""
        self._patch("_run_launcher", self._fake_launcher(
            events=self._fixture_events(), meta={"code": 3}))
        r = self._run()
        self.assertTrue(r["steps"], "超时也要留下已收到的分段")
        notes = r["steps"][0].get("notes") or []
        self.assertTrue(any("部分结果" in n for n in notes), notes)

    def test_zero_events_surfaces_the_hint(self) -> None:
        self._patch("_run_launcher", self._fake_launcher(events=[], meta={
            "code": 2, "hint": "两种成因：源的 bookSourceUrl 不一致 / dispatcher 不对"}))
        r = self._run()
        self.assertEqual(r["steps"], [])
        self.assertIn("两种成因", r["error"])

    def test_zero_events_prefers_the_sidecar_error(self) -> None:
        self._patch("_run_launcher", self._fake_launcher(events=[], meta={
            # 兼容旧启动器仍写 code=2 的情况：error 是更具体的证据。
            "code": 2, "hint": "通用零事件提示",
            "error": "IllegalStateException: browser profile locked"}))
        r = self._run()
        self.assertEqual(r["steps"], [])
        self.assertIn("IllegalStateException: browser profile locked", r["error"])
        self.assertNotIn("通用零事件提示", r["error"])


    def test_args_are_restored_after_the_run(self) -> None:
        """批量校验与调试共用 `args.properties`：跑完必须还原（否则批量校验参数被顶掉）。"""
        jvm_debug.ARGS.write_text("file=keep\n", encoding="utf-8", newline="\n")
        self._patch("_run_launcher", self._fake_launcher(
            events=self._fixture_events(), meta={"code": 0}))
        self._run()
        self.assertEqual(jvm_debug.ARGS.read_text(encoding="utf-8"), "file=keep\n")

    def test_launcher_hook_replaces_gradle(self) -> None:
        """`launcher=` 是「拉起那一步」的替换口（D0 的直起、常驻 daemon 都从它接）。

        **是替换、不是并列**：给了它就不该再拉 Gradle——否则一次调试起两个 JVM，
        写的是同一份 `args.properties`、抢的是同一个 Gradle 任务，正是 RUN_LOCK
        要挡的那种互相踩。而它产出的两个文件必须照样被解析成同样的 steps/时长
        （参数拼装与结果解析只有一份，这才是「换个拉起方式」不改变结论的前提）。
        """
        gradle = []
        self._patch("_run_launcher",
                    lambda timeout_min=20: (gradle.append(1), (0, 9.9, "", ""))[1])
        direct = []
        events = self._fixture_events()

        def fake_direct(timeout_min=20):
            direct.append(True)
            self.out.write_text(
                "\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n",
                encoding="utf-8", newline="\n")
            pathlib.Path(str(self.out) + ".meta.json").write_text(
                json.dumps({"code": 0, "events": len(events)}), encoding="utf-8", newline="\n")
            return 0, 2.5, "", ""

        r = self._run(launcher=fake_direct)
        self.assertEqual(gradle, [], "给了 launcher 就不该再拉 Gradle")
        self.assertEqual(len(direct), 1)
        self.assertEqual([s["name"] for s in r["steps"]],
                         ["search", "bookUrl", "toc", "content"], "四段照样解析得出来")
        self.assertEqual(r["cost_sec"], 2.5, "时长要用**这条**拉起方式量的那个")

    def test_launch_notes_are_surfaced_on_the_first_step(self) -> None:
        """**降落必须说出来**（D2）：快速通道回落了却静默，用户只会觉得「也没快多少」，
        而真正的问题（daemon 起不来）永远没人发现。附注落在第一条 step 上——
        抽屉已经在渲染它（`.debug-notes`）。"""
        def fake_default(notes, **kwargs):
            notes.append("快速启动没走成（DaemonError: 起不来），这次用了慢速方式")
            return self._fake_launcher(events=self._fixture_events(), meta={"code": 0})

        self._patch("default_launcher", fake_default)
        r = self._run()
        notes = r["steps"][0]["notes"]
        self.assertTrue(any("快速启动没走成" in n for n in notes), notes)
        self.assertTrue(r["steps"][0]["has_notes"])

    def test_cache_mode_is_forwarded_to_page_fetch(self) -> None:
        self._patch("_run_launcher", self._fake_launcher(
            events=self._fixture_events(), meta={"code": 0}))
        self._run(cache="refresh")
        self.assertEqual(self._patched_pages, ["refresh"])

    def test_no_launch_note_when_the_default_path_is_clean(self) -> None:
        """正常走常驻时**不许**多出一行附注：那是噪音，而且会让「抽屉里带黄点」
        失去意义（黄点必须等价于「这一步真有问题」）。"""
        def fake_default(notes, **kwargs):
            return self._fake_launcher(events=self._fixture_events(), meta={"code": 0})

        self._patch("default_launcher", fake_default)
        r = self._run()
        notes = r["steps"][0]["notes"]
        self.assertFalse([n for n in notes if "常驻" in n], notes)


class PageFromEngineErrorTests(unittest.TestCase):
    def test_page_from_engine_surfaces_the_sidecar_error(self) -> None:
        from core.jvm_debug import page_from_engine

        with mock.patch("core.jvm_debug.run_jvm_debug", return_value={
            "pages": [], "error": "本机引擎异常：IllegalStateException: profile locked",
        }):
            with self.assertRaises(RuntimeError) as ctx:
                page_from_engine("https://example.com/search?q=我")
        self.assertIn("IllegalStateException: profile locked", str(ctx.exception))


class DefaultLauncherTests(unittest.TestCase):
    """默认拉起方式：daemon（Gradle boot）优先，失败回落 Gradle。

    原先的「dump 比 .kt 旧就不用常驻」判定随直起一起退场：daemon 的 boot 经
    Gradle 必编译，进程里跑的永远是当前源码——旧字节码的失败模式结构性消失。
    """

    def test_daemon_failure_falls_back_to_gradle_not_direct(self) -> None:
        """常驻半路死了 → **回落 Gradle**（不是直起）。

        Gradle 一定会先编译——这里要的是「无论如何都能跑对」，速度是次要的。
        **这条是变异验证逼出来的**：把 `fallback=_run_launcher`
        改成 `fallback=None`（退化成直起）原本一条测试都不红。
        """
        gradle_calls: list = []

        def fake_gradle(timeout_min=20):
            gradle_calls.append(1)
            return 0, 11.0, "", ""

        notes: list = []
        with mock.patch.object(jvm_daemon, "ensure",
                               side_effect=jvm_daemon.DaemonError("起不来")), \
             mock.patch.object(jvm_daemon, "params_from_args",
                               lambda *a, **kw: {"file": "a", "key": "b", "out": "c",
                                                 "timeout": 30, "cookie": ""}), \
             mock.patch.object(jvm_debug, "_run_launcher", fake_gradle):
            run = jvm_debug.default_launcher(notes)
            rc, _cost, _so, _se = run()
        self.assertEqual(gradle_calls, [1], "回落目标必须是 Gradle")
        self.assertEqual(rc, 0)
        # 提示不区分回落叫什么（用户只需要知道「这次慢」），
        # 「落没落到 Gradle」由上面的 gradle_calls 断言守
        self.assertTrue(any("快速启动没走成" in n for n in notes), notes)


class EndpointTests(_RunCase):
    """端点做入参校验，然后**提交成任务**（执行体在 `jvm_debug_job`，这里只看形状）。

    调试不再是同步长轮询：端点返回任务号，过程与结果从那条观测流 / 明细里看。
    所以这里钉的是"参数有没有原样进 payload"——校验与转交这两件事各只有一处。
    """

    def setUp(self) -> None:
        super().setUp()
        self.seen = {}

        def capture(kind, payload, **kw):
            self.seen.update(payload)
            self.seen["kind"] = kind
            return "debug-job"

        self._submit = mock.patch.object(job_runner, "submit", side_effect=capture)
        self._submit.start()
        self.addCleanup(self._submit.stop)

    def _call(self, **kw):
        body = JvmDebugRequest(source=fake_source(), **kw)
        return asyncio.run(jvm_debug_endpoint(body))

    def test_endpoint_submits_a_debug_job_with_the_params(self) -> None:
        r = self._call(target="toc", query="++https://a.test/t", timeout=90,
                       cookie="a=1", cache="only")
        self.assertEqual(r["job_id"], "debug-job")
        self.assertEqual(self.seen["kind"], "jvm_debug")
        self.assertEqual(self.seen["key"], "++https://a.test/t")
        self.assertEqual(self.seen["target"], "toc")
        self.assertEqual(self.seen["query"], "++https://a.test/t")
        self.assertFalse(self.seen["key_override"])
        self.assertEqual(self.seen["timeout"], 90)
        self.assertEqual(self.seen["cookie"], "a=1")
        self.assertEqual(self.seen["cache"], "only")
        self.assertIsNotNone(self.seen["readiness"])

    def test_busy_jvm_lane_returns_conflict_instead_of_queueing_debug(self) -> None:
        message = "校验引擎正忙，本次调试未排队，请稍后重试"
        with mock.patch.object(job_runner, "submit",
                               side_effect=job_runner.JvmLaneBusyError(message)):
            with self.assertRaises(HTTPException) as ctx:
                self._call(target="search", query="词")
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(ctx.exception.detail, message)

    def test_unknown_cache_is_400(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            self._call(cache="whatever")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_non_positive_timeout_is_400(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            self._call(timeout=0)
        self.assertEqual(ctx.exception.status_code, 400)

    def test_timeout_defaults_from_settings(self) -> None:
        """不传 timeout → 吃设置里的调试预算（AGENTS #8：默认值只有 settings_store
        一份；前端曾写死 60，与桥的渲染上限同值互相掐死）。

        这份桩里没有 `stage_timeout`，按段取就回落到 `chain_timeout`——
        「按段取」本身由下面那条钉。"""
        from core import settings_store
        fake = {"network": {"proxy": ""},
                "jvm": {"app_repo": "", "android_sdk_dir": ""},
                "debug": {"chain_timeout": 120}}
        with mock.patch.object(settings_store, "load", return_value=fake), \
                mock.patch("core.jvm_env.readiness",
                           return_value={"ok": True, "checks": []}):
            self._call()
        self.assertEqual(self.seen["timeout"], 120)

    def test_timeout_without_value_takes_the_stage_budget(self) -> None:
        """不传 timeout：**指定了单步目标就按段取**（`debug.stage_timeout[那一段]`），
        只有跑整链（target 空）才吃 `debug.chain_timeout`。

        各段共享一根预算时，慢的搜索段会把正文段挤掉——调试是单源、有人盯着，
        分段之后「前面慢」不再惩罚「后面能不能验」（`settings_store.debug_stage_timeout`）。"""
        from core import settings_store
        fake = {"network": {"proxy": ""},
                "jvm": {"app_repo": "", "android_sdk_dir": "", "keyword": "我"},
                "debug": {"chain_timeout": 120,
                          "stage_timeout": {"search": 30, "explore": 30, "info": 31,
                                            "toc": 60, "content": 90}}}
        with mock.patch.object(settings_store, "load", return_value=fake), \
                mock.patch("core.jvm_env.readiness",
                           return_value={"ok": True, "checks": []}):
            self._call(target="toc", query="++https://a.test/t")
            self.assertEqual(self.seen["timeout"], 60, "单步目标吃那一段的预算")
            self._call(key="https://x.example/b/1", target="")
            self.assertEqual(self.seen["timeout"], 120,
                             "整链（没有具体步骤）才吃 chain_timeout")

    def test_timeout_out_of_range_is_400_not_clamped(self) -> None:
        """显式传值越界要 400 说清区间——静默夹的话用户不知道设的数没生效。"""
        with self.assertRaises(HTTPException) as ctx:
            self._call(timeout=500)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("300", str(ctx.exception.detail))

    @staticmethod
    def _settings(keyword: str) -> dict:
        return {"network": {"proxy": ""},
                "jvm": {"app_repo": "", "android_sdk_dir": "", "keyword": keyword},
                "debug": {"chain_timeout": 120}}

    def test_target_and_query_build_the_key(self) -> None:
        """语义化入参 → `core.debug_keys` 拼装（App 的 key 语法只有后端一份）；
        ``key`` 非空时优先（手工覆盖口）。"""
        from core import settings_store

        with mock.patch.object(settings_store, "load",
                               return_value=self._settings("斗破")), \
                mock.patch("core.jvm_env.readiness",
                           return_value={"ok": True, "checks": []}):
            self._call(target="toc", query="++https://a.com/t")
            self.assertEqual(self.seen["key"], "++https://a.com/t")
            self._call()
            self.assertEqual(self.seen["key"], "斗破",
                             "详情/目录/正文留空回落搜索起步，关键词吃设置")
            self._call(key="https://x.example/b/1")
            self.assertEqual(self.seen["key"], "https://x.example/b/1")

    def test_explore_without_url_is_400(self) -> None:
        """发现没得回落 → 400 带原因，不静默换目标（AGENTS #4）。"""
        from core import settings_store

        src = fake_source()
        src["exploreUrl"] = ""
        with mock.patch.object(settings_store, "load",
                               return_value=self._settings("斗破")),                mock.patch("core.jvm_env.readiness",
                           return_value={"ok": True, "checks": []}):
            with self.assertRaises(HTTPException) as ctx:
                self._call(target="explore", query="")
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("exploreUrl", str(ctx.exception.detail))


class DebugRunManifestTests(unittest.TestCase):
    """调试任务自己的运行目录：manifest.json 落盘；成功清理、崩溃保留现场。"""

    def setUp(self) -> None:
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="jvm_debug_manifest_"))
        self.runs = self.root / "app_probe" / "runs"
        self._patches = []

    def tearDown(self) -> None:
        for name, old in self._patches:
            setattr(jvm_debug, name, old)
        shutil.rmtree(self.root, ignore_errors=True)

    def _patch(self, name, value):
        self._patches.append((name, getattr(jvm_debug, name)))
        setattr(jvm_debug, name, value)

    def _run(self, launcher):
        self._patch("ARGS", self.root / "args.properties")
        self._patch("_env_error", lambda: "")
        self._patch("data_path", lambda *parts: self.root.joinpath(*parts))
        self._patch("fetch_debug_pages", lambda steps, source, **kw: [])
        return jvm_debug.run_jvm_debug(fake_source(), key="我", launcher=launcher)

    def _fake_launcher(self, events):
        def run():
            # owns_run_dir 模式下参数文件在运行目录里，路径记在 manifest 上——
            # 从那里读也算顺带验了「manifest 指路可用」
            manifest_dir = next(self.runs.glob("debug-*"))
            args = (manifest_dir / "args.properties").read_text(encoding="utf-8")
            out = pathlib.Path(next(line.split("=", 1)[1] for line in args.splitlines()
                                    if line.startswith("out=")))
            out.write_text("\n".join(json.dumps(e, ensure_ascii=False)
                                     for e in events) + "\n",
                           encoding="utf-8", newline="\n")
            pathlib.Path(str(out) + ".meta.json").write_text(
                json.dumps({"code": 0}), encoding="utf-8")
            return 0, 1.0, "", ""
        return run

    def test_success_cleans_run_dir(self) -> None:
        out = self._run(self._fake_launcher(
            [json.loads(x) for x in FIXTURE.read_text(encoding="utf-8").splitlines()
             if x.strip()]))
        self.assertTrue(out["steps"])
        self.assertEqual(list(self.runs.glob("debug-*")), [])

    def test_crash_keeps_run_dir_with_manifest(self) -> None:
        def boom():
            raise RuntimeError("炸了")

        with self.assertRaises(RuntimeError):
            self._run(boom)
        dirs = list(self.runs.glob("debug-*"))
        self.assertEqual(len(dirs), 1, "崩溃现场被清掉了")
        envelope = json.loads((dirs[0] / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(envelope["kind"], "jvm_debug")
        self.assertEqual(envelope["source_url"], fake_source()["bookSourceUrl"])
        self.assertEqual(envelope["key"], "我")
        for key in ("args", "source", "events"):
            self.assertIn(key, envelope["paths"])


if __name__ == "__main__":
    unittest.main()
