# -*- coding: utf-8 -*-
"""契约测试：Agent 上下文只接收事实摘要，不泄露原始材料。"""
from __future__ import annotations

import json
import unittest

from core.agent_context import build_agent_context, canonical_context_json


class AgentContextTests(unittest.TestCase):
    def test_l1_keeps_target_stats_evidence_and_candidates(self):
        context = build_agent_context({
            "layer": "L1",
            "target": {"step": "search", "want": "list"},
            "page": {
                "stats": {"links": 12, "images": 2, "images_with_src": 1, "text_len": 80},
                "has_wanted": True,
                "evidence": [{"kind": "page", "why": "target", "line": 3, "snippet": "book list"}],
            },
            "candidates": [{"kind": "list", "rule": ".books .item", "count": 8,
                            "samples": ["甲", "乙"]}],
        })
        self.assertEqual(context["layer"], "L1")
        self.assertEqual(context["facts"]["stats"]["links"], 12)
        self.assertTrue(context["facts"]["has_wanted"])
        self.assertEqual(context["evidence_refs"][0]["line"], 3)
        self.assertEqual(context["candidates"][0]["rule"], ".books .item")
        self.assertEqual(context["gaps"], [])

    def test_l2_keeps_container_and_engine_facts(self):
        context = build_agent_context({
            "layer": "L2",
            "page": {"stats": {"images": 3}, "container": "empty-image-container"},
            "capabilities": {"jvm_debug": True, "app_debug": False, "unexpected": True},
        })
        self.assertEqual(context["facts"]["container"], "empty-image-container")
        self.assertEqual(context["facts"]["available_engines"], {
            "jvm_debug": True, "app_debug": False,
        })

    def test_l3_excludes_ciphertext_and_keeps_runtime_shape(self):
        context = build_agent_context({
            "layer": "L3",
            "page": {
                "markers": ["CryptoJS", "createObjectURL"],
                "evidence": [{"why": "base64", "line": 4, "snippet": "A" * 300}],
            },
            "runtime": {
                "channel": "jvm",
                "object_types": ["dict", "list"],
                "keys": ["chapter_images", "secret_token"],
                "counts": {"chapter_images": 12},
            },
        })
        self.assertEqual(context["facts"]["markers"], ["CryptoJS", "createObjectURL"])
        self.assertNotIn("snippet", context["evidence_refs"][0])
        self.assertEqual(context["runtime"]["object_types"], ["dict", "list"])
        self.assertEqual(context["runtime"]["keys"], ["chapter_images"])
        self.assertEqual(context["runtime"]["counts"]["chapter_images"], 12)

    def test_l4_keeps_network_shape_without_response_body(self):
        context = build_agent_context({
            "layer": "L4",
            "network": [{
                "method": "get", "status": 200,
                "url": "https://example.test/api/books?id=987654",
                "content_type": "application/json",
                "json_shape": {"data": [{"title": "secret response"}]},
                "candidate_paths": ["$.data[*].title"],
                "body": "THIS MUST NOT APPEAR",
            }],
        })
        network = context["network"][0]
        self.assertEqual(network["method"], "GET")
        self.assertEqual(network["path_shape"], "/api/books")
        self.assertEqual(network["status"], 200)
        self.assertEqual(network["candidate_paths"], ["$.data[*].title"])
        self.assertEqual(network["json_shape"], {
            "type": "object", "keys": ["data"],
        })
        self.assertNotIn("body", network)
        self.assertNotIn("secret response", json.dumps(context))

    def test_l5_keeps_login_fact_and_capability(self):
        context = build_agent_context({
            "layer": "L5",
            "page": {"login_marker": "请登录后继续"},
            "capabilities": {"login_context": True, "cookie_jar": False},
        })
        self.assertEqual(context["facts"]["login_marker"], "请登录后继续")
        self.assertEqual(context["facts"]["login_capabilities"], {
            "login_context": True, "cookie_jar": False,
        })
        self.assertEqual(context["gaps"], [])

    def test_missing_material_produces_explicit_gap(self):
        context = build_agent_context({"layer": "L4", "target": {"step": "content"}})
        self.assertEqual(context["gaps"][0]["code"], "network_material_missing")
        self.assertTrue(context["gaps"][0]["reason"])
        invalid = build_agent_context({})
        self.assertEqual(invalid["gaps"][0]["code"], "layer_missing")

    def test_supplied_gaps_are_bounded_and_sorted(self):
        context = build_agent_context({
            "layer": "L1",
            "page": {"stats": {"links": 1}},
            "gaps": [
                {"code": "z_gap", "reason": "z"},
                {"code": "a_gap", "reason": "a"},
            ],
        })
        self.assertEqual([item["code"] for item in context["gaps"]], ["a_gap", "z_gap"])

    def test_input_order_does_not_change_canonical_context(self):
        left = {
            "layer": "L4",
            "page": {"stats": {"links": 1}},
            "network": [
                {"method": "POST", "url": "/z", "status": 500},
                {"method": "GET", "url": "/a", "status": 200},
            ],
            "candidates": [
                {"kind": "text", "rule": ".z", "count": 2},
                {"kind": "link", "rule": ".a", "count": 1},
            ],
        }
        right = {
            "candidates": list(reversed(left["candidates"])),
            "network": list(reversed(left["network"])),
            "page": left["page"],
            "layer": left["layer"],
        }
        self.assertEqual(
            canonical_context_json(build_agent_context(left)),
            canonical_context_json(build_agent_context(right)),
        )

    def test_sensitive_material_is_not_copied(self):
        cookie = "sessionid=do-not-copy-this-value"
        secret = "A" * 120
        context = build_agent_context({
            "layer": "L1",
            "page": {"stats": {"links": 1}, "evidence": [
                {"why": "authorization: " + cookie, "snippet": secret},
            ]},
            "network": [{"url": "https://x.test/a?token=" + secret,
                         "headers": {"Cookie": cookie}, "body": secret}],
            "candidates": [{"kind": "media", "rule": ".x@src", "samples": [secret]}],
        })
        encoded = json.dumps(context, ensure_ascii=False)
        self.assertNotIn(cookie, encoded)
        self.assertNotIn(secret, encoded)
        self.assertIn("sha256", encoded)


if __name__ == "__main__":
    unittest.main()
