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
        """回归防线：极短正文（本例 fixture 只有 4 个字符）也必须是 pass，不是 fail。"""
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


class PinnedOrderTests(unittest.TestCase):
    """钉死 judge_content / judge_list_step 的前置分流顺序。

    这里守的是本模块最贵的误杀路径。3600 个用 ``@js`` 的源靠 ``rule_error``
    走 unknown；而 ``test_rule_error_is_unknown`` 传的是**非空 values**，
    根本区分不出第 3 步（rule_error）与第 4 步（提取为空）谁先执行——
    一旦有人把 rule_error 挪到 ``values_total == 0`` 之后，那条用例仍然全绿，
    这批源却会集体从 unknown 掉成 fail。所以必须用「rule_error 非空 +
    values 为空」这条唯一能区分两步顺序的输入把它钉住。

    同理，空规则必须按 bookSourceType 分派（第 2 步），不能被当成
    ``unsupported="空规则"`` 的不可回放规则而一律 unknown。
    """

    def test_empty_rule_text_source_is_fail_not_unknown(self):
        """rule="" 且 rule_error="空规则"：文本源要 fail（Legado 会把章节链接当正文）。"""
        j = content_judge(0, [], rule="", rule_error="空规则")
        self.assertEqual(j.verdict, Q.VERDICT_FAIL)
        self.assertIn("章节链接", j.reason)

    def test_empty_rule_audio_source_is_pass_not_unknown(self):
        """rule="" 且 rule_error="空规则"：音频源要 pass（回退用章节链接是正常配置）。"""
        j = content_judge(1, [], rule="", rule_error="空规则")
        self.assertEqual(j.verdict, Q.VERDICT_PASS)

    def test_rule_error_with_empty_values_is_unknown_not_fail(self):
        """关键用例：rule_error 非空 + values 为空 → unknown。

        这是唯一能区分「rule_error 先判」与「提取为空先判」的输入：
        把 rule_error 挪到后面，本用例立刻变成 fail。
        """
        j = content_judge(0, [], rule="id.content@text", rule_error="JS 规则无法离线回放")
        self.assertEqual(j.verdict, Q.VERDICT_UNKNOWN)
        self.assertIn("无法离线回放", j.reason)

    def test_list_step_rule_error_with_empty_values_is_unknown(self):
        """列表步同样要 rule_error 先于「解析结果为空」。"""
        j = Q.judge_list_step(Q.STEP_TOC, [], rule_error="JS 规则无法离线回放")
        self.assertEqual(j.verdict, Q.VERDICT_UNKNOWN)
        self.assertIn("无法离线回放", j.reason)


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


class ListStepStepKeyTests(unittest.TestCase):
    """回归防线：judge_list_step 的 step 入口归一化。

    "TOC" 这类笔误若不被归一化，下载源豁免就失效——本该 unknown 的结果
    会掉成 fail。这是本模块唯一一条「靠拼写笔误即可产生误杀」的路径。
    """

    def test_uppercase_toc_keeps_download_source_exemption(self):
        j = Q.judge_list_step("TOC", [], source_type=3)
        self.assertEqual(j.verdict, Q.VERDICT_UNKNOWN)

    def test_uppercase_toc_behaves_same_as_lowercase(self):
        upper = Q.judge_list_step("TOC", ["/c/1"])
        lower = Q.judge_list_step("toc", ["/c/1"])
        self.assertEqual(upper.verdict, lower.verdict)
        self.assertEqual(upper.reason, lower.reason)
        self.assertEqual(upper.notes, lower.notes)

    def test_whitespace_is_stripped(self):
        # 用下载源断言，否则「有没有 strip」在空列表上分不出来（两种都是 fail）
        self.assertEqual(
            Q.judge_list_step("  ToC  ", [], source_type=3).verdict, Q.VERDICT_UNKNOWN)

    def test_exported_step_constants_are_the_canonical_names(self):
        # 调用方一律用这些常量，别再手写字符串字面量
        self.assertEqual(Q.STEP_SEARCH, "search")
        self.assertEqual(Q.STEP_BOOK_URL, "bookUrl")
        self.assertEqual(Q.STEP_TOC, "toc")


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


class DirtySourceTypeTests(unittest.TestCase):
    """bookSourceType 来自外部 JSON，脏值（""/[]/"abc"/None）必须降级为 0，不能抛异常。

    本模块是全部源的共用闸门：抛异常会中断整批校验，降级为 0 最坏只是口径偏保守。
    """

    def test_dirty_types_do_not_raise_and_fall_back_to_zero(self):
        for dirty in ("", [], "abc", None, {}):
            with self.subTest(dirty=dirty):
                # 降级为 0 = 文本源：空规则 → fail；列表空 → fail
                self.assertEqual(content_judge(dirty, [], rule="").verdict, Q.VERDICT_FAIL)
                self.assertEqual(
                    Q.judge_list_step("toc", [], source_type=dirty).verdict, Q.VERDICT_FAIL)

    def test_string_number_is_accepted(self):
        # "3" 是合法字符串数字，应识别为下载源而不是降级
        self.assertEqual(content_judge("3", ["x"]).verdict, Q.VERDICT_UNKNOWN)

    def test_infinite_type_does_not_raise(self):
        # json.loads('{"bookSourceType": 1e400}') → inf，int(inf) 抛 OverflowError，
        # 必须同样降级为 0（文本源）：空规则 → fail
        inf = float("inf")
        self.assertEqual(content_judge(inf, [], rule="").verdict, Q.VERDICT_FAIL)
        self.assertEqual(
            Q.judge_list_step("toc", [], source_type=inf).verdict, Q.VERDICT_FAIL)

    def test_static_misconfig_survives_dirty_type(self):
        # 必须用 "abc" 这类 int() 真会抛的脏值："" / [] / None / {} 经
        # `v or 0` 短路后都变成 0，裸 int() 也不抛，退化成假覆盖
        # （把 _safe_int 换回裸 int 后这些用例照样全绿）
        self.assertEqual(Q.static_misconfig_notes({"bookSourceType": "abc"}), [])


