# -*- coding: utf-8 -*-
"""批量提议（suggest_many）的离线测试：一次调用出多字段、单字段失败不塌整批。

假客户端注入（不真调模型）；提示词是纯函数直接断言。
"""
from __future__ import annotations

import asyncio
import unittest

from core.repair.suggest import build_batch_prompt, suggest_many

HTML = """<html><body>
<h1 class="title">甲书</h1>
<div class="author-line">作者: 夏子久</div>
</body></html>"""


class FakeLLM:
    """假客户端：按给定的返回体应答，记录调用次数。"""

    def __init__(self, body="", *, enabled=True):
        self.calls = 0
        self._body = body
        self.last_usage = {}
        self.config = type("C", (), {"model": "fake", "enabled": enabled})()

    @property
    def enabled(self):
        return self.config.enabled

    async def chat(self, system, user, session=None, temperature=None):
        self.calls += 1
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def _ok_body():
    return """{"fields":[
      {"field":"name","candidates":[{"rule":".title@text","why":"标题行"}],"reason":""},
      {"field":"author","candidates":[{"rule":".author-line@text##作者: ##","why":"作者行"}],"reason":""}
    ]}"""


FIELDS = [
    {"field": "name", "want_label": "书名", "app_values": ["甲书"]},
    {"field": "author", "want_label": "作者", "app_values": []},
]


class SuggestManyTests(unittest.TestCase):

    def test_prompt_shares_outline_and_lists_fields(self):
        """提示词只喂一份 DOM 大纲，字段与实测值逐一列出（省上下文钱的落点）。"""
        prompt = build_batch_prompt({"html": HTML, "step": "bookUrl",
                                     "step_label": "详情",
                                     "fields": FIELDS})
        self.assertEqual(prompt.count("页面 DOM 大纲"), 1,
                         "共享大纲是一族一次调用的全部意义，喂两份就是白花钱")
        self.assertIn("- name：书名", prompt)
        self.assertIn("- author：作者", prompt)
        self.assertIn("甲书", prompt)

    def test_parse_keeps_independent_fields(self):
        """多字段一次解析；某字段没给候选就缺席（不塌整批、不编默认）。"""
        client = FakeLLM(_ok_body())
        out = asyncio.run(suggest_many(
            {"html": HTML, "step": "bookUrl", "step_label": "详情",
             "fields": FIELDS}, client=client))
        self.assertEqual(out["llm"], "ok")
        self.assertEqual(out["fields"]["name"][0]["rule"], ".title@text")
        self.assertEqual(out["fields"]["author"][0]["why"], "作者行")
        self.assertEqual(client.calls, 1, "一族字段只花一次调用")

    def test_bad_json_reports_original_text(self):
        """输出不是 JSON：llm=error、原因带原文前 200 字（AGENTS #4）。"""
        client = FakeLLM("我觉得应该是 class.title@text")
        out = asyncio.run(suggest_many(
            {"html": HTML, "step": "bookUrl", "step_label": "详情",
             "fields": FIELDS}, client=client))
        self.assertEqual(out["llm"], "error")
        self.assertIn("不是合法 JSON", out["error"])
        self.assertIn("class.title@text", out["error"], "原因要带原文")


if __name__ == "__main__":
    unittest.main()
