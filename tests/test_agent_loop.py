# -*- coding: utf-8 -*-
"""Agent 循环编排的离线测试：提议/自查的决策逻辑，不真调模型与引擎。

`suggest` 与 `rule_try` 都通过 monkeypatch 注入假实现——这里只验循环的
预算纪律、失败原因的透传，以及「可用的提议升格为普通建议」的判据
（AGENTS #3 的执行侧）。"""
from __future__ import annotations

import asyncio
import json
import unittest
from unittest import mock

from core import agent_loop, agent_actions

HTML = """<html><body>
<h1 class="title">甲书</h1>
<a class="chapter" href="/c/1">第1页</a>
<a class="chapter" href="/c/2">第2页</a>
</body></html>"""

SOURCE = {"bookSourceUrl": "http://example.com", "bookSourceName": "测试源"}


def _patch_suggest(rule="class.title@text"):
    """假 suggest_many：请求里的每个字段都返回同一条固定提议。

    真实形状是 ``{field: [candidates]}``（一族字段一次调用，单字段缺席 =
    没有候选）。"""
    async def fake(ctx):
        return {"llm": "ok",
                "fields": {f["field"]: [{"rule": rule, "why": "w"}]
                           for f in ctx.get("fields") or []}}
    return fake


def _patch_try(results):
    """假 rule_try：按调用次序返回 results 列表里的项。"""
    calls = {"n": 0}

    def fake(source, step, body, base, rule):
        r = results[min(calls["n"], len(results) - 1)]
        calls["n"] += 1
        return r
    return fake


