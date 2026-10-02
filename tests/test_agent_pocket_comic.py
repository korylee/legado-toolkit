# -*- coding: utf-8 -*-
"""Guarded pocket-comic L3 strategy tests."""
from __future__ import annotations

import unittest

from core.agent_context import build_agent_context
from core.agent_pocket_comic import build_pocket_comic_strategy
from core.agent_router import route_agent_context


class PocketComicStrategyTests(unittest.TestCase):
    def _context(self, **runtime):
        return build_agent_context({
            "layer": "L3",
            "target": {"step": "content", "want": "media", "chapter_selected": True},
            "runtime": runtime,
        })

    def test_ready_strategy_is_generic_es5_and_requires_app_verification(self):
        result = build_pocket_comic_strategy(self._context(
            object_types=["dict"],
            keys=["params", "params.chapter_images"],
            counts={"chapter_images": 3, "chapter_images_accessible": 3},
        ))
        self.assertTrue(result["ready"])
        self.assertEqual(result["strategy"], {
            "requires_webview": True,
            "runtime_field": "params.chapter_images",
            "content_mode": "media",
        })
        self.assertEqual(result["verification"]["method"], "app")
        self.assertNotIn("http://", result["draft"]["ruleContent"]["webJs"])
        self.assertNotIn("https://", result["draft"]["ruleContent"]["webJs"])
        self.assertNotIn("AES", result["draft"]["ruleContent"]["webJs"])
        self.assertIn("function", result["draft"]["ruleContent"]["content"])

    def test_chapter_selection_is_required(self):
        context = self._context(
            object_types=["dict"], keys=["params.chapter_images"],
            counts={"chapter_images": 2, "chapter_images_accessible": 2},
        )
        context["target"]["chapter_selected"] = False
        result = build_pocket_comic_strategy(context)
        self.assertEqual(result["code"], "chapter_required")

    def test_string_params_are_not_accepted(self):
        result = build_pocket_comic_strategy(self._context(
            object_types=["str"], keys=["params.chapter_images"],
            counts={"chapter_images": 2, "chapter_images_accessible": 2},
        ))
        self.assertEqual(result["code"], "params_not_object")

    def test_empty_or_inaccessible_images_remain_explicit(self):
        empty = build_pocket_comic_strategy(self._context(
            object_types=["dict"], keys=["params.chapter_images"], counts={"chapter_images": 0},
        ))
        self.assertEqual(empty["code"], "images_empty")
        inaccessible = build_pocket_comic_strategy(self._context(
            object_types=["dict"], keys=["params.chapter_images"],
            counts={"chapter_images": 2, "chapter_images_accessible": 1},
        ))
        self.assertEqual(inaccessible["code"], "images_not_accessible")

    def test_blob_images_are_rejected(self):
        result = build_pocket_comic_strategy(self._context(
            object_types=["dict"], keys=["params.chapter_images"],
            counts={"chapter_images": 2, "chapter_images_accessible": 2, "chapter_images_blob": 2},
        ))
        self.assertEqual(result["code"], "images_blob_only")

    def test_router_exposes_ready_strategy_only_after_checks(self):
        context = self._context(
            channel="app", object_types=["dict"], keys=["params.chapter_images"],
            counts={"chapter_images": 2, "chapter_images_accessible": 2},
        )
        plan = route_agent_context(context)
        self.assertEqual(plan["action"], "suggest_rule")
        self.assertEqual(plan["strategy"]["runtime_field"], "params.chapter_images")
        self.assertEqual(plan["verification"]["method"], "app")


if __name__ == "__main__":
    unittest.main()
