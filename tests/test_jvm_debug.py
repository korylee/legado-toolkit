# -*- coding: utf-8 -*-
"""A4 契约：本机引擎调试（`core/jvm_debug` + `POST /api/rules/jvm-debug`）。

**不跑 Gradle、不联网**：把「拉 Gradle」与「补抓页面」两步换成假的，只钉组装与降级
那部分逻辑——参数文件怎么写、结果怎么从 NDJSON/侧车拼出来、出错时怎么说。
Gradle 那条链本身由 CLI 验收（A1–A3 的判据，见 `data/app_probe/echo_server.py` 那套）。

为什么值得单独钉：这条链现在有**两个消费方**（CLI 与界面），拼装逻辑合在一处
（`core/jvm_debug`）就要有东西守着它——`steps`/`events` 的形状一变，前端抽屉与列表
两处都跟着坏，而那种坏看起来像前端 bug。
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import shutil
import tempfile
import threading
import unittest
from unittest import mock

from fastapi import HTTPException

from backend.api.rules import jvm_debug as jvm_debug_endpoint
from backend.schemas import JvmDebugRequest
from core import jvm_daemon, jvm_debug, jvm_direct

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
        self.assertNotIn("\r\n", t, "行尾必须是 LF——这是 git 跟踪的文件（同 agent-write-safety §三）")

    def test_cookie_line_when_given(self) -> None:
        jvm_debug._write_args("D:/x/src.json", "我", "D:/x/out.ndjson", 60, cookie="a=1; b=2")
        self.assertIn("cookie=a=1; b=2", self._text())


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
        self._patch("default_launcher", lambda notes: jvm_debug._run_launcher)
        self.launcher_calls = []

    def _fetch_pages(self, steps, source, proxy="", cache="auto"):
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
        """跑批与调试共用参数文件与 Gradle 任务：拿不到锁要**明说**，不排队。"""
        self._patch("_run_launcher", self._fake_launcher())
        jvm_debug.RUN_LOCK.acquire()
        try:
            r = self._run()
        finally:
            jvm_debug.RUN_LOCK.release()
        self.assertIn("另一个 JVM 任务在跑", r["error"])
        self.assertEqual(self.launcher_calls, [])

    def test_assembles_steps_events_and_reports_cookie_len(self) -> None:
        """真 fixture 走一遍：形状必须与设备通道同形（前端零改动接得上）。"""
        self._patch("_run_launcher", self._fake_launcher(
            events=self._fixture_events(),
            meta={"code": 0, "cookie_len": 11, "hint": "", "error": ""}))
        r = self._run()
        self.assertEqual(r["source"], "jvm")
        self.assertEqual(r["code"], 0)
        self.assertEqual(r["cookie_len"], 11)
        self.assertEqual(r["error"], "")
        self.assertTrue(r["steps"], "必须解析出分段")
        self.assertTrue(all({"name"} <= set(s) for s in r["steps"]))
        self.assertTrue(r["events"], "事件流要带上（抽屉的事件页签要用）")
        self.assertTrue(all({"t", "text"} <= set(e) for e in r["events"]),
                        "事件形状要与设备通道一致：[{t: 秒, text: 原文}]")
        self.assertEqual(len(r["pages"]), 1, "补抓要接上（抽屉靠它看源码）")

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

    def test_args_are_restored_after_the_run(self) -> None:
        """跑批与调试共用 `args.properties`：跑完必须还原（否则跑批参数被顶掉）。"""
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
        """**降落必须说出来**（D2）：常驻回落了却静默，用户只会觉得「也没快多少」，
        而真正的问题（daemon 起不来）永远没人发现。附注落在第一条 step 上——
        抽屉已经在渲染它（`.debug-notes`）。"""
        def fake_default(notes):
            notes.append("这次没能用常驻进程（DaemonError: 起不来），已改用 Gradle（启动慢一些）")
            return self._fake_launcher(events=self._fixture_events(), meta={"code": 0})

        self._patch("default_launcher", fake_default)
        r = self._run()
        notes = r["steps"][0]["notes"]
        self.assertTrue(any("没能用常驻进程" in n for n in notes), notes)
        self.assertTrue(r["steps"][0]["has_notes"])

    def test_cache_mode_is_forwarded_to_page_fetch(self) -> None:
        self._patch("_run_launcher", self._fake_launcher(
            events=self._fixture_events(), meta={"code": 0}))
        self._run(cache="refresh")
        self.assertEqual(self._patched_pages, ["refresh"])

    def test_no_launch_note_when_the_default_path_is_clean(self) -> None:
        """正常走常驻时**不许**多出一行附注：那是噪音，而且会让「抽屉里带黄点」
        失去意义（黄点必须等价于「这一步真有问题」）。"""
        def fake_default(notes):
            return self._fake_launcher(events=self._fixture_events(), meta={"code": 0})

        self._patch("default_launcher", fake_default)
        r = self._run()
        notes = r["steps"][0]["notes"]
        self.assertFalse([n for n in notes if "常驻" in n], notes)


class DefaultLauncherTests(unittest.TestCase):
    """默认拉起方式的选择（D2）：**dump 比 .kt 新**才敢用常驻/直起。"""

    def test_stale_dump_falls_back_to_gradle(self) -> None:
        """dump 比 .kt 旧 = 「这份类可能不是最新编译的」。常驻进程里跑的是**加载时那份
        类**，用它就会表现成「规则改了没生效」——像源的问题，其实是我们在跑旧代码。

        `load_dump` 一起打桩：**不打桩这条会假绿**——真实 dump 不在测试的工作目录里，
        `load_dump` 抛异常后也走回 Gradle，于是「把陈旧判定删掉」这个变异根本抓不住
        （实测：补上打桩才变红）。"""
        with mock.patch.object(jvm_direct, "dump_is_stale", lambda: True), \
             mock.patch.object(jvm_direct, "load_dump", lambda warn_stale=True: {"x": 1}):
            got = jvm_debug.default_launcher([])
        self.assertIs(got, jvm_debug._run_launcher, "类可能是旧的就必须走 Gradle（它会先编译）")

    def test_fresh_dump_prefers_the_daemon(self) -> None:
        notes: list = []
        with mock.patch.object(jvm_direct, "dump_is_stale", lambda: False), \
             mock.patch.object(jvm_direct, "load_dump", lambda warn_stale=True: {"x": 1}):
            got = jvm_debug.default_launcher(notes)
        self.assertIsNot(got, jvm_debug._run_launcher, "dump 新鲜就该走常驻")
        self.assertEqual(notes, [])

    def test_unreadable_dump_does_not_block_debugging(self) -> None:
        notes: list = []
        with mock.patch.object(jvm_direct, "dump_is_stale", lambda: False), \
             mock.patch.object(jvm_direct, "load_dump",
                               side_effect=OSError("dump 坏了")):
            got = jvm_debug.default_launcher(notes)
        self.assertIs(got, jvm_debug._run_launcher)
        self.assertTrue(any("dump" in n for n in notes), notes)

    def test_daemon_failure_falls_back_to_gradle_not_direct(self) -> None:
        """常驻半路死了 → **回落 Gradle**（不是直起）。

        直起同样依赖 dump 的新鲜度，而 Gradle 一定会先编译——这里要的是「无论如何都能
        跑对」，速度是次要的。**这条是变异验证逼出来的**：把 `fallback=_run_launcher`
        改成 `fallback=None`（退化成直起）原本一条测试都不红。
        """
        gradle_calls: list = []

        def fake_gradle(timeout_min=20):
            gradle_calls.append(1)
            return 0, 11.0, "", ""

        notes: list = []
        with mock.patch.object(jvm_direct, "dump_is_stale", lambda: False), \
             mock.patch.object(jvm_direct, "load_dump", lambda warn_stale=True: {"x": 1}), \
             mock.patch.object(jvm_daemon, "ensure",
                               side_effect=jvm_daemon.DaemonError("起不来")), \
             mock.patch.object(jvm_daemon, "params_from_args",
                               lambda *a, **kw: {"file": "a", "key": "b", "out": "c",
                                                 "timeout": 30, "cookie": ""}), \
             mock.patch.object(jvm_debug, "_run_launcher", fake_gradle):
            run = jvm_debug.default_launcher(notes)
            rc, _cost, _so, _se = run()
        self.assertEqual(gradle_calls, [1], "回落目标必须是 Gradle")
        self.assertEqual(rc, 0)
        self.assertTrue(any("Gradle" in n for n in notes), notes)


class GradleDumpRefreshTests(unittest.TestCase):
    def test_gradle_run_refreshes_the_dump(self) -> None:
        """**「dump 比 .kt 新」= 「这份类是新编译的」**——这条不变式是常驻链的判据，
        而它靠的是「每次 Gradle 跑测都顺手 dump 一份」。少了它，改一次 Kotlin 就会让
        常驻一直被挡在外面（除非有人记得手工 --refresh）。"""
        seen: dict = {}

        class _P:
            returncode = 0
            stdout = ""
            stderr = ""

        def fake_run(cmd, **kw):
            seen.update(kw)
            return _P()

        with mock.patch.object(jvm_debug.subprocess, "run", fake_run):
            jvm_debug._run_launcher()
        env = seen.get("env") or {}
        self.assertIn("LEGADO_TEST_JVM_ENV_OUT", env, "Gradle 那条没刷 dump")
        self.assertTrue(str(env["LEGADO_TEST_JVM_ENV_OUT"]).endswith("test_jvm_env.json"))


class EndpointTests(_RunCase):
    """端点只做入参校验与转交（同步阻塞的部分交给线程池，这里直调看形状）。"""

    def setUp(self) -> None:
        super().setUp()
        self.seen = {}

        def fake_run(source, key, timeout, cookie, cache, proxy="", out_path=""):
            self.seen.update(source=source, key=key, timeout=timeout, cookie=cookie, cache=cache)
            return {"source": "jvm", "steps": [{"name": "search", "ok": True}], "pages": [],
                    "all_ok": True, "events": [], "error": ""}

        self._patch_run = fake_run
        real = jvm_debug.run_jvm_debug
        jvm_debug.run_jvm_debug = fake_run            # 端点内部 `from core.jvm_debug import`
        self.addCleanup(setattr, jvm_debug, "run_jvm_debug", real)

    def _call(self, **kw):
        body = JvmDebugRequest(source=fake_source(), **kw)
        return asyncio.run(jvm_debug_endpoint(body))

    def test_endpoint_passes_params_through(self) -> None:
        r = self._call(key="斗破", timeout=90, cookie="a=1", cache="only")
        self.assertEqual(r["source"], "jvm")
        self.assertEqual(self.seen["key"], "斗破")
        self.assertEqual(self.seen["timeout"], 90)
        self.assertEqual(self.seen["cookie"], "a=1")
        self.assertEqual(self.seen["cache"], "only")

    def test_unknown_cache_is_400(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            self._call(cache="whatever")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_non_positive_timeout_is_400(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            self._call(timeout=0)
        self.assertEqual(ctx.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
