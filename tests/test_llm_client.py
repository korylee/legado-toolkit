# -*- coding: utf-8 -*-
"""LLM 客户端三种协议的请求/响应形状测试（假 session，不联网）。"""

from __future__ import annotations

import asyncio
import json
import unittest

from core.repair.llm import LLMClient, LLMConfig


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status = status

    async def text(self):
        return json.dumps(self._payload)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Session:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return _Resp(self.payload)


def _config(protocol):
    return LLMConfig(base_url="https://api.example.com/v1", api_key="k",
                     model="m", timeout=90, protocol=protocol)


class LLMProtocolTests(unittest.TestCase):
    def test_anthropic_posts_messages_and_reads_content_blocks(self) -> None:
        session = _Session({
            "content": [{"type": "text", "text": "pong"}],
            "usage": {"input_tokens": 7, "output_tokens": 3,
                      "cache_read_input_tokens": 2},
        })
        client = LLMClient(config=_config("anthropic"))
        text = asyncio.run(client.chat("sys", "hi", session=session))
        url, kwargs = session.calls[0]
        self.assertEqual(url, "https://api.example.com/v1/messages")
        self.assertEqual(kwargs["headers"]["x-api-key"], "k")
        self.assertIn("anthropic-version", kwargs["headers"])
        self.assertGreater(kwargs["json"]["max_tokens"], 0)
        self.assertEqual(kwargs["json"]["system"], "sys")
        self.assertEqual(text, "pong")
        self.assertEqual(client.last_usage["prompt_tokens"], 7)
        self.assertEqual(client.last_usage["completion_tokens"], 3)
        self.assertEqual(client.last_usage["prompt_cache_hit_tokens"], 2)

    def test_responses_posts_responses_and_reads_output_text(self) -> None:
        session = _Session({
            "output": [{"type": "message", "content": [
                {"type": "output_text", "text": "po"},
                {"type": "output_text", "text": "ng"}]}],
            "usage": {"input_tokens": 9, "output_tokens": 4,
                      "input_tokens_details": {"cached_tokens": 5}},
        })
        client = LLMClient(config=_config("responses"))
        text = asyncio.run(client.chat("sys", "hi", session=session))
        url, kwargs = session.calls[0]
        self.assertEqual(url, "https://api.example.com/v1/responses")
        self.assertEqual(kwargs["json"]["instructions"], "sys")
        self.assertEqual(kwargs["json"]["input"], "hi")
        self.assertNotIn("temperature", kwargs["json"])
        self.assertEqual(text, "pong")
        self.assertEqual(client.last_usage["prompt_cache_hit_tokens"], 5)


if __name__ == "__main__":
    unittest.main()
