# -*- coding: utf-8 -*-
"""剥离试跑证据字段。三处消费方共用同一个函数，所以它必须是一个纯函数。"""

import unittest

from core.verify import strip_evidence


class StripEvidenceTests(unittest.TestCase):
    def _sample(self):
        return {
            "steps": [{
                "name": "content", "ok": True, "verdict": "pass",
                "reason": "", "notes": ["正文较短"], "has_notes": True,
                "values": ["很长的正文" * 1000],
                "matched_html": "<div>" * 1000,
                "evidence": {"chars": 5000},
            }],
            "pages": [{"id": "chapter", "html": "<html>" * 10000}],
            "all_ok": True,
        }

    def test_strips_big_fields(self):
        v = strip_evidence(self._sample())
        self.assertEqual(v["pages"], [])
        self.assertEqual(v["steps"][0]["values"], [])
        self.assertEqual(v["steps"][0]["matched_html"], "")

    def test_keeps_verdict_and_evidence(self):
        """剥的只是证据原文；判定结论与附注必须原样保留。

        消费方要靠 verdict / reason / notes 做决策，丢了它们等于把功能剥没了。
        """
        v = strip_evidence(self._sample())
        s = v["steps"][0]
        self.assertEqual(s["verdict"], "pass")
        self.assertTrue(s["has_notes"])
        self.assertEqual(s["notes"], ["正文较短"])
        self.assertEqual(s["evidence"], {"chars": 5000})
        self.assertIs(v["all_ok"], True)

    def test_does_not_mutate_input(self):
        """必须返回新对象。

        `core/repair/loop.py` 会同时持有剥离前后的两份结果，
        就地改写会把另一份也一起改掉。
        """
        src = self._sample()
        strip_evidence(src)
        self.assertEqual(len(src["pages"]), 1)
        self.assertTrue(src["steps"][0]["values"])

    def test_none_and_empty_are_passed_through(self):
        """兜底：调用方可能传 None 或空 dict（如 ops.py 的 skipped 分支）。"""
        self.assertIsNone(strip_evidence(None))
        self.assertEqual(strip_evidence({}), {})

    def test_step_without_evidence_fields_is_untouched(self):
        """真实场景里并非每步都带 values / matched_html，缺键不能炸。"""
        v = strip_evidence({"steps": [{"name": "search", "ok": False}], "all_ok": False})
        self.assertEqual(v["steps"][0], {"name": "search", "ok": False})


if __name__ == "__main__":
    unittest.main()


# ------------------------------------------------------------------ 变异验证记录
# 为避免「断言里两个值恰好相等、被测分支生效与否都成立」的假测试，上面每条断言
# 都做了一次变异验证：改坏 `core/verify.py::strip_evidence` 的对应分支 → 跑本文件
# → 确认至少一条变红 → 精确回退。下表是真实输出（变异用临时脚本完成，跑完已删）。
#
# | # | 改坏了什么 | 变红的测试 |
# |---|---|---|
# | M1 | 返回体不清空 `pages` | test_strips_big_fields |
# | M2 | 不清空 `steps[].values` | test_strips_big_fields |
# | M3 | 不清空 `steps[].matched_html` | test_strips_big_fields |
# | M4 | 用 dict 字面量重建 step（丢 verdict/notes/evidence） | test_strips_big_fields、test_keeps_verdict_and_evidence |
# | M5 | 返回体丢掉 `all_ok` | test_keeps_verdict_and_evidence |
# | M6 | `new_step = s`（就地改写，不返回新对象） | test_does_not_mutate_input |
# | M7 | 删掉 `None` / `{}` 兜底 | test_none_and_empty_are_passed_through |
# | M8 | 无条件清 `values`（缺键也凭空添键） | test_step_without_evidence_fields_is_untouched |
#
# M8 守的是本实现的一处有意选择：**键存在才清**，不给外部注入的合成结果添键。
# 真实 `verify_chain` 的每一步都必然带这两个键（口径在 `quality.Judgement.as_step_dict`），
# 所以两种写法在真实数据上等价，M8 这条只是在钉住这个选择别被改回去。
#
# 另对 `core/repair/loop.py` 的两处剥离点做了同样的变异（临时验证，未留成常驻用例）：
#
# | 变异 | 变红 |
# |---|---|
# | 点 1 `before` 不剥 | `AssertionError: before.pages 未清空` |
# | 点 2 轮内 `v` 不剥 | `AssertionError: after.pages 未清空` |