class AgentLoopTests(unittest.TestCase):

    def test_good_proposal_keeps_try_result(self):
        """提议经自查取到值：**升格为普通建议**（rule 落位）并带 try_result——
        前端的写入只认 rule，AI 行不能另开一条路径。"""
        with mock.patch("core.repair.suggest.suggest_many", _patch_suggest()), \
             mock.patch("core.rule_try.rule_try",
                        _patch_try([{"ok": True, "values": ["甲书"], "count": 1,
                                     "error": ""}])):
            out = agent_loop.run_loop_sync(HTML, {}, SOURCE, "http://x", "bookUrl")
        rows = {r["field"]: r for r in out["fields"]}
        self.assertEqual(rows["name"]["ai"]["rule"], "class.title@text")
        self.assertEqual(rows["name"]["rule"], "class.title@text",
                         "可用的提议要落成 rule——否则前端写入路径读不到")
        self.assertNotIn("ref_only", rows["name"])
        self.assertTrue(rows["name"]["try_result"]["ok"])
        # 一族字段一次调用（8 个详情字段分三族），预算 3 次/轮正好三族各一次；
        # 同族 6 个字段的自查撞上 5 次的 rule_try 预算——这就是「一族一次调用」
        # 的真实代价，触顶要如实标出来
        self.assertEqual(out["budget"]["model_calls"], 3)
        self.assertFalse(out["budget"]["model_limit_reached"], "三族各一次，没超预算")
        self.assertTrue(out["budget"]["try_limit_reached"])

    def test_model_stop_reason_reaches_the_caller(self):
        """模型没配/失败：原因在 budget.model_stop 里，**不冒充「次数已用完」**
        （AGENTS #4：原因要一路走到用户眼前）；整段停了就不为后面的族再花钱。"""
        async def fake(_ctx):
            return {"llm": "off", "fields": {},
                    "error": "没有可用的模型：到「设置 → 模型」配置一个"}

        with mock.patch("core.repair.suggest.suggest_many", fake):
            out = agent_loop.run_loop_sync(HTML, {}, SOURCE, "http://x", "bookUrl")
        self.assertIn("没有可用的模型", out["budget"]["model_stop"])
        self.assertFalse(out["budget"]["model_limit_reached"], "一次调用都没花，不是「用完」")
        self.assertEqual(out["budget"]["model_calls"], 0)

    def test_failed_proposals_leave_no_rule_text(self):
        """提议全不中：该行**不带规则文本**，只留一句原因——已知不可用的规则
        落进输入框等于让用户先信一次假的。"""
        async def fake(ctx):
            return {"llm": "ok",
                    "fields": {f["field"]: [{"rule": "class.不存在@text", "why": "w"}]
                               for f in ctx.get("fields") or []}}

        with mock.patch("core.repair.suggest.suggest_many", fake), \
             mock.patch("core.rule_try.rule_try",
                        _patch_try([{"ok": False, "values": [], "count": 0,
                                     "error": "0 条"}])), \
             mock.patch("core.agent_loop._candidates_for_ai", lambda html, kind: []):
            out = agent_loop.run_loop_sync(HTML, {}, SOURCE, "http://x", "bookUrl")
        rows = {r["field"]: r for r in out["fields"]}
        self.assertNotIn("rule", rows["name"], "验不过就不出规则文本")
        self.assertNotIn("ai", rows["name"])
        self.assertTrue(rows["name"].get("ai_failed"))
        self.assertIn("都没选中", rows["name"].get("reason") or "")

    def test_fields_filter_limits_the_loop(self):
        """``fields`` 限制提议范围：单字段「AI 重议」只影响那一个字段。"""
        with mock.patch("core.repair.suggest.suggest_many", _patch_suggest()), \
             mock.patch("core.rule_try.rule_try",
                        _patch_try([{"ok": True, "values": ["甲书"], "count": 1,
                                     "error": ""}])):
            out = agent_loop.run_loop_sync(HTML, {}, SOURCE, "http://x",
                                           "bookUrl", fields=["author"])
        rows = {r["field"]: r for r in out["fields"]}
        self.assertEqual(set(rows), {"author"}, "过滤后只剩请求的字段")
        self.assertEqual(rows["author"]["rule"], "class.title@text")

    def test_bad_proposal_is_swapped_for_candidate(self):
        """提议取不到值：换候选头名；换的还取不到才不出规则文本。"""
        results = [
            {"ok": False, "values": [], "count": 0, "error": "0 条"},
            {"ok": True, "values": ["甲书"], "count": 1, "error": ""},
        ]
        with mock.patch("core.repair.suggest.suggest_many",
                        _patch_suggest("class.不存在@text")), \
             mock.patch("core.rule_try.rule_try", _patch_try(results)), \
             mock.patch("core.agent_loop._candidates_for_ai",
                        lambda html, kind: [{"rule": "class.title@text"}]):
            out = agent_loop.run_loop_sync(HTML, {}, SOURCE, "http://x", "bookUrl")
        rows = {r["field"]: r for r in out["fields"]}
        self.assertEqual(rows["name"]["ai"]["rule"], "class.title@text")
        self.assertIn("候选头名", rows["name"]["ai"]["why"])

    def test_try_budget_stops_and_flags(self):
        """rule_try 预算尽：剩余行标未自查（try_result=None），budget 说触顶。"""
        results = [{"ok": True, "values": ["v"], "count": 1, "error": ""}]
        with mock.patch("core.repair.suggest.suggest_many", _patch_suggest()), \
             mock.patch("core.rule_try.rule_try", _patch_try(results)):
            agent_actions_rules = agent_actions.RULE_TRY_PER_TURN
            with mock.patch.object(agent_actions, "RULE_TRY_PER_TURN", 0):
                out = agent_loop.run_loop_sync(HTML, {}, SOURCE, "http://x",
                                               "bookUrl")
            self.assertEqual(agent_actions_rules, 5)  # 权威值不被测试改动
        self.assertTrue(out["budget"]["try_limit_reached"])
        self.assertTrue(all(r.get("try_result") is None
                            for r in out["fields"] if r.get("rule")))

    def test_model_budget_limits_proposals(self):
        """模型预算收紧到 1 次：第一族提议完，后面的族不再提议（保持免费段的行），
        并把「触顶」如实标出来。"""
        with mock.patch("core.repair.suggest.suggest_many", _patch_suggest()), \
             mock.patch("core.rule_try.rule_try",
                        _patch_try([{"ok": True, "values": ["v"], "count": 1,
                                     "error": ""}])), \
             mock.patch.object(agent_actions, "MODEL_CALLS_PER_TURN", 1):
            out = agent_loop.run_loop_sync(HTML, {}, SOURCE, "http://x", "bookUrl")
        self.assertEqual(out["budget"]["model_calls"], 1)
        self.assertTrue(out["budget"]["model_limit_reached"])


