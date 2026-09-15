# -*- coding: utf-8 -*-
"""LLM JSON 配置存储测试。"""

from __future__ import annotations

import os
import shutil
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
            "id": "deepseek", "name": "DeepSeek",
            "base_url": "https://api.deepseek.com/v1",
            "api_key": "sk-abcdefgh", "model": "deepseek-chat",
        })
        self.assertEqual(p["id"], "deepseek")
        self.assertEqual(llm_store.get_effective_profile()["id"], "deepseek")
        masked = llm_store.get_effective_profile(mask=True)
        self.assertNotIn("api_key", masked)
        self.assertTrue(masked["api_key_set"])
        self.assertTrue(masked["api_key_masked"].endswith("efgh"))

        updated = llm_store.update_profile("deepseek", {"name": "DeepSeek 2", "api_key": ""})
        self.assertEqual(updated["name"], "DeepSeek 2")
        self.assertEqual(updated["api_key"], "sk-abcdefgh")

    def test_delete_reassigns_active(self) -> None:
        llm_store.create_profile({"id": "a", "name": "A", "api_key": "x", "model": "m"})
        llm_store.create_profile({"id": "b", "name": "B", "api_key": "y", "model": "m"})
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
