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


class FragmentPlanTests(unittest.TestCase):
    """段落按事实出：事实给了就出那一段，没给就不出（不造默认值）。

    为什么钉它：加一种页面结构 / 解析方式 = 在 `SECTIONS` 里加一个函数，基础提示词与两个
    builder 都不动。**段自己判定缺席**（返回 None），所以这里断言提示词里有没有那一段。
    """

    def test_sections_follow_the_facts(self):
        full = build_batch_prompt({
            "html": HTML, "step": "bookUrl", "step_label": "详情",
            "fields": [{"field": "name", "want_label": "书名", "app_values": ["甲书"]}],
            "runtimes": [{"name": "params", "type": "[object Object]", "keys": ["a"]}],
            "known_webjs": "var imgs = params.chapter_images;",
            "rule": "class.title@text",
        })
        for header in ("【要的字段】", "【页面 DOM 大纲", "【页面里的全局数据",
                       "【源里已有的页面脚本", "【App 实测取到的值", "【当前规则】"):
            self.assertIn(header, full, "给了这份事实就该出这一段：%s" % header)
        # 只给页面：其余段一律缺席（不造默认值）
        bare = build_batch_prompt({"html": HTML, "step": "bookUrl", "step_label": "详情"})
        for header in ("【要的字段】", "【页面里的全局数据", "【源里已有的页面脚本",
                       "【App 实测取到的值", "【当前规则】", "【已知的问题】"):
            self.assertNotIn(header, bare, "没给这份事实就不该出这一段：%s" % header)
        self.assertIn("【页面 DOM 大纲", bare)

    def test_diagnosis_is_always_last(self):
        """易变段在最后：它的顺序错了，前缀缓存就吃不到，等于每次改一次规则就全量计费。"""
        prompt = build_batch_prompt({
            "html": HTML, "step": "bookUrl", "step_label": "详情",
            "fields": [{"field": "name", "want_label": "书名", "app_values": ["甲书"]}],
            "runtimes": [{"name": "params", "type": "[object Object]"}],
            "rule": "class.title@text",
        })
        self.assertGreater(prompt.index("【当前规则】"),
                           prompt.index("【App 实测取到的值"))
        self.assertGreater(prompt.index("【当前规则】"), prompt.index("【页面 DOM 大纲"))

    def test_empty_values_do_not_produce_a_section(self):
        """空白值与 None 都不算实测值：那一段不该出现，`None` 更不许被转成字符串喂进去。"""
        prompt = build_batch_prompt({"html": HTML, "step": "bookUrl",
                                     "step_label": "详情",
                                     "fields": [{"field": "name",
                                                 "app_values": ["  ", None, ""]}]})
        self.assertNotIn("App 实测取到的值", prompt)
        self.assertNotIn("None", prompt.split("【页面 DOM 大纲")[0])


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

    def test_prompt_carries_page_globals_when_given(self):
        """给了候选数据源就列出来（名字 + 类型 + 长度/键名），**绝不带值**。

        为什么这算一条：正文的地址常常不在 DOM 里，而在某个全局变量上；变量名不可猜
        （`params` 只是某个站点的叫法），所以把「形状」列给模型让它判。
        """
        prompt = build_batch_prompt({
            "html": HTML, "step": "content", "step_label": "正文", "fields": FIELDS,
            "runtimes": [
                {"name": "pageData", "type": "[object Object]",
                 "keys": ["title", "chapter_images"]},
                {"name": "imgs", "type": "[object Array]", "count": 137},
            ],
            "runtime_dropped": 3,
        })
        self.assertIn("【页面里的全局数据", prompt)
        self.assertIn("pageData（Object，键：title、chapter_images）", prompt)
        self.assertIn("imgs（Array，137 条）", prompt)
        self.assertIn("另有 3 个更小的候选未列出", prompt)

    def test_known_webjs_is_offered_as_a_clue_with_truncation(self):
        """源里已有的 webJs 当线索：带变量名、明说不是材料、超长自报截断。

        它比枚举全局更准（变量名是源作者自己选的），但容易被模型当成 DOM 去解——
        所以「不是本步的材料」这句是判据的一部分，不是客套。
        """
        prompt = build_batch_prompt({
            "html": HTML, "step": "content", "step_label": "正文", "fields": FIELDS,
            "known_webjs": "var imgs = params.chapter_images; result = imgs.join('\\n');",
        })
        self.assertIn("【源里已有的页面脚本", prompt)
        self.assertIn("params.chapter_images", prompt)
        self.assertIn("不是本步的材料", prompt)
        self.assertNotIn("脚本已截断", prompt)
        # 超长：截断发生，且**自报**（不让模型以为看全了）
        long_prompt = build_batch_prompt({
            "html": HTML, "step": "content", "step_label": "正文", "fields": FIELDS,
            "known_webjs": "x" * 5000,
        })
        self.assertIn("脚本已截断", long_prompt)

    def test_no_webjs_section_without_one(self):
        prompt = build_batch_prompt({"html": HTML, "step": "content",
                                     "step_label": "正文", "fields": FIELDS})
        self.assertNotIn("源里已有的页面脚本", prompt)

    def test_no_outline_when_material_is_not_dom(self):
        """材料是页面脚本的输出（webjs_output）时**不给大纲**：压出来是垃圾。

        实测（2026-10-22）：有 webJs 时引擎交回的「整页」是 27150 字节、137 行图片地址、
        没有任何标签。对它压大纲等于让模型在一堆 URL 上找选择器——不如直说「没有结构」。
        """
        prompt = build_batch_prompt({
            "html": "https://img.example.com/1.jpg\nhttps://img.example.com/2.jpg",
            "step": "content", "step_label": "正文", "fields": FIELDS,
            "material_kind": "webjs_output",
        })
        self.assertNotIn("页面 DOM 大纲", prompt)
        self.assertIn("是页面脚本（webJs）的输出，不是 HTML", prompt)

    def test_image_heavy_dom_replaces_outline_with_one_stats_line(self):
        """图多而带地址的极少 → 用一行统计代替 90 行大纲（统计本身就是判据）。"""
        prompt = build_batch_prompt({
            "html": HTML, "step": "content", "step_label": "正文", "fields": FIELDS,
            "material_kind": "rendered_dom",
            "page_stats": {"images": 139, "images_with_src": 1, "links": 8},
        })
        self.assertNotIn("页面 DOM 大纲", prompt)
        self.assertIn("图片 139 个，其中带 src 的只有 1 个", prompt)
        self.assertIn("优先看下面的页面全局数据", prompt)

    def test_text_dom_still_gets_the_outline(self):
        """普通文本页照旧给大纲（别为了省 token 把正常路径也砍掉）。"""
        prompt = build_batch_prompt({
            "html": HTML, "step": "bookUrl", "step_label": "详情", "fields": FIELDS,
            "material_kind": "rendered_dom",
            "page_stats": {"images": 2, "images_with_src": 2},
        })
        self.assertIn("页面 DOM 大纲", prompt)

    def test_globals_section_teaches_the_two_scopes(self):
        """候选数据源那一段必须说清**两个环境**——错一次就是一条取不到的规则。

        2026-10-22 实测（端到端验收抓到的）：AI 给的 `@js:params.chapter_images` 当 content
        规则时 `verdict=unknown`、产出 0；而 `@js:result` 同场景 `pass`；`@js:typeof params`
        在 content 里返回 `undefined`。所以段里要写明「页面变量只在 webJs 里可读」+ 两步范式。
        """
        prompt = build_batch_prompt({
            "html": HTML, "step": "content", "step_label": "正文", "fields": FIELDS,
            "runtimes": [{"name": "params", "type": "[object Object]",
                          "keys": ["chapter_images"]}],
        })
        self.assertIn("只在页面脚本（webJs）里可读", prompt)
        self.assertIn("undefined", prompt)
        self.assertIn("result", prompt)

    def test_no_globals_section_when_nothing_was_collected(self):
        """没采到候选时**不出现这一段**：空清单不是「页面没有数据」（AGENTS #12）。"""
        prompt = build_batch_prompt({"html": HTML, "step": "content",
                                     "step_label": "正文", "fields": FIELDS})
        self.assertNotIn("页面里的全局数据", prompt)

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