class BuildEvidenceTests(unittest.TestCase):
    """evidence 的 values_total 是判定里唯一的 fail 判据，其余字段直接喂 UI。"""

    def test_empty_values(self):
        ev = Q.build_evidence([])
        self.assertEqual(ev["values_total"], 0)
        self.assertEqual(ev["chars"], 0)
        self.assertEqual(ev["cjk_chars"], 0)
        self.assertEqual(ev["block_seps"], 0)
        self.assertEqual(ev["tag_ratio"], 0.0)
        self.assertEqual(ev["noise_hit"], "")

    def test_values_total_counts_only_nonblank(self):
        self.assertEqual(Q.build_evidence(["a", "", "  ", "b"])["values_total"], 2)

    def test_chars_and_cjk_chars(self):
        ev = Q.build_evidence(["正文abc"])
        self.assertEqual(ev["chars"], 5)
        self.assertEqual(ev["cjk_chars"], 2)

    def test_block_seps_come_from_matched_html(self):
        ev = Q.build_evidence(["正文"], "<p>a</p><br>")
        self.assertEqual(ev["block_seps"], 3)

    def test_tag_ratio(self):
        # "<p>a</p>" 共 8 字符，其中 7 个字符属于标签
        self.assertEqual(Q.build_evidence(["<p>a</p>"])["tag_ratio"], 0.875)

    def test_noise_hit_only_when_content_short(self):
        self.assertEqual(Q.build_evidence(["页面不存在"])["noise_hit"], "页面不存在")
        self.assertEqual(Q.build_evidence(["正文" * 400 + "页面不存在"])["noise_hit"], "")


class ShortContentThresholdTests(unittest.TestCase):
    """SHORT_CONTENT_CHARS = 500 取自 Legado BookContent.kt:194，只影响附注与噪声词。"""

    @staticmethod
    def _judge_chars(n):
        return content_judge(0, ["文" * n])

    def test_499_chars_is_short(self):
        self.assertTrue(any("正文较短" in n for n in self._judge_chars(499).notes))

    def test_500_chars_is_not_short(self):
        self.assertFalse(any("正文较短" in n for n in self._judge_chars(500).notes))

    def test_501_chars_is_not_short(self):
        self.assertFalse(any("正文较短" in n for n in self._judge_chars(501).notes))

    def test_boundary_never_changes_verdict(self):
        for n in (499, 500, 501):
            with self.subTest(n=n):
                self.assertEqual(self._judge_chars(n).verdict, Q.VERDICT_PASS)

    def test_noise_hit_uses_the_same_boundary(self):
        # "页面不存在" 占 5 字符
        self.assertEqual(
            Q.build_evidence(["文" * 494 + "页面不存在"])["noise_hit"], "页面不存在")
        self.assertEqual(Q.build_evidence(["文" * 495 + "页面不存在"])["noise_hit"], "")


class AsStepDictTests(unittest.TestCase):
    """摊平口径只有一处：Judgement.as_step_dict()。「试跑」与「批量校验」共用。"""

    def test_legacy_fields_present(self):
        j = content_judge(0, [])          # fail，reason 非空
        step = j.as_step_dict("content", url="https://a.com/c/1", page_id="p1")
        self.assertEqual(step["name"], "content")
        self.assertIs(step["ok"], False)
        self.assertEqual(step["detail"], "正文提取为空")
        self.assertEqual(step["url"], "https://a.com/c/1")
        self.assertEqual(step["page_id"], "p1")

    def test_new_fields_present(self):
        j = content_judge(0, ["页面不存在"])
        step = j.as_step_dict("content", values=["页面不存在"],
                              matched_html="<p>页面不存在</p>")
        self.assertEqual(step["verdict"], j.verdict)
        self.assertEqual(step["reason"], j.reason)
        self.assertEqual(step["shape"], j.shape)
        self.assertIs(step["has_notes"], j.has_notes)
        self.assertEqual(step["notes"], j.notes)
        self.assertEqual(step["evidence"], j.evidence)
        self.assertEqual(step["values"], ["页面不存在"])
        self.assertEqual(step["matched_html"], "<p>页面不存在</p>")
        self.assertEqual(step["rule_error"], "")

    def test_pass_detail_falls_back_to_shape(self):
        # pass 时 reason 为空，detail 退回形态名，保证旧前端不显示空白
        step = Q.Judgement(Q.VERDICT_PASS, shape=Q.SHAPE_TEXT).as_step_dict("content")
        self.assertEqual(step["detail"], Q.SHAPE_TEXT)
        self.assertIs(step["ok"], True)

    def test_detail_override_wins(self):
        j = Q.Judgement(Q.VERDICT_FAIL, reason="原始原因")
        self.assertEqual(j.as_step_dict("content", detail="覆盖说明")["detail"], "覆盖说明")

    def test_values_notes_evidence_are_copies_not_references(self):
        values = ["正文"]
        j = content_judge(0, values)
        step = j.as_step_dict("content", values=values)
        step["values"].append("注入")
        step["notes"].append("注入")
        step["evidence"]["injected"] = True
        self.assertEqual(values, ["正文"])
        self.assertNotIn("注入", j.notes)
        self.assertNotIn("injected", j.evidence)


if __name__ == "__main__":
    unittest.main()