class AgentLoopRouteTests(unittest.TestCase):
    """端点按 ``use_ai`` 分流：false 免费段同步返回，**不发模型请求**；
    true 提交成 ``agent_loop`` 任务（花钱动作由用户显式点，且要能看进度/重挂）。"""

    def _store(self):
        """最小 Store 替身：只要 get_job 返回任务行（material 从中取）。"""
        job = {
            "payload": json.dumps({"source": SOURCE}),
            "result_json": json.dumps({
                "steps": [{"name": "bookUrl", "page_id": "detail",
                           "structured": {"name": "甲书"}}],
                "pages": [{"id": "detail", "url": "http://x/d", "html": HTML}],
            }),
        }

        class _St:
            def get_job(self, _job_id):
                return job

        return _St()

    def _call(self, use_ai):
        from backend.api.rules import agent_loop
        from backend.schemas import AutofillRunRequest

        calls = {"model": 0, "engine": 0, "submit": []}

        async def fake_suggest(_ctx):
            calls["model"] += 1
            return {"llm": "off", "fields": {}, "error": "没有可用的模型"}

        def fake_try(*_args, **_kw):
            calls["engine"] += 1
            return {"ok": True, "values": ["甲书"], "count": 1, "error": ""}

        def fake_submit(kind, payload=None, **_kw):
            calls["submit"].append((kind, payload or {}))
            return "job-1"

        with mock.patch("core.repair.suggest.suggest_many", fake_suggest), \
                mock.patch("core.rule_try.rule_try", fake_try), \
                mock.patch("backend.api.rules.runner.submit", fake_submit):
            out = asyncio.run(agent_loop(
                AutofillRunRequest(job_id="j1", step="bookUrl", use_ai=use_ai),
                st=self._store()))
        return out, calls

    def test_free_segment_never_calls_the_model(self):
        out, calls = self._call(False)
        self.assertEqual(calls["model"], 0, "免费段不许发模型请求")
        self.assertEqual(calls["engine"], 0, "免费段不许拉引擎")
        self.assertEqual(calls["submit"], [], "免费段不起任务")
        self.assertIn("fields", out)
        self.assertNotIn("budget", out, "免费段没有预算概念")

    def test_ai_segment_submits_a_job(self):
        """AI 段提交任务并回任务号：材料按 debug 任务号现取（payload 只放引用），
        进度/取消/重挂走那条观测流。"""
        out, calls = self._call(True)
        self.assertEqual(len(calls["submit"]), 1)
        kind, payload = calls["submit"][0]
        self.assertEqual(kind, "agent_loop")
        self.assertEqual(payload.get("debug_job_id"), "j1")
        self.assertEqual(payload.get("step"), "bookUrl")
        self.assertEqual(out.get("job_id"), "job-1")
        self.assertEqual(calls["model"], 0, "模型调用在任务体里，端点不做")

    def test_ai_segment_passes_field_filter(self):
        """单字段「AI 重议」：fields 跟着进 payload（一颗按钮只影响一行）。"""
        from backend.api.rules import agent_loop
        from backend.schemas import AutofillRunRequest

        seen = {}

        def fake_submit(kind, payload=None, **_kw):
            seen.update(payload or {})
            return "job-2"

        with mock.patch("backend.api.rules.runner.submit", fake_submit):
            asyncio.run(agent_loop(
                AutofillRunRequest(job_id="j1", step="bookUrl", use_ai=True,
                                   fields=["author"]),
                st=self._store()))
        self.assertEqual(seen.get("fields"), ["author"])


if __name__ == "__main__":
    unittest.main()