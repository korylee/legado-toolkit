# -*- coding: utf-8 -*-
"""`/rules/outline` 的断言：把页面压成**给人看**的缩进大纲。

它是「材料栏默认看大纲」的数据源——判据一份在 `core.repair.evidence.dom_outline`
（AI 提示词的同一份实现），这里钉的是端点的两件事：材料缺失时**给出原因**（不是空
文本）、超预算时**自报不全**（不能把「前 N 行」说成全部）。
"""
from __future__ import annotations

import asyncio
import json
import unittest

from fastapi import HTTPException

from backend.api.rules import rule_outline
from backend.schemas import OutlineRequest
from core.repair.evidence import DISPLAY_OUTLINE_LINES

HTML = """<html><body>
<div class="book"><h1 class="title">甲书</h1><span class="author">夏子久</span></div>
</body></html>"""


def _store(html=HTML, step="bookUrl"):
    job = {
        "payload": json.dumps({"source": {"bookSourceUrl": "http://example.com"}}),
        "result_json": json.dumps({
            "steps": [{"name": step, "page_id": "detail", "url": "http://x/d"}],
            "pages": [{"id": "detail", "url": "http://x/d", "html": html}],
        }),
    }

    class _St:
        def get_job(self, _job_id):
            return job

    return _St()


def _call(store, step="bookUrl"):
    return asyncio.run(rule_outline(
        OutlineRequest(job_id="j1", step=step), st=store))


class RuleOutlineTests(unittest.TestCase):

    def test_returns_indented_outline(self):
        out = _call(_store())
        self.assertIn("div .book", out["text"])
        self.assertIn("h1 .title", out["text"])
        self.assertIn("夏子久", out["text"], "短文本要带上——它才是判「选对了没」的凭据")
        self.assertFalse(out["truncated"])

    def test_long_page_reports_truncation(self):
        """超预算要自报不全：界面据此说「只到前 N 行」，不能当全部。"""
        html = "<html><body>" + "".join(
            '<div class="row"><span class="c%d">值%d</span></div>' % (i, i)
            for i in range(DISPLAY_OUTLINE_LINES * 3)
        ) + "</body></html>"
        out = _call(_store(html=html))
        self.assertTrue(out["truncated"])
        self.assertEqual(len(out["text"].split("\n")), DISPLAY_OUTLINE_LINES)

    def test_missing_page_gives_a_reason(self):
        """页面没材料：报原因（404/400），不返回空大纲冒充「这页没有结构」。"""
        with self.assertRaises(HTTPException) as ctx:
            _call(_store(html="   "))
        self.assertIn("页面材料", str(ctx.exception.detail))


if __name__ == "__main__":
    unittest.main()
