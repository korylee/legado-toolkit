# -*- coding: utf-8 -*-
"""首屏五格决策：缺口唯一、fix 与 probe 分栏、AI 只认合格材料。

判据一份在 `core/agent_plan`、中文句子在前端按码取词——所以这里除了判据，还钉两条
跨语言契约：**缺口码逐个有句子**、**动作种类逐个有按钮词**（AGENTS #22⑤ 的同一条原则：
两份清单没法共享代码，只能靠测试拦住「各改一边」）。
"""
from __future__ import annotations

import asyncio
import pathlib
import re
import unittest

from backend.api.rules import agent_plan
from backend.schemas import AgentPlanRequest
from core.agent_context import build_agent_context
from core.agent_plan import (FIX_KINDS, FIX_TARGETS, GAP_CODES, PROBE_KINDS,
                             build_plan)

ROOT = pathlib.Path(__file__).resolve().parent.parent

#: 口袋漫画那一步的真实形状：正文规则为空、页面判到 L3、本机引擎没有运行时材料。
#: `values_count` 是引擎给的事实（`planBody` 一直带它）：0 条 = 规则没选中任何节点。
POCKET_COMIC = {
    "layer": "L3",
    "target": {"step": "content", "want": "media", "rule_empty": True},
    "step": {"verdict": "unknown", "values_count": 0,
             "page_id": "p1", "url": "https://a.com/c/1"},
    "page": {"present": True, "has_wanted": False,
             "stats": {"links": 7, "images": 2, "images_with_src": 0}},
    "channel": "jvm",
    "signals": {"can_suggest": True, "llm_ready": True},
    "capabilities": {"jvm_debug": True, "app_debug": True},
}


#: 本机引擎跑不了的那一段（WebView 能力边界）：规则非空、页面在、取值 0——与
#: 「规则一条都没选中」形状相同，区别只在调用方给了 `webview_unsupported` 这个事实。
WEBVIEW_STEP = {
    "layer": "L2",
    "target": {"step": "toc", "want": "link", "rule_empty": False},
    "step": {"verdict": "fail", "values_count": 0,
             "page_id": "p1", "url": "https://a.com/toc"},
    "page": {"present": True, "has_wanted": True,
             "stats": {"links": 3, "images": 0, "images_with_src": 0}},
    "channel": "jvm",
    "signals": {"can_suggest": True, "llm_ready": True},
}


def plan_for(snapshot, **changes):
    data = dict(snapshot)
    data.update(changes)
    return build_plan(build_agent_context(data))


