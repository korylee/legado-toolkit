# -*- coding: utf-8 -*-
"""`/rules/outline` 的断言：把页面压成**给人看**的缩进大纲。

它是「材料栏默认看大纲」的数据源——判据一份在 `core.repair.evidence.dom_outline`
（AI 提示词的同一份实现），这里钉的是端点的两件事：材料缺失时**给出原因**（不是空
文本）、超预算时**自报不全**（不能把「前 N 行」说成全部）。
"""
from __future__ import annotations

import asyncio
import unittest

from fastapi import HTTPException

from backend.api.rules import rule_outline
from backend.schemas import OutlineRequest
from core.repair.evidence import DISPLAY_OUTLINE_LINES
from tests.support import job_store, one_step_job

HTML = """<html><body>
<div class="book"><h1 class="title">甲书</h1><span class="author">夏子久</span></div>
</body></html>"""


def _store(html=HTML, step="bookUrl"):
    return one_step_job(html, step=step)


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
        """超预算要自报不全：界面据此说「只到前 N 行」，不能当全部。

        构造上要**避开同构折叠**：一串同形状的兄弟会被折成一行 `×N`（`dom_outline`
        的既有行为），页数再多也不超预算。所以这里每层都换标签与 class，保证每行独立。
        """
        tags = ("div", "section", "article", "aside", "nav", "main", "header", "footer")
        html = "<html><body>" + "".join(
            '<%s class="c%d"><span class="s%d">值%d</span></%s>' % (
                tags[i % len(tags)], i, i, i, tags[i % len(tags)])
            for i in range(DISPLAY_OUTLINE_LINES * 3)
        ) + "</body></html>"
        out = _call(_store(html=html))
        self.assertTrue(out["truncated"])
        self.assertEqual(len(out["text"].split(chr(10))), DISPLAY_OUTLINE_LINES)

    def test_missing_page_gives_a_reason(self):
        """页面没材料：报原因（404/400），不返回空大纲冒充「这页没有结构」。"""
        with self.assertRaises(HTTPException) as ctx:
            _call(_store(html="   "))
        self.assertIn("页面材料", str(ctx.exception.detail))

    def test_webjs_output_is_not_treated_as_dom(self):
        """材料是页面脚本的输出时不给大纲，说清原因与下一步。

        实测（2026-10-21，同一页）：页面脚本生效时引擎交回的「整页」是那段脚本的返回值
        （27150 字节、137 行图片地址、没有任何标签）。压大纲会得到空树，而界面默认就看
        大纲——用户会读成「这页没有可写的结构」（AGENTS #12：读不到 ≠ 没有）。
        判据只看页上的引擎事实（`rendered` + `render_js`），不看源里配没配 webJs。
        """
        store = job_store(
            source={"bookSourceUrl": "http://example.com"},
            steps=[{"name": "content", "page_id": "chapter", "url": "http://x/1"}],
            pages=[{"id": "chapter", "url": "http://x/1", "rendered": True,
                    "render_js": True,
                    "html": "https://img.example.com/1.jpg\nhttps://img.example.com/2.jpg"}])

        with self.assertRaises(HTTPException) as ctx:
            _call(store, step="content")
        self.assertIn("页面脚本", str(ctx.exception.detail))
        self.assertIn("js 选项", str(ctx.exception.detail), "要给出能执行的那一步")


if __name__ == "__main__":
    unittest.main()
