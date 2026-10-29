# -*- coding: utf-8 -*-
"""LLM JSON 配置存储测试。"""

from __future__ import annotations

import json
import os
import unittest
import uuid

from core import llm_store


_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
os.makedirs(_ROOT, exist_ok=True)


class LLMStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.path = os.path.join(_ROOT, "tmp_llm_" + uuid.uuid4().hex[:8] + ".json")
        os.environ["LEGADO_LLM_CONFIG"] = self.path

    def tearDown(self) -> None:
        os.environ.pop("LEGADO_LLM_CONFIG", None)
        if os.path.exists(self.path):
            os.remove(self.path)

    def test_create_activate_mask_and_update_key(self) -> None:
        p = llm_store.create_profile({
            "id": "deepseek", "base_url": "https://api.deepseek.com/v1",
            "api_key": "sk-abcdefgh", "model": "deepseek-chat",
        })
        self.assertEqual(p["id"], "deepseek")
        self.assertEqual(p["protocol"], "openai")
        self.assertEqual(llm_store.get_effective_profile()["id"], "deepseek")
        masked = llm_store.get_effective_profile(mask=True)
        self.assertNotIn("api_key", masked)
        self.assertTrue(masked["api_key_set"])
        self.assertTrue(masked["api_key_masked"].endswith("efgh"))

        updated = llm_store.update_profile("deepseek", {"model": "deepseek-reasoner",
                                                        "api_key": ""})
        self.assertEqual(updated["model"], "deepseek-reasoner")
        self.assertEqual(updated["api_key"], "sk-abcdefgh")

    def test_legacy_profile_fields_are_dropped(self) -> None:
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({
                "schema_version": 2, "active_profile_id": "x",
                "profiles": [{
                    "id": "x", "name": "X", "provider": "openai",
                    "adapter": "openai-compatible", "api_key": "k", "model": "m",
                    "base_url": "https://api.deepseek.com/v1",
                    "temperature": 0.5, "max_tokens": 100, "enabled": False,
                    "sort_order": 3, "api_key_env": "X_KEY",
                }],
            }, f)
        p = llm_store.load()["profiles"][0]
        self.assertEqual(p["protocol"], "openai")
        self.assertEqual(p["base_url"], "https://api.deepseek.com/v1")
        self.assertEqual(
            sorted(p), sorted(["id", "protocol", "base_url", "api_key", "model",
                               "timeout", "created_at", "updated_at"]))

    def test_delete_reassigns_active(self) -> None:
        llm_store.create_profile({"id": "a", "api_key": "x", "model": "m"})
        llm_store.create_profile({"id": "b", "api_key": "y", "model": "m"})
        self.assertTrue(llm_store.set_active("b"))
        self.assertTrue(llm_store.delete_profile("b"))
        self.assertEqual(llm_store.get_effective_profile()["id"], "a")

    def test_env_fallback(self) -> None:
        os.environ["LEGADO_LLM_API_KEY"] = "env-key"
        os.environ["LEGADO_LLM_BASE_URL"] = "http://localhost:11434/v1"
        os.environ["LEGADO_LLM_MODEL"] = "qwen2.5:7b"
        try:
            p = llm_store.get_effective_profile()
            self.assertEqual(p["id"], "env")
            self.assertEqual(p["api_key"], "env-key")
            self.assertEqual(p["model"], "qwen2.5:7b")
        finally:
            os.environ.pop("LEGADO_LLM_API_KEY", None)
            os.environ.pop("LEGADO_LLM_BASE_URL", None)
            os.environ.pop("LEGADO_LLM_MODEL", None)


if __name__ == "__main__":
    unittest.main()
