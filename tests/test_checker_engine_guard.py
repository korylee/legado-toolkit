# -*- coding: utf-8 -*-
"""深度探测遇到「离线跑不了的规则」时，必须报**这条规则自己的**原因。

三个地方各自用字符串子串查 JS，判据还互不相同：

    _confirm_hit                     "<js" / startswith("js:") / "@xpath"
    _probe_toc 的 bookUrl            "<js"
    _probe_content 的 chapterUrl     "<js"

而真实语料里最常见的形态是**中间**的 `selector@js:code`——三处全部漏掉。
`core/rules/replayer.py::parse_rule` 的检测是完整的，同一类 bug 在它那里
修过一次（JS 检测提到最前 + 补上 `@js:`），这三处没跟上。

后果分两档：

  - `_confirm_hit`：漏掉 → 拿去 `apply_css_rule` 跑出空 → 判「搜索未命中」。
    而判未命中会让整个深度验证跳过（`checker.py:732` 要求 `search_hit`），
    目录/正文一次都不验——界面上也看不出原因。
    ⚠️ 这条早退是**有意不留痕**的，别顺手加记录：见 test_checker_judge 的
    `test_js_rule_is_a_known_tradeoff_not_a_downgrade`（记满 JS 会淹没真正的异常）。
  - `_probe_toc` / `_probe_content`：结论是 None（安全，不误杀），但**原因错**成
    「解析为空（站点结构变化？）」——把「工具测不了」说成「源坏了」。lessons §二
    专门钉过这条：这个工具的全部价值就是告诉他为什么。

判据必须是**规则自己的 `unsupported` 原因**，不是我们临时猜的一句话。
（实测：68115 条真实规则里，没有一条出现「`parse_rule` 判无法解析、但旧引擎
`apply_css_rule` 仍跑得出值」，所以这里可以直接以 `parse_rule` 为准。）
"""

from __future__ import annotations

import asyncio
import unittest

from core.checker import AsyncChecker
from core.models import build_record


def make_source(**over) -> dict:
    src = {
        "bookSourceUrl": "https://x.example",
        "bookSourceName": "X",
        "searchUrl": "https://x.example/s?q={{key}}",
        "ruleSearch": {"bookList": "class.item", "bookUrl": "class.item@tag.a@href"},
        "ruleToc": {"chapterList": "class.c", "chapterUrl": "a@href"},
        "ruleContent": {"content": "class.content"},
    }
    src.update(over)
    return src


HTML = b'<html><body><div class="item"><a href="/b/1">A</a></div></body></html>'


class EngineBoundRuleTests(unittest.TestCase):
    """规则跑不了时：判「无法验证」+ 报出规则自己的原因。"""

    def setUp(self) -> None:
        self.ck = AsyncChecker(concurrency=1)

    def test_confirm_hit_treats_mid_rule_js_as_a_hit_silently(self):
        """中间形态 `selector@js:` 也必须走「保守算命中」那条早退。

        **早退本身是有意设计**——见 test_checker_judge 的
        `test_js_rule_is_a_known_tradeoff_not_a_downgrade`：记满 JS 会变成噪音，
        淹没真正要看的那种异常（依赖缺失、解析器坏掉）。所以这里**不**要求留痕，
        只要求「别掉下去」。

        掉下去的后果：`apply_css_rule` 对 `@js:` 规则返回空（实测）→
        `return len(items) >= 1` = False → 判「搜索未命中」。而那条路径会让
        整个深度验证跳过（`checker.py:732` 要求 `search_hit`），界面上还看不出原因。
        这正是那条老测试注释里预言的失败模式：「本该保守算命中的源会被判成未命中」。
        """
        rec = build_record(make_source(ruleSearch={"bookList": "class.item@text@js:result"}), 0)
        self.assertTrue(self.ck._confirm_hit(HTML, rec), "保守方向：按命中处理")
        self.assertEqual(self.ck.hit_downgrades, [], "明知的能力边界，按设计不留痕")

    def test_confirm_hit_still_reports_a_real_miss(self):
        """反向断言：规则能跑但跑不出东西 → 仍然是「未命中」，不降级。

        少了这条，把「跑不了」和「没结果」一起降级也会全绿——而那等于
        把「源真的没有这本书」也说成无法判定。
        """
        rec = build_record(make_source(ruleSearch={"bookList": "class.nothing"}), 0)
        self.assertFalse(self.ck._confirm_hit(HTML, rec))
        self.assertEqual(self.ck.hit_downgrades, [])

    def test_toc_reports_the_rule_reason_not_a_guess(self):
        """bookUrl 含 `@js:` → 原因是「含 JS 规则」，不是「解析为空」。"""
        rec = build_record(make_source(ruleSearch={
            "bookList": "class.item",
            "bookUrl": "class.item@tag.a@href@js:result",
        }), 0)
        asyncio.run(self.ck._probe_toc(None, rec, "https://x.example", HTML))
        self.assertIsNone(rec.toc_complete, "跑不了 → 无法验证，不能判不完整")
        self.assertIn("JS", rec.toc_fail_reason or "",
                      "原因必须指向规则本身：%r" % rec.toc_fail_reason)

    def test_content_reports_the_rule_reason_not_a_guess(self):
        """chapterUrl 含 `@js:` → 原因是「含 JS 规则」，不是「解析为空」。"""
        rec = build_record(make_source(ruleToc={
            "chapterList": "class.c",
            "chapterUrl": "a@href@js:result",
        }), 0)
        asyncio.run(self.ck._probe_content(None, rec, "https://x.example", HTML))
        self.assertIsNone(rec.content_ok, "跑不了 → 无法验证，不能判正文不可用")
        self.assertIn("JS", rec.content_fail_reason or "",
                      "原因必须指向规则本身：%r" % rec.content_fail_reason)


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------- 变异记录
# 以下为实测（改坏 → `python -B -m unittest tests.test_checker_engine_guard` →
# 确认变红 → 还原）。**必须带 -B**，理由见 test_reclassify.py 的脚注。
#
# 三处是**三份独立的判据**，所以逐个变异、逐个断言——合成一条的话，
# 第一个失败后面的就不再执行，另外两处漏改看不出来。
#
#  M11  `_confirm_hit` 退回子串判据（`"<js"` / `startswith("js:")` / `"@xpath"`）
#         → test_confirm_hit_treats_mid_rule_js_as_a_hit_silently 红
#         （反向断言 test_confirm_hit_still_reports_a_real_miss 与
#           test_checker_judge 的 JS-tradeoff 那条**保持绿**——它们守的是别的方向）
#  M12  `_probe_toc` 退回 `"<js" in book_url_rule or "<js" in chapter_list_rule`
#         → test_toc_reports_the_rule_reason_not_a_guess 红
#  M13  `_probe_content` 退回 `"<js" in chapter_url_rule`
#         → test_content_reports_the_rule_reason_not_a_guess 红
#
#  ⚠️ 做 M12/M13 时第一版变异**同时改了原因字符串的大小写**（写成 `"<js 规则"`），
#  于是测试因为断言 `"JS" in reason` 的大小写而红——**红的理由不对**，看着像
#  「检测退化被抓住了」，其实只抓住了措辞。改成保留原措辞、只退检测才是有效的。