class DecisionGapTests(unittest.TestCase):

    def test_webview_unsupported_wins_over_rule_gaps_and_probes(self):
        """本机跑不了的这一段：给「换通道取证」，不给「改规则」。

        它的语义与 `no_url` / `runtime_missing` 同族（本机引擎拿不到材料），所以必须
        排在 `no_hit` 之前——否则界面会说「改选择器」，用户照着改还是跑不了这一段。
        """
        plan = plan_for(WEBVIEW_STEP, webview_unsupported=True)
        self.assertEqual(plan["gap"]["code"], "webview_unsupported")
        self.assertEqual(plan["probe"], {"kind": "app"})
        self.assertIsNone(plan["fix"])
        self.assertIn("no_hit", [g["code"] for g in plan["deferred_gaps"]],
                      "规则类缺口要降级进折叠，不许抢主动作")

    def test_webview_unsupported_absent_means_no_such_gap(self):
        """没给这个事实就不许出现（读不到 ≠ 有，AGENTS #12），按原来的判据走。"""
        plan = plan_for(WEBVIEW_STEP)
        self.assertEqual(plan["gap"]["code"], "no_hit")

    def test_rule_empty_is_the_global_prefix(self):
        """规则为空压过一切：只留一条缺口，且不给取证、不给 AI。"""
        plan = plan_for(POCKET_COMIC)
        self.assertEqual(plan["gap"]["code"], "rule_missing")
        self.assertEqual([g["code"] for g in plan["deferred_gaps"]],
                         ["no_wanted_nodes", "runtime_missing"])
        self.assertEqual(plan["fix"], {"kind": "edit_rule", "target": "rule"})
        self.assertIsNone(plan["probe"], "规则没写，换通道也读不出东西")
        self.assertEqual(plan["ai"]["reason_code"], "rule_missing")

    def test_rule_empty_also_routes_to_editing_the_rule(self):
        """层策略也必须先认这一档：不然 L3 上会给出「先取运行时材料」。"""
        plan = plan_for(POCKET_COMIC)
        self.assertEqual(plan["action"], "edit_rule")
        self.assertEqual(plan["reason_code"], "rule_missing")

    def test_one_gap_only(self):
        plan = plan_for(POCKET_COMIC)
        codes = [plan["gap"]["code"]] + [g["code"] for g in plan["deferred_gaps"]]
        self.assertEqual(len(codes), len(set(codes)))

    def test_runtime_gap_splits_fix_and_probe(self):
        """解决是改源（按层模板），取证是换通道——两者不同栏。"""
        plan = plan_for(POCKET_COMIC, layer="L2",
                        target={"step": "search", "want": "list"},
                        page={"present": True, "has_wanted": True})
        self.assertEqual(plan["gap"]["code"], "runtime_missing")
        self.assertEqual(plan["fix"], {"kind": "edit_rule", "target": "layer",
                                       "layer": "L2"})
        self.assertEqual(plan["probe"], {"kind": "app"})

    def test_app_channel_gets_no_probe(self):
        """真机已经是最后一条通道，再给 probe 就只剩换回来。"""
        plan = plan_for(POCKET_COMIC, layer="L2", channel="app",
                        target={"step": "search", "want": "list"},
                        page={"present": True, "has_wanted": True},
                        step={"verdict": "unknown"})
        self.assertEqual(plan["gap"]["code"], "unknown")
        self.assertIsNone(plan["probe"])

    def test_l5_fix_is_login_not_webjs(self):
        plan = plan_for(POCKET_COMIC, layer="L5",
                        target={"step": "content", "want": "text"},
                        page={"present": True, "has_wanted": True})
        self.assertEqual(plan["gap"]["code"], "runtime_missing")
        self.assertEqual(plan["fix"], {"kind": "edit_rule", "target": "login"})

    def test_candidate_takes_the_primary_action(self):
        """L1 上规则取不中：把候选摆在主动作位（运行时那一档在 L1 不适用）。"""
        plan = plan_for(POCKET_COMIC, layer="L1",
                        target={"step": "search", "want": "list"},
                        page={"present": True, "has_wanted": True},
                        candidates=[{"rule": ".books .item@tag.a@href"}])
        self.assertEqual(plan["gap"]["code"], "no_hit")
        self.assertEqual(plan["fix"], {"kind": "apply_candidate",
                                       "rule": ".books .item@tag.a@href"})

    def test_stale_says_rerun_first(self):
        plan = plan_for({"layer": "L1", "target": {"step": "search", "want": "list"},
                         "step": {"verdict": "pass", "stale": True},
                         "channel": "jvm",
                         "page": {"present": True, "has_wanted": True}})
        self.assertEqual(plan["gap"]["code"], "stale")
        self.assertEqual(plan["fix"], {"kind": "rerun"})

    def test_missing_facts_are_not_read_as_false(self):
        """没给的事实不当成假（AGENTS #12）：空上下文只该有一条「层判不了」。"""
        plan = build_plan(build_agent_context({}))
        self.assertIsNone(plan["gap"])
        self.assertIsNone(plan["fix"])
        self.assertIsNone(plan["probe"])
        self.assertEqual(plan["action"], "stop_unsupported")

    def test_every_layer_gets_a_full_five_slot_plan(self):
        """L1–L4 都要能填满五格：**填不出格是缺陷**，不是可接受的空。"""
        for layer in ("L1", "L2", "L3", "L4"):
            with self.subTest(layer=layer):
                plan = plan_for(POCKET_COMIC, layer=layer,
                                target={"step": "search", "want": "list"},
                                page={"present": True, "has_wanted": True})
                for key in ("gap", "deferred_gaps", "fix", "probe", "ai"):
                    self.assertIn(key, plan, "%s 缺格：%s" % (layer, key))
                self.assertIsNotNone(plan["gap"], "%s 应当给得出缺口" % layer)
                self.assertTrue(plan["ai"]["material_kind"], "%s 缺 AI 材料形状" % layer)


