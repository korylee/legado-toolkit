# -*- coding: utf-8 -*-
"""AI 补全任务（``kind="agent_loop"``）的断言：过程写事件、调用写账本。

钉住的是「为什么它必须是个任务」那两条：**逐字段过程**要能从事件账本读出来
（界面吃流就有真进度），**模型调用的输入输出**要能从 ``llm.jsonl`` 按行号取回
（排查「它为什么这么说」）。模型与引擎都是假件，不联网。
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import tempfile
import unittest
from unittest import mock

from backend.jobs import agent_loop_job
from tests.support import (
    fake_rule_try,
    fake_suggest,
    one_step_job,
)

HTML = """<html><body><h1 class="title">甲书</h1></body></html>"""


def _store(*, html=HTML):
    return one_step_job(html, structured={"name": "甲书"})


def _patch_suggest(rule="class.title@text"):
    """假模型：带上调用账本要的字（model / prompt / response / usage）。"""
    return fake_suggest(rule, model="fake", response="{\"fields\":[]}",
                        usage={"prompt_tokens": 7})


def _patch_try():
    return fake_rule_try([{"ok": True, "values": ["甲书"], "count": 1, "error": ""}])


class AgentLoopJobTests(unittest.TestCase):

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.run_dir = self.tmp / "agent-job1"
        patcher = mock.patch.object(agent_loop_job, "run_dir_of",
                                    lambda _job_id: self.run_dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, payload=None):
        with mock.patch("core.repair.suggest.suggest_many", _patch_suggest()), \
             mock.patch("core.rule_try.rule_try", _patch_try()):
            return asyncio.run(agent_loop_job.run_agent_loop_job(
                "job1", _store(),
                payload or {"debug_job_id": "dbg1", "step": "bookUrl", "fields": []}))

    def _events(self):
        raw = (self.run_dir / "events.jsonl").read_text(encoding="utf-8")
        return [json.loads(line) for line in raw.splitlines() if line.strip()]

    def _llm(self):
        raw = (self.run_dir / "llm.jsonl").read_text(encoding="utf-8")
        return [json.loads(line) for line in raw.splitlines() if line.strip()]

    def test_process_events_are_written_per_field(self):
        """逐字段过程落事件账本：界面吃流才有真进度（不是「响应回来才知道」）。"""
        self._run()
        kinds = [e["kind"] for e in self._events()]
        self.assertIn("ai_started", kinds)
        self.assertIn("ai_family", kinds)
        self.assertIn("ai_accepted", kinds)
        accepted = [e for e in self._events() if e["kind"] == "ai_accepted"]
        self.assertTrue(all(e.get("field") and e.get("rule") for e in accepted),
                        "每一条落地都要带字段与规则——界面靠它逐行写进输入框")

    def test_llm_ledger_keeps_input_and_output(self):
        """模型调用的**输入输出原文**进 llm.jsonl：摘要列表不带全文，按 seq 单取。"""
        out = self._run()
        records = self._llm()
        self.assertTrue(records)
        first = records[0]
        self.assertIn("PROMPT-", first["prompt"])
        self.assertEqual(first["response"], "{\"fields\":[]}")
        self.assertEqual(first["usage"]["prompt_tokens"], 7)
        self.assertTrue(first["fields"], "这一次调用要问的字段")
        summary = agent_loop_job.llm_calls(self.run_dir)
        self.assertEqual(len(summary["items"]), len(records))
        self.assertNotIn("prompt", summary["items"][0], "列表不背全文")
        self.assertIn("PROMPT-", agent_loop_job.llm_call(self.run_dir, 1)["prompt"])
        self.assertIsNone(agent_loop_job.llm_call(self.run_dir, 99))
        # 任务结果体仍是前端写入用的形状（不带提示词那种大对象）
        self.assertIn("fields", out)
        self.assertIn("budget", out)

    def test_failure_reason_is_recorded_not_swallowed(self):
        """模型没配：原因原样进事件与预算（AGENTS #4），任务不装成功。"""
        async def fake_off(_ctx):
            return {"llm": "off", "fields": {}, "error": "没有可用的模型"}

        # `rule_try` 也要打桩（同 `_run`）：**免费段那次「引擎真跑」**会走
        # `core.rule_try` → `ensure()` —— 机器上没有热着的 daemon 时它会**真起一个**
        # （28s + 三个 JVM ≈1.2GB，实测；有热 daemon 时又变成复用，结论随机器而变）。
        with mock.patch("core.repair.suggest.suggest_many", fake_off), \
             mock.patch("core.rule_try.rule_try", _patch_try()):
            out = asyncio.run(agent_loop_job.run_agent_loop_job(
                "job1", _store(),
                {"debug_job_id": "dbg1", "step": "bookUrl", "fields": []}))
        self.assertIn("没有可用的模型", out["budget"]["model_stop"])
        stopped = [e for e in self._events() if e["kind"] == "ai_family_failed"]
        self.assertTrue(stopped, "整段停了要说得出为什么")


if __name__ == "__main__":
    unittest.main()
