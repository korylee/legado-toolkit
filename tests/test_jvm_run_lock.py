# -*- coding: utf-8 -*-
"""跑批与调试**共用一把锁**（`core.jvm_debug.RUN_LOCK`）——接线测试。

守的是「调试在跑时点跑批」这件事：两条链都写 `appservice/args.properties`、
都 `--rerun` 同一个 `:app:testAppDebugUnitTest`。同时跑会互相踩（参数被改写、
两个 Gradle 抢同一份构建产物），而**踩坏的表现是「JVM 坏了」**——用户会去查
环境，实际只要等对方跑完。

这个保护原先只有调试侧有；`core/jvm_debug.py` 里那句「跑批与调试共用一把锁」
当时描述的是一个**并不存在**的保护（AGENTS #12 那类「读起来像存在的信号」）。
所以这里钉三件事：① 忙时不跑批、也不动参数文件；② 忙时不把**别人的锁**释放掉；
③ 跑完（含抛异常）一定归还，否则下一次永远「另一个任务在跑」。

**打桩下游、断言收到什么**（本仓库的接线测试写法）：真跑 Gradle 就不叫接线测试了。
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import shutil
import tempfile
import unittest
from unittest import mock

from backend.api import jvm as jvm_api
from core import jvm_debug


class _FakeStore:
    """挡住 `Store()`——测试**不许开真管理库**（那是用户的数据）。"""

    def __init__(self, *a, **kw) -> None:
        pass

    def close(self) -> None:
        pass


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="jvm_lock_"))
        self.addCleanup(shutil.rmtree, str(self.tmp), ignore_errors=True)
        agsvc = self.tmp / "appservice"
        agsvc.mkdir()
        # `_launcher()` 要它存在才肯往下走
        (agsvc / "legado-gradle.bat").write_text("@echo off\n", encoding="utf-8")
        self.agsvc = agsvc
        self.gradle_calls = 0
        self.fail_gradle = False

        for p in (
            mock.patch.object(jvm_api, "_AGSVC", agsvc),
            mock.patch.object(jvm_api, "data_dir", lambda: self.tmp / "data"),
            mock.patch.object(jvm_api, "Store", _FakeStore),
            mock.patch.object(jvm_api, "selftest", lambda repo: {"ok": True, "checks": []}),
            mock.patch.object(jvm_api, "_export_sources_file", self._fake_export),
            mock.patch.object(jvm_api, "_write_meta", lambda rows: "testbatch"),
            mock.patch.object(jvm_api, "_run_gradle", self._fake_gradle),
            mock.patch.object(jvm_api.settings_store, "load",
                              lambda: {"jvm": {"app_repo": "X:/repo", "keyword": "我",
                                               "timeout": 25, "concurrency": 8,
                                               "limit": 2, "depth": "search"}}),
        ):
            p.start()
            self.addCleanup(p.stop)

    def _fake_export(self, st, urls=None, filt=None) -> pathlib.Path:
        # 签名要与真的一致（跑批现在会传「只跑这几条」）：少了这个参数，
        # 打桩就成了「只有测试里才成立的那种函数」（实测踩过：TypeError）
        self.seen_urls = urls
        f = self.tmp / "batch_src.json"
        f.write_text(json.dumps([{"bookSourceUrl": "https://a.com"}]), encoding="utf-8")
        return f

    def _fake_gradle(self) -> int:
        self.gradle_calls += 1
        if self.fail_gradle:
            raise RuntimeError("Gradle 炸了")
        out = self.tmp / "data" / "app_probe" / "jvm_results.jsonl"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n".join(json.dumps({"url": "https://a.com/%d" % i, "state": "ok"})
                                 for i in range(3)), encoding="utf-8")
        return 0

    def _call(self) -> dict:
        return asyncio.run(jvm_api.jvm_run())

    def _args_file(self) -> pathlib.Path:
        return self.agsvc / "args.properties"


class BusyTests(_Base):
    """"另一个 JVM 任务在跑"时，跑批**什么都不许动**。"""

    def setUp(self) -> None:
        super().setUp()
        jvm_debug.RUN_LOCK.acquire()
        self.addCleanup(jvm_debug.RUN_LOCK.release)

    def test_busy_reports_reason_and_touches_nothing(self) -> None:
        r = self._call()
        self.assertFalse(r["started"])
        self.assertEqual(r["reason"], jvm_debug.BUSY_REASON)
        self.assertIn("另一个 JVM 任务在跑", r["reason"])
        # 一条命令都没发、参数文件也没被改写（改写了会把在跑的那个调试参数弄脏）
        self.assertEqual(self.gradle_calls, 0)
        self.assertFalse(self._args_file().exists())

    def test_busy_does_not_release_someone_elses_lock(self) -> None:
        """**释放别人的锁**是这类改动最容易犯的错：一次误释放之后，
        「忙」这个状态就再也挡不住并发了（两边都以为自己是唯一持有者）。"""
        self._call()
        self.assertTrue(jvm_debug.RUN_LOCK.locked())


class FreeTests(_Base):
    def test_runs_and_releases_the_lock(self) -> None:
        self.assertFalse(jvm_debug.RUN_LOCK.locked())
        r = self._call()
        self.assertTrue(r["started"])
        self.assertTrue(r["ok"])
        self.assertEqual(r["count"], 3)
        self.assertEqual(self.gradle_calls, 1)
        self.assertIn("keyword=我", self._args_file().read_text(encoding="utf-8"))
        # 归还了，否则界面上「跑批」从此永远说「另一个任务在跑」
        self.assertFalse(jvm_debug.RUN_LOCK.locked())

    def test_lock_is_released_even_when_gradle_raises(self) -> None:
        self.fail_gradle = True
        with self.assertRaises(RuntimeError):
            self._call()
        self.assertFalse(jvm_debug.RUN_LOCK.locked())


class SharedWordingTests(_Base):
    """同一把锁、同一个原因 → **同一句话**（两处各写一份必然在界面上分家）。"""

    def test_debug_path_says_the_same_thing(self) -> None:
        jvm_debug.RUN_LOCK.acquire()
        try:
            with mock.patch.object(jvm_debug, "_env_error", lambda: ""):
                out = jvm_debug.run_jvm_debug({"bookSourceUrl": "https://a.com"},
                                              out_path=str(self.tmp / "x.ndjson"))
        finally:
            jvm_debug.RUN_LOCK.release()
        self.assertEqual(out["error"], jvm_debug.BUSY_REASON)


if __name__ == "__main__":
    unittest.main()
