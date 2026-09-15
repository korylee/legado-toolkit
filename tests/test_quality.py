# -*- coding: utf-8 -*-
"""判定模块单元测试。

重点守住一条底线：**没有任何一条基于长度的 fail**。
Legado 的调试只判 contentStr.isBlank()（BookContent.kt:203-205），
自创长度阈值会误杀短章节。
"""

import unittest

from core import quality as Q


def content_judge(source_type, values, rule="id.content@text",
                  matched_html="", rule_error=""):
    return Q.judge_content(source_type, values, rule, matched_html, rule_error)


class SniffShapeTests(unittest.TestCase):
    def test_text(self):
        shape, _ = Q.sniff_shape(["第一章 正文内容"])
        self.assertEqual(shape, Q.SHAPE_TEXT)

    def test_image_by_ext_and_tag(self):
        self.assertEqual(Q.sniff_shape(["https://a.com/1.jpg"])[0], Q.SHAPE_IMAGE)
        self.assertEqual(Q.sniff_shape(['<img src="/1.png">'])[0], Q.SHAPE_IMAGE)

    def test_audio(self):
        self.assertEqual(Q.sniff_shape(["https://a.com/1.mp3"])[0], Q.SHAPE_AUDIO)

    def test_empty_and_mixed(self):
        self.assertEqual(Q.sniff_shape([])[0], Q.SHAPE_EMPTY)
        self.assertEqual(Q.sniff_shape(["  ", ""])[0], Q.SHAPE_EMPTY)
        self.assertEqual(Q.sniff_shape(["https://a.com/1.jpg", "正文"])[0], Q.SHAPE_MIXED)


class ContentVerdictTests(unittest.TestCase):
    def test_nonempty_is_pass_even_if_very_short(self):
        """回归防线：20 字的合法短正文必须是 pass，不是 fail。"""
        j = content_judge(0, ["短正文。"])
        self.assertEqual(j.verdict, Q.VERDICT_PASS)

    def test_empty_is_fail(self):
        j = content_judge(0, [])
        self.assertEqual(j.verdict, Q.VERDICT_FAIL)
        self.assertIn("空", j.reason)

    def test_blank_only_is_fail(self):
        j = content_judge(0, ["  ", "\n"])
        self.assertEqual(j.verdict, Q.VERDICT_FAIL)


class ContentSourceTypeTests(unittest.TestCase):
    def test_text_source_empty_rule_is_fail(self):
        j = content_judge(0, [], rule="")
        self.assertEqual(j.verdict, Q.VERDICT_FAIL)
        self.assertIn("章节链接", j.reason)

    def test_audio_source_empty_rule_is_pass(self):
        j = content_judge(1, [], rule="")
        self.assertEqual(j.verdict, Q.VERDICT_PASS)
        self.assertIn("章节链接", j.reason)

    def test_image_source_empty_rule_is_pass(self):
        j = content_judge(2, [], rule="")
        self.assertEqual(j.verdict, Q.VERDICT_PASS)

    def test_unknown_type_empty_rule_is_unknown(self):
        j = content_judge(4, [], rule="")
        self.assertEqual(j.verdict, Q.VERDICT_UNKNOWN)

    def test_download_source_is_unknown(self):
        j = content_judge(3, ["x"])
        self.assertEqual(j.verdict, Q.VERDICT_UNKNOWN)
        self.assertIn("文件类", j.reason)

    def test_rule_error_is_unknown(self):
        j = content_judge(0, ["x"], rule_error="JS 规则（@js:/<js>）需要 Legado 的 Rhino 引擎，无法离线回放")
        self.assertEqual(j.verdict, Q.VERDICT_UNKNOWN)
        self.assertIn("Rhino", j.reason)


class NoteTests(unittest.TestCase):
    """启发式只能产出 notes，不能改变 verdict。"""

    def test_struct_tag_is_note_not_fail(self):
        j = content_judge(0, ['<div class="content">正文</div>'])
        self.assertEqual(j.verdict, Q.VERDICT_PASS)
        self.assertTrue(j.has_notes)
        self.assertTrue(any("结构性" in n for n in j.notes))

    def test_noise_word_is_note_not_fail(self):
        j = content_judge(0, ["页面不存在"])
        self.assertEqual(j.verdict, Q.VERDICT_PASS)
        self.assertTrue(any("疑似错误页" in n for n in j.notes))

    def test_noise_word_ignored_when_content_long(self):
        long_text = "正文" * 400 + "页面不存在"
        j = content_judge(0, [long_text])
        self.assertFalse(any("疑似错误页" in n for n in j.notes))

    def test_type_mismatch_note(self):
        j = content_judge(0, ["https://a.com/1.jpg", "https://a.com/2.jpg"])
        self.assertEqual(j.verdict, Q.VERDICT_PASS)
        self.assertTrue(any("bookSourceType" in n for n in j.notes))


class ListStepTests(unittest.TestCase):
    def test_toc_empty_is_fail(self):
        j = Q.judge_list_step("toc", [])
        self.assertEqual(j.verdict, Q.VERDICT_FAIL)

    def test_toc_few_chapters_is_pass_with_note(self):
        j = Q.judge_list_step("toc", ["/c/1", "/c/2"])
        self.assertEqual(j.verdict, Q.VERDICT_PASS)
        self.assertTrue(any("章节数偏少" in n for n in j.notes))

    def test_toc_download_source_is_unknown(self):
        j = Q.judge_list_step("toc", [], source_type=3)
        self.assertEqual(j.verdict, Q.VERDICT_UNKNOWN)

    def test_search_empty_is_fail(self):
        j = Q.judge_list_step("search", [])
        self.assertEqual(j.verdict, Q.VERDICT_FAIL)


class StaticMisconfigTests(unittest.TestCase):
    def test_webjs_without_webview(self):
        notes = Q.static_misconfig_notes({
            "searchUrl": "https://a.com/s?q={{key}}",
            "ruleContent": {"webJs": "return 1"},
        })
        self.assertTrue(any("webJs" in n for n in notes))

    def test_webjs_with_webview_is_clean(self):
        notes = Q.static_misconfig_notes({
            "searchUrl": 'https://a.com/s?q={{key}},{"webView":true}',
            "ruleContent": {"webJs": "return 1"},
        })
        self.assertEqual([n for n in notes if "webJs" in n], [])

    def test_type_four_warns(self):
        notes = Q.static_misconfig_notes({"bookSourceType": 4})
        self.assertTrue(any("bookSourceType" in n for n in notes))


class CheckerStateMapTests(unittest.TestCase):
    def test_mapping(self):
        self.assertIs(Q.Judgement(Q.VERDICT_PASS).checker_state, True)
        self.assertIs(Q.Judgement(Q.VERDICT_FAIL).checker_state, False)
        self.assertIsNone(Q.Judgement(Q.VERDICT_UNKNOWN).checker_state)

    def test_ok_only_fail_is_false(self):
        self.assertTrue(Q.Judgement(Q.VERDICT_PASS).ok)
        self.assertTrue(Q.Judgement(Q.VERDICT_UNKNOWN).ok)
        self.assertFalse(Q.Judgement(Q.VERDICT_FAIL).ok)


if __name__ == "__main__":
    unittest.main()
