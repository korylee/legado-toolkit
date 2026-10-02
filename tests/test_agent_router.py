# -*- coding: utf-8 -*-
"""Deterministic Layer routing tests: no model or execution side effects."""
from __future__ import annotations

import unittest

from core.agent_router import route_agent_context


class AgentRouterTests(unittest.TestCase):
    def _context(self, layer, **changes):
        context = {
            "layer": layer,
            "target": {"step": "content", "want": "text"},
            "capabilities": {"jvm_debug": True, "app_debug": True},
            "evidence_refs": [{"id": "evidence-001"}],
        }
        context.update(changes)
        return context

    def test_invalid_layer_stops_with_visible_reason(self):
        plan = route_agent_context({})
        self.assertEqual(plan["action"], "stop_unsupported")
        self.assertEqual(plan["reason_code"], "unsupported_layer")
        self.assertFalse(plan["requires_model"])
        self.assertTrue(plan["reason"])

    def test_invalid_layer_uses_unsupported_reason_and_canonical_layer(self):
        plan = route_agent_context({"layer": "l9"})
        self.assertEqual(plan["layer"], "L9")
        self.assertEqual(plan["reason_code"], "unsupported_layer")
        self.assertIsNone(plan["proposal"])

    def test_l1_candidate_uses_app_when_jvm_is_unavailable(self):
        plan = route_agent_context(self._context("L1", capabilities={"app_debug": True}, candidates=[
            {"kind": "text", "rule": ".content"},
        ]))
        self.assertEqual(plan["verification"]["method"], "app")

    def test_l3_runtime_plan_names_the_field_to_inspect(self):
        plan = route_agent_context(self._context("L3", runtime={
            "keys": ["params", "params.chapter_images"],
        }))
        self.assertEqual(plan["action"], "inspect_runtime")
        self.assertEqual(plan["runtime_field"], "params.chapter_images")
        self.assertIsNone(plan["proposal"])

    def test_l3_runtime_without_field_stays_manual(self):
        plan = route_agent_context(self._context("L3", runtime={"status": "available"}))
        self.assertEqual(plan["action"], "ask_user")
        self.assertEqual(plan["reason_code"], "insufficient_evidence")

    def test_l4_network_plan_uses_app_verification_when_available(self):
        plan = route_agent_context(self._context("L4", capabilities={"app_debug": True}, network=[
            {"method": "GET", "path_shape": "/api/books", "status": 200},
        ]))
        self.assertEqual(plan["verification"]["method"], "app")

        plan = route_agent_context(self._context("L1", candidates=[
            {"kind": "text", "rule": ".content", "count": 2},
        ]), model_available=False)
        self.assertEqual(plan["action"], "suggest_rule")
        self.assertEqual(plan["mode"], "deterministic")
        self.assertFalse(plan["requires_model"])
        self.assertEqual(plan["candidate"]["rule"], ".content")

    def test_l1_without_candidate_has_manual_fallback_without_model(self):
        plan = route_agent_context(self._context("L1"), model_available=False)
        self.assertEqual(plan["action"], "suggest_rule")
        self.assertEqual(plan["mode"], "deterministic")
        self.assertFalse(plan["requires_model"])
        self.assertTrue(plan["fallback"])

    def test_l1_unresolved_can_use_agent_only_when_enabled(self):
        plan = route_agent_context(self._context("L1"), model_available=True)
        self.assertEqual(plan["mode"], "agent")
        self.assertTrue(plan["requires_model"])

    def test_l2_prefers_runtime_engine(self):
        plan = route_agent_context(self._context("L2"))
        self.assertEqual(plan["action"], "run_jvm_debug")
        self.assertEqual(plan["reason_code"], "needs_runtime")
        self.assertEqual(plan["verification"]["method"], "jvm")

    def test_l3_prefers_runtime_inspection_when_material_exists(self):
        plan = route_agent_context(self._context("L3", runtime={"keys": ["params"]}))
        self.assertEqual(plan["action"], "inspect_runtime")
        self.assertEqual(plan["reason_code"], "needs_runtime")

    def test_l3_prefers_app_before_jvm_without_runtime_material(self):
        plan = route_agent_context(self._context("L3"))
        self.assertEqual(plan["action"], "run_app_debug")
        self.assertEqual(plan["reason_code"], "needs_app_debug")
        self.assertEqual(plan["verification"]["method"], "app")

    def test_l4_network_evidence_is_only_api_candidate_route(self):
        plan = route_agent_context(self._context("L4", network=[
            {"method": "GET", "path_shape": "/api/books", "status": 200},
        ]), model_available=False)
        self.assertEqual(plan["action"], "suggest_api_rule")
        self.assertFalse(plan["requires_model"])
        self.assertIn("接口", plan["reason"])

    def test_l4_without_network_evidence_collects_it_first(self):
        plan = route_agent_context(self._context("L4"))
        self.assertEqual(plan["action"], "run_jvm_debug")
        self.assertEqual(plan["reason_code"], "needs_network_evidence")

    def test_l5_always_requires_user_session_choice(self):
        plan = route_agent_context(self._context("L5", capabilities={
            "jvm_debug": True, "app_debug": True, "login_context": True,
        }))
        self.assertEqual(plan["action"], "ask_user")
        self.assertEqual(plan["reason_code"], "needs_login")
        self.assertFalse(plan["requires_model"])

    def test_missing_engine_is_explicit_not_silent(self):
        plan = route_agent_context(self._context("L2", capabilities={}))
        self.assertEqual(plan["action"], "ask_user")
        self.assertTrue(plan["fallback"])
        self.assertTrue(plan["reason"])


if __name__ == "__main__":
    unittest.main()
