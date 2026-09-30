# -*- coding: utf-8 -*-
"""AgentProposal protocol tests: validation only, never execution."""
from __future__ import annotations

import json
import unittest

from core.agent_actions import (
    ACTIONS,
    AgentProposalError,
    parse_agent_proposal,
    validate_agent_proposal,
)


class AgentActionSchemaTests(unittest.TestCase):
    def _payload(self, **changes):
        payload = {
            "action": "suggest_rule",
            "layer": "L1",
            "reason_code": "candidate_available",
            "proposal": {"field": "ruleSearch.bookList", "rule": ".books .item"},
            "verification": {"required": True, "method": "jvm"},
            "evidence_refs": ["evidence-001"],
        }
        payload.update(changes)
        return payload

    def test_all_actions_have_a_strict_valid_shape(self):
        proposals = {
            "suggest_rule": {"field": "ruleSearch.bookList", "rule": ".item"},
            "run_jvm_debug": {"target": "search", "query": "关键词"},
            "run_app_debug": {"target": "content"},
            "inspect_runtime": {"runtime_field": "params.chapter_images"},
            "suggest_api_rule": {
                "field": "ruleContent.content", "rule": "$.data[*].text",
                "method": "GET", "path": "/api/books", "json_path": "$.data",
            },
            "ask_user": {"question": "请选择运行通道", "options": ["本机", "App"]},
            "stop_unsupported": {},
        }
        reasons = {
            "suggest_rule": "candidate_available",
            "run_jvm_debug": "needs_jvm_debug",
            "run_app_debug": "needs_app_debug",
            "inspect_runtime": "needs_runtime",
            "suggest_api_rule": "needs_network_evidence",
            "ask_user": "user_confirmation_required",
            "stop_unsupported": "unsupported_layer",
        }
        for action in ACTIONS:
            with self.subTest(action=action):
                result = validate_agent_proposal(
                    self._payload(action=action, reason_code=reasons[action], proposal=proposals[action]),
                    available_evidence_refs={"evidence-001"},
                )
                self.assertEqual(result["action"], action)
                self.assertTrue(result["verification"]["required"])

    def test_each_layer_is_accepted_without_reimplementing_routing(self):
        for layer in ("L1", "L2", "L3", "L4", "L5"):
            with self.subTest(layer=layer):
                result = validate_agent_proposal(
                    self._payload(layer=layer), available_evidence_refs={"evidence-001"}
                )
                self.assertEqual(result["layer"], layer)

    def test_unknown_action_is_rejected(self):
        with self.assertRaises(AgentProposalError) as ctx:
            validate_agent_proposal(self._payload(action="save_source"))
        self.assertEqual(ctx.exception.code, "unknown_action")

    def test_non_json_model_output_is_rejected(self):
        with self.assertRaises(AgentProposalError) as ctx:
            parse_agent_proposal("I think you should change the rule")
        self.assertEqual(ctx.exception.code, "invalid_json")

    def test_unknown_top_level_and_proposal_fields_are_rejected(self):
        with self.assertRaises(AgentProposalError) as top:
            validate_agent_proposal(self._payload(confidence=0.99))
        self.assertEqual(top.exception.code, "forbidden_field")
        with self.assertRaises(AgentProposalError) as nested:
            validate_agent_proposal(self._payload(proposal={
                "field": "ruleSearch.bookList", "rule": ".item", "save": True,
            }))
        self.assertEqual(nested.exception.code, "forbidden_field")

    def test_verification_is_mandatory(self):
        for verification in ({"required": False, "method": "jvm"}, {"required": True}):
            with self.subTest(verification=verification):
                with self.assertRaises(AgentProposalError) as ctx:
                    validate_agent_proposal(self._payload(verification=verification))
                self.assertEqual(ctx.exception.code, "verification_required")

    def test_evidence_must_reference_existing_rows(self):
        with self.assertRaises(AgentProposalError) as ctx:
            validate_agent_proposal(
                self._payload(evidence_refs=["evidence-999"]),
                available_evidence_refs={"evidence-001"},
            )
        self.assertEqual(ctx.exception.code, "unknown_evidence_ref")
        with self.assertRaises(AgentProposalError) as duplicate:
            validate_agent_proposal(self._payload(evidence_refs=["evidence-001", "evidence-001"]))
        self.assertEqual(duplicate.exception.code, "invalid_evidence_ref")

    def test_strategy_is_controlled_and_normalized(self):
        result = validate_agent_proposal(self._payload(proposal={
            "field": "ruleContent.content", "rule": "params.chapter_images",
            "strategy": {"requires_webview": True, "runtime_field": "params.chapter_images",
                          "content_mode": "media"},
        }))
        self.assertTrue(result["proposal"]["strategy"]["requires_webview"])
        with self.assertRaises(AgentProposalError) as ctx:
            validate_agent_proposal(self._payload(proposal={
                "field": "ruleContent.content", "rule": ".item",
                "strategy": {"auto_save": True},
            }))
        self.assertEqual(ctx.exception.code, "forbidden_field")

    def test_oversized_lists_are_rejected_not_truncated(self):
        with self.assertRaises(AgentProposalError) as options:
            validate_agent_proposal(self._payload(action="ask_user", reason_code="user_confirmation_required",
                proposal={"question": "选择", "options": [str(i) for i in range(9)]}))
        self.assertEqual(options.exception.code, "field_too_long")
        with self.assertRaises(AgentProposalError) as refs:
            validate_agent_proposal(self._payload(evidence_refs=["evidence-%03d" % i for i in range(21)]))
        self.assertEqual(refs.exception.code, "field_too_long")

    def test_parse_returns_canonical_protocol_only(self):
        payload = self._payload()
        text = json.dumps(payload, ensure_ascii=False)
        result = parse_agent_proposal(text, available_evidence_refs={"evidence-001"})
        self.assertEqual(set(result), {
            "action", "layer", "reason_code", "proposal", "verification", "evidence_refs",
        })
        self.assertNotIn("confidence", result)


if __name__ == "__main__":
    unittest.main()