class AiMaterialTests(unittest.TestCase):

    def test_l1_static_page_is_a_qualified_material(self):
        plan = plan_for(POCKET_COMIC, layer="L1", target={"step": "search", "want": "list"},
                        page={"present": True, "has_wanted": True},
                        step={"verdict": "unknown", "values_count": 0})
        self.assertTrue(plan["ai"]["eligible"])
        self.assertEqual(plan["ai"]["material_kind"], "page_html")

    def test_dynamic_layer_material_is_not_qualified(self):
        plan = plan_for(POCKET_COMIC, layer="L3", target={"step": "content", "want": "media"})
        self.assertFalse(plan["ai"]["eligible"])
        self.assertEqual(plan["ai"]["reason_code"], "material_mismatch")
        self.assertEqual(plan["ai"]["material_kind"], "runtime_object")

    def test_missing_model_is_a_setting_problem_not_a_material_one(self):
        plan = plan_for(POCKET_COMIC, layer="L1", target={"step": "search", "want": "list"},
                        page={"present": True, "has_wanted": True},
                        signals={"can_suggest": True, "llm_ready": False})
        self.assertEqual(plan["ai"]["reason_code"], "no_model")

    def test_login_wall_blocks_ai_before_the_model_check(self):
        plan = plan_for(POCKET_COMIC, layer="L1", target={"step": "search", "want": "list"},
                        page={"present": True, "has_wanted": True},
                        signals={"can_suggest": True, "llm_ready": True,
                                 "login_wall": True})
        self.assertEqual(plan["ai"]["reason_code"], "login_wall")


class AgentPlanRouteTests(unittest.TestCase):

    def test_endpoint_returns_the_same_plan_as_the_module(self):
        """端点只转交事实：同一份输入，直调模块与调端点的结论必须一致。"""
        body = AgentPlanRequest(**POCKET_COMIC)
        out = asyncio.run(agent_plan(body))
        self.assertEqual(out["gap"]["code"], "rule_missing")
        self.assertEqual(out, plan_for(POCKET_COMIC))


class FrontendWordTableParityTests(unittest.TestCase):
    """跨语言契约：后端出的**码与动作种类**，前端消费方必须逐项覆盖。

    2026-10-16 工作台减法：诊断卡随「诊断与建议」块退场，前端不再有 GAP_TEXT 词表
    （`frontend/src/utils/debugDecision.js` 已删）。这些断言转钉**后端自身的封闭性**：
    码与动作种类仍须互洽，等 Agent 侧新消费方出现时再补它的词表契约。
    """

    def test_gap_codes_and_action_kinds_stay_self_consistent(self):
        self.assertTrue(GAP_CODES, "缺口码表不应为空")
        self.assertTrue(FIX_KINDS, "fix 动作种类不应为空")
        self.assertTrue(PROBE_KINDS, "取证通道不应为空")


class ExecutionActionParityTests(unittest.TestCase):
    """执行动作词表的后端自洽：路由动作 ↔ 执行 kind ↔ 预算类。

    ``core/agent_actions`` 是权威。前端执行侧词表（`debugAction.ACTION_KINDS` /
    `agentBudget.js`）已随诊断卡退场删除；Agent 消费方重接时按同一原则补回契约。
    """

    def test_router_actions_are_all_known(self):
        """路由返回的每个 action 都在权威表里：拼写漂移当场红。"""
        from core import agent_actions

        for action in agent_actions.ACTIONS:
            self.assertIn(action, agent_actions.ACTION_BUDGET,
                          "路由动作缺预算类：%s" % action)

    def test_budget_classes_are_a_closed_vocabulary(self):
        """预算类只有三种：evidence-budget 的执行者按它计数，多一种就多一种漏计。"""
        from core import agent_actions

        for action, budget in agent_actions.ACTION_BUDGET.items():
            self.assertIn(budget, ("free", "evidence", "model_call"),
                          "%s 的预算类不在词表内：%s" % (action, budget))

    def test_execution_budget_expands_every_route_action(self):
        from core import agent_actions

        for action, kinds in agent_actions.EXECUTION_ACTIONS.items():
            # inspect_runtime / ask_user / stop_unsupported 这类「交模型/交人工/
            # 终止」的动作没有执行 kind 是合法的：它们不落到调试动作链上
            # （预算仍记 model_call/evidence，终止动作本身 free）。
            if action in ("inspect_runtime", "ask_user", "stop_unsupported"):
                continue
            self.assertTrue(kinds, "路由动作 %s 没有展开出执行 kind" % action)

    def test_model_call_actions_are_exactly_the_spending_ones(self):
        """发模型请求的动作与 /suggest-rule 的「花钱须显式触发」边界对齐：不多、不少。"""
        from core import agent_actions

        spending = sorted(a for a, b in agent_actions.ACTION_BUDGET.items()
                          if b == "model_call")
        self.assertEqual(spending, ["suggest_api_rule", "suggest_rule"])


if __name__ == "__main__":
    unittest.main()
