# -*- coding: utf-8 -*-
"""调试任务（``kind="jvm_debug"``）：目录约定、事件账本、终态、lane 档位。

调试从"同步长轮询"变成任务之后，过程与状态都由**运行目录里的账本**给出（行号即游标，
消费端见 ``backend/api/job_timeline``）。所以这里钉的都是生产者那一侧：这次运行的材料
写到哪、引擎逐条 flush 的事件怎么搬进账本、终态落不落盘、以及它排的是**调试档**。

不跑 Gradle、不联网：执行体与数据库都打桩。
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import shutil
import tempfile
import unittest
from unittest import mock

from backend.jobs import jvm_debug_job
from backend.jobs import runner as job_runner
from core.store import Store


def _lines(path: pathlib.Path) -> list:
    text = path.read_text(encoding="utf-8")
    return [json.loads(l) for l in text.splitlines() if l.strip()]


class JvmDebugJobTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="jvm_debug_job_"))
        self.addCleanup(shutil.rmtree, str(self.tmp), ignore_errors=True)
        self.data = self.tmp / "data"
        self.db = str(self.tmp / "sources.sqlite3")
        for patch in (
            mock.patch.object(jvm_debug_job, "data_dir", lambda: self.data),
            # 相位/进度是短连接写的（runner.update_phase 自己开 Store）：不隔离它就会
            # 写进**真库**，而断言查的是临时库——表现是"相位没推进"，其实是打错了库
            mock.patch.object(job_runner, "Store", lambda *a, **kw: Store(self.db)),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def _run_dir(self, job_id: str) -> pathlib.Path:
        return jvm_debug_job.run_dir_of(job_id)

    @staticmethod
    def _ok_run(source, key, timeout, cookie, cache, proxy="", run_dir=None,
                keep_run_dir=False, job_id="", readiness_result=None, **kw):
        # 引擎那边是**逐条 flush** 的（DebugService.kt）：这里写一行就相当于"跑到了这一步"
        (run_dir / "debug.ndjson").write_text(
            json.dumps({"elapsed_ms": 120, "text": "≡获取成功:搜索"}) + "\n",
            encoding="utf-8")
        return {"source": "jvm", "steps": [{"name": "search", "ok": True}],
                "pages": [], "all_ok": True, "code": 0, "error": "",
                "events": [{"t": 0.12, "text": "≡获取成功:搜索"}]}

    def _run_job(self, job_id="debug-1", payload=None):
        payload = payload or {"source": {"bookSourceUrl": "https://a.com"}, "key": "我",
                              "timeout": 60}
        with Store(self.db) as st:
            st.create_job(job_id, "jvm_debug", payload=payload)
            return asyncio.run(jvm_debug_job.run_jvm_debug_job(job_id, st, payload))

    def test_run_dir_is_derived_from_the_job_id(self) -> None:
        """目录按任务号现算：提交时还不知道 job_id，而"目录属于哪个任务"既过期清理要
        用、观测端也要用（见 job_timeline._run_dir_of）。"""
        self.assertEqual(self._run_dir("abc"),
                         self.data / "app_probe" / "runs" / "debug-abc")

    def test_engine_events_land_in_the_account(self) -> None:
        with mock.patch("core.jvm_debug.run_jvm_debug", side_effect=self._ok_run):
            result = self._run_job()
        self.assertEqual(result["steps"][0]["name"], "search")
        events = _lines(self._run_dir("debug-1") / "events.jsonl")
        self.assertEqual(events[0]["kind"], "debug_started")
        self.assertEqual(events[-1]["kind"], "done")
        steps = [e for e in events if e["kind"] == "debug"]
        self.assertEqual(steps[0]["text"], "≡获取成功:搜索")
        self.assertEqual(steps[0]["t"], 0.12)
        # 拿到 lane 之后相位推进到 starting：运行条据此把「等引擎」换成「正在拉起引擎」
        with Store(self.db) as st:
            self.assertEqual(st.get_job_summary("debug-1")["phase"], "starting")

    def test_failure_marks_terminal_failed_with_reason(self) -> None:
        with mock.patch("core.jvm_debug.run_jvm_debug",
                        return_value={"error": "本机引擎不可用：填源码目录",
                                      "steps": [], "pages": [], "events": [],
                                      "all_ok": False, "code": 1}):
            self._run_job("debug-2")
        last = _lines(self._run_dir("debug-2") / "events.jsonl")[-1]
        self.assertEqual(last["kind"], "failed")
        self.assertIn("本机引擎不可用", last["reason"])

    def test_events_keep_the_engine_run_dir(self) -> None:
        """任务链**不清场**：时间线读的就是目录里的账本，清掉等于跑完看不见过程。"""
        with mock.patch("core.jvm_debug.run_jvm_debug", side_effect=self._ok_run):
            self._run_job("debug-3")
        self.assertTrue((self._run_dir("debug-3") / "events.jsonl").is_file())
        self.assertTrue((self._run_dir("debug-3") / "debug.ndjson").is_file())


class DebugSubmitTests(unittest.TestCase):
    """提交侧：lane 档位与 payload（执行体在另一个测试类里）。"""

    def test_submit_queues_on_the_debug_lane(self) -> None:
        from backend.api import rules
        from backend.schemas import JvmDebugRequest

        seen = {}

        def capture(kind, payload, **kw):
            seen.update(kind=kind, payload=payload)
            seen.update(kw)
            return "job-9"

        body = JvmDebugRequest(
            source={"bookSourceUrl": "https://a.com", "bookSourceName": "甲"},
            target="search", query="我")
        with mock.patch.object(rules.runner, "submit", side_effect=capture), \
             mock.patch.object(rules, "_debug_timeout_or_400", lambda t: 60), \
             mock.patch("core.jvm_env.readiness", return_value={"ok": True}), \
             mock.patch("core.settings_store.load", return_value={"jvm": {}}), \
             mock.patch("core.settings_store.resolve_proxy", return_value=""):
            got = asyncio.run(rules.jvm_debug(body))

        self.assertEqual(got["job_id"], "job-9")
        self.assertEqual(seen["kind"], "jvm_debug")
        self.assertEqual(seen["lane"], "jvm")
        # 调试档（0）要排在批量档（10）前面：写死成 batch 就白设了优先级
        self.assertEqual(seen["lane_kind"], "debug")
        self.assertEqual(seen["payload"]["key"], "我")
        self.assertEqual(seen["payload"]["source"]["bookSourceUrl"], "https://a.com")
        self.assertEqual(seen["payload"]["timeout"], 60)
        self.assertIn("readiness", seen["payload"])


if __name__ == "__main__":
    unittest.main()
