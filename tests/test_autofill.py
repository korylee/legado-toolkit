# -*- coding: utf-8 -*-
"""详情页一键填充：按字段以引擎实测值反推规则的编排判据。"""
from __future__ import annotations

import unittest
from unittest import mock

from core.autofill import autofill_detail

#: 站形足够丰富的详情页：封面/链接有明确 class，文本块够 200 字（text 候选门槛）
DETAIL_HTML = """<html><body>
<h1 class="title">我贱卖自己的理由</h1>
<div class="intro-wrap"><p>简介铺垫一句。""" + "很长的介绍文字。" * 40 + """</p></div>
<img class="cover" src="https://img.example.com/cover/abc.webp">
<a class="chapter" href="/book/1/1.html">第1话</a>
<a class="chapter" href="/book/1/2.html">第2话</a>
<a class="chapter" href="/book/1/3.html">第3话</a>
</body></html>"""

STRUCTURED = {
    "name": "我贱卖自己的理由",
    "intro": "简介铺垫一句。" + "很长的介绍文字。" * 40,
    "cover_url": "https://img.example.com/cover/abc.webp",
    "toc_url": "/book/1/1.html",
    # author/kind/... 没锚点：引擎取到的就是空
}


class AutofillDetailTests(unittest.TestCase):

    def test_anchored_fields_get_rules_with_scores(self):
        """有实测值锚点的字段能出建议（cover/link 类直接命中；理由带预选结论）。"""
        out = autofill_detail(DETAIL_HTML, dict(STRUCTURED))
        rows = {r["field"]: r for r in out["fields"]}
        self.assertEqual(set(rows), {
            "name", "author", "kind", "word_count", "last_chapter",
            "intro", "cover_url", "toc_url"})
        # 封面与目录链接：media/link 候选 + 实测值锚 → picked
        self.assertEqual(rows["cover_url"]["rule"], ".cover@src")
        self.assertGreater(rows["cover_url"]["score"], 0)
        self.assertIn("abc.webp", "".join(rows["cover_url"]["samples"]))
        # 文本字段（name）拿不到锚？——锚有（structured.name），但 text 候选
        # 要求 200 字的块：h1 太短不成候选 → 如实比不了，不编规则
        self.assertNotIn("rule", rows["name"], "候选不成形就不出规则文本")
        self.assertTrue(rows["name"].get("reason"), "比不了要给原因")

    def test_no_anchor_fields_produce_no_rule(self):
        """引擎取到的就是空的字段（没锚点）：**不产出规则**，只给原因。

        这一层的结果会直接落进字段输入框，编一条「页面上最像的材料」等于让用户
        先信一次假的——用户实测反馈是「填的几乎都不可用，不如不填」。"""
        out = autofill_detail(DETAIL_HTML, {"name": "", "cover_url": ""})
        rows = {r["field"]: r for r in out["fields"]}
        self.assertTrue(rows["name"].get("no_anchor"),
                        "没有实测值可比要标出来，界面据此说「还没有实测值」")
        self.assertNotIn("rule", rows["name"], "没有锚点不许出规则文本")
        self.assertIn("实测值", rows["name"].get("reason") or "")

    def test_unpicked_candidates_produce_no_rule(self):
        """preselect 拒绝拍板（并列 / 低分 / 只有 weak）：只有原因，没有规则文本。

        这就是原先预览表里那行「仅参考」的由来——头名候选和实测值对不上时，
        把它落进输入框等于让用户先信一次假的。并列的具体形状由 ``preselect``
        自己的测试覆盖，这里只钉本模块的分支：**picked 为空就一个 rule 都不出**。"""
        tied = {
            "picked": None,
            "ranked": [{"rule": ".a@text", "score": 2, "samples": ["甲书"]},
                       {"rule": ".b@text", "score": 2, "samples": ["乙书"]}],
            "reason": "多条候选都能对上、报的还不是同一批样本（2 分 / 2 分），不替你猜",
        }
        with mock.patch("core.autofill.preselect", lambda *a, **k: tied):
            out = autofill_detail(DETAIL_HTML, dict(STRUCTURED))
        row = {r["field"]: r for r in out["fields"]}["name"]
        self.assertNotIn("rule", row, "不敢拍板就不出规则文本")
        self.assertTrue(row.get("need_ai"), "要标出「这里得靠模型提议」")
        self.assertIn("不替你猜", row.get("reason") or "")

    def test_empty_html_gives_reason_per_row(self):
        """没页面材料：每行都有原因，整批不炸（单字段失败不带整批的纪律）。"""
        out = autofill_detail("", dict(STRUCTURED))
        self.assertTrue(out["fields"])
        for row in out["fields"]:
            self.assertNotIn("rule", row)
            self.assertTrue(row.get("reason"))

    def test_content_step_goes_to_ai_on_script_material(self):
        """正文步：材料是脚本输出时不采 DOM 候选；`webJs` 本身没有候选可采。

        实测那种材料是「137 行图片地址、没有任何标签」——在它上面找 DOM 候选会拍出
        「把地址列表逐行当正文」的假通过（`body@html` 也 pass）。判据是**引擎事实**
        （`material_kind`），不读源配置；两行都标 `need_ai`，交 AI 按材料写。
        """
        urls = "https://img.example.com/1.jpg\nhttps://img.example.com/2.jpg"
        rows = {r["field"]: r for r in autofill_detail(
            urls, {}, step="content", material_kind="webjs_output")["fields"]}
        self.assertEqual(set(rows), {"webJs", "content"})
        for row in rows.values():
            self.assertNotIn("rule", row)
            self.assertTrue(row.get("need_ai"))
        # `webJs` 不是 DOM 取样：不许把「未知的候选类型」这种工具故障当原因（AGENTS #4）
        self.assertNotIn("候选生成失败", rows["webJs"].get("reason") or "")
        # 材料是 DOM 时不再拦它
        dom = autofill_detail(DETAIL_HTML, dict(STRUCTURED), step="content",
                              material_kind="rendered_dom")["fields"]
        self.assertTrue(all("页面脚本的输出" not in (r.get("reason") or "")
                            for r in dom))


if __name__ == "__main__":
    unittest.main()
