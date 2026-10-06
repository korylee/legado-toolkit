# -*- coding: utf-8 -*-
"""jvm_run 跑完了进度还是 0/N——钉住 progress 的三个口径。

1. **终值**：读完结果后 progress = 实际产出结果行的条数。可能小于 total：
   有的源没产出结果行，进度停在真实位置，不硬拉满。
2. **失败路径不写进度**：jvm_run 的 done ≠ 校验完成（daemon 失败且不回退、
   Gradle 没产出文件，任务照样 done + ok:False）——那些路径 progress 保持 0。
3. **跑的过程中也在动**：results.jsonl 由 Kotlin 侧每条 flush（ValidateService
   的 writer 回调），轮询数完整行就是真实完成数，不是编出来的假进度。

不跑 Gradle、不联网：`_run_gradle` / daemon / `store_checks` / `_write_meta` 全打桩。
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import shutil
import tempfile
import time
import unittest
from unittest import mock

from backend.api import jvm as jvm_api
from backend.jobs import jvm_exec
from core.store import Store

_DUMP = {"workingDir": "C:/repo", "classpath": "x", "maxHeapSize": "3g",
         "environment": {}, "jvmArgs": [], "systemProperties": {},
         "javaHomeEnv": "C:/jdk"}


class JvmRunProgressTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="jvm_progress_"))
        self.addCleanup(shutil.rmtree, str(self.tmp), ignore_errors=True)
        self.db = str(self.tmp / "sources.sqlite3")

    def _payload(self, single: bool = False) -> dict:
        run_dir = self.tmp / "runs" / "batch-x"
        run_dir.mkdir(parents=True, exist_ok=True)
        source_file = run_dir / "sources.json"
        args_file = run_dir / "args.properties"
        out_path = run_dir / "results.jsonl"
        source_file.write_text("[]", encoding="utf-8")
        args_file.write_text("file=%s\nout=%s\n" % (source_file, out_path),
                             encoding="utf-8")
        return {
            "prep": {"started": True},
            "total": 2,
            "run_dir": str(run_dir),
            "source_file": str(source_file),
            "args_file": str(args_file),
            "out_path": str(out_path),
            "single": single,
            "allow_gradle_fallback": True,
        }

    def _create_job(self, total: int) -> None:
        with Store(self.db) as st:
            st.create_job("testjob", "jvm_run", total=total)

    def _job(self) -> dict:
        with Store(self.db) as st:
            return st.get_job("testjob")

    def _run(self, payload: dict, gradle=None, daemon=None) -> dict:
        if gradle is None:
            def gradle(args_path=None, runtime=None):
                return {"exit": 0, "stdout": "", "stderr": ""}
        if daemon is None:
            def daemon(dump, args_file):
                return {"code": 0, "cost_ms": 1, "error": ""}
        # runner 的阶段/进度更新走短连接（runner 自己的 Store）：必须落到这份
        # 临时库——落到真库的话 job 不存在、UPDATE 是 no-op，断言会恒 0。
        # prepare 判失败 + probe 恒 None：批量块确定走 Gradle
        with mock.patch.object(jvm_exec, "_run_gradle", gradle), \
             mock.patch.object(jvm_exec, "_write_meta", lambda rows: "testbatch"), \
             mock.patch("core.jvm_health.store_checks", lambda *a, **kw: 0), \
             mock.patch.object(jvm_exec, "_PROGRESS_POLL_INTERVAL", 0.05), \
             mock.patch.object(jvm_exec, "execution_readiness",
                               lambda dump=None: {"ok": True}), \
             mock.patch("core.jvm_direct.load_dump", return_value=dict(_DUMP)), \
             mock.patch("core.jvm_validate_daemon.run", daemon), \
             mock.patch("core.jvm_validate_daemon.prepare",
                        return_value={"outcome": "failed",
                                      "reason": "测试不启 daemon"}), \
             mock.patch("core.jvm_validate_daemon.probe", return_value=None), \
             mock.patch.object(jvm_api.runner, "Store",
                               lambda *a, **kw: Store(self.db)):
            return asyncio.run(
                jvm_exec.run_jvm_job("testjob", Store(self.db), payload))

    @staticmethod
    def _rows(out: pathlib.Path, urls: list) -> None:
        out.write_text("".join(
            json.dumps({"url": u, "state": "ok"}) + "\n" for u in urls),
            encoding="utf-8")

    def test_completed_run_reports_full_progress(self) -> None:
        payload = self._payload()
        self._create_job(2)

        def fake_gradle(args_path=None, runtime=None, **kwargs):
            self._rows(pathlib.Path(payload["out_path"]),
                       ["https://a.com", "https://b.com"])
            return {"exit": 0, "stdout": "", "stderr": ""}

        result = self._run(payload, gradle=fake_gradle)
        self.assertTrue(result["ok"])
        job = self._job()
        self.assertEqual(job["progress"], 2)
        self.assertEqual(job["total"], 2)

    def test_partial_output_reports_what_actually_finished(self) -> None:
        """只产出一条结果行 → 进度 1/2。进度是「实际完成几条」，不是硬拉满。"""
        payload = self._payload()
        self._create_job(2)

        def fake_gradle(args_path=None, runtime=None, **kwargs):
            self._rows(pathlib.Path(payload["out_path"]), ["https://a.com"])
            return {"exit": 0, "stdout": "", "stderr": ""}

        result = self._run(payload, gradle=fake_gradle)
        self.assertTrue(result["ok"])
        self.assertEqual(self._job()["progress"], 1)

    def test_gradle_failure_keeps_progress_at_zero(self) -> None:
        payload = self._payload()
        self._create_job(2)

        def failing_gradle(args_path=None, runtime=None, **kwargs):
            return {"exit": 1, "stdout": "", "stderr": "boom"}

        result = self._run(payload, gradle=failing_gradle)
        self.assertFalse(result["ok"])
        self.assertEqual(self._job()["progress"], 0)

    def test_daemon_failure_without_fallback_keeps_progress_at_zero(self) -> None:
        """single + 不许回退：任务 done 但一条没跑成，进度必须是 0 不是 1/1。"""
        payload = self._payload(single=True)
        payload["allow_gradle_fallback"] = False
        self._create_job(1)

        def broken_daemon(dump, args_file):
            raise RuntimeError("端口不可用")

        result = self._run(payload, daemon=broken_daemon)
        self.assertFalse(result["ok"])
        self.assertEqual(self._job()["progress"], 0)

    def test_progress_moves_while_the_batch_is_running(self) -> None:
        payload = self._payload()
        self._create_job(2)
        seen = {"mid": 0}

        def slow_gradle(args_path=None, runtime=None, **kwargs):
            out = pathlib.Path(payload["out_path"])
            self._rows(out, ["https://a.com"])
            # 等轮询把 1 写进任务表（间隔 0.05s，给足余量）再写第二条；
            # 轮询坏了这里 4 秒超时，下面的断言会带着原因红
            with Store(self.db) as st:
                for _ in range(200):
                    if (st.get_job("testjob").get("progress") or 0) >= 1:
                        seen["mid"] = 1
                        break
                    time.sleep(0.02)
            self._rows(out, ["https://a.com", "https://b.com"])
            return {"exit": 0, "stdout": "", "stderr": ""}

        result = self._run(payload, gradle=slow_gradle)
        self.assertTrue(result["ok"])
        self.assertEqual(seen["mid"], 1, "跑批期间轮询没把已完成条数写进进度")
        self.assertEqual(self._job()["progress"], 2, "终值以解析出的结果行为准")


if __name__ == "__main__":
    unittest.main()
