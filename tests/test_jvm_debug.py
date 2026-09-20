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

from fastapi import HTTPException

from backend.api.rules import jvm_debug as jvm_debug_endpoint
from backend.schemas import JvmDebugRequest
from core import jvm_debug

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

    def test_cache_mode_is_forwarded_to_page_fetch(self) -> None:
        self._patch("_run_launcher", self._fake_launcher(
            events=self._fixture_events(), meta={"code": 0}))
        self._run(cache="refresh")
        self.assertEqual(self._patched_pages, ["refresh"])


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
