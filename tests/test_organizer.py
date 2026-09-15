# -*- coding: utf-8 -*-
"""面向使用的书源分组与备注清理测试。"""

from __future__ import annotations

import unittest

from core.models import Health, build_record
from core.organizer import group_title, infer_health_from_group, organize_sources


def source(group: str = "") -> dict:
    return {
        "bookSourceName": "测试源",
        "bookSourceUrl": "https://example.com",
        "bookSourceGroup": group,
        "bookSourceComment": "说明\n[原分组] 📖小说/✅★★★★★,命中《斗破苍穹》,规则完整",
        "ruleSearch": {"bookList": ".book"},
        "ruleToc": {"chapterList": ".chapter"},
        "ruleContent": {"content": "#content"},
    }


class OrganizerTests(unittest.TestCase):
    def test_status_group_uses_readable_lifecycle_name(self) -> None:
        self.assertEqual(group_title(0, Health.OK), "📖小说,可用")
        self.assertEqual(group_title(0, Health.AUTH), "📖小说,需验证")
        self.assertEqual(group_title(0, Health.GFW), "📖小说,需代理复检")

    def test_auth_and_pending_are_different_tags(self) -> None:
        """「有结论但被站点拒绝」与「我们没结论」必须是两个标签。

        合并过一阵子：AUTH 也写「待验证」，结果一个真在用的源被判 AUTH 后，
        在编辑弹窗里和三千多条从没校验过的源显示一模一样，看不出区别。
        """
        self.assertNotEqual(group_title(0, Health.AUTH), group_title(0, Health.SKIPPED))
        self.assertEqual(group_title(0, Health.SKIPPED), "📖小说,待验证")
        for h in (Health.NO_SEARCH, Health.TIMEOUT, Health.ERROR):
            self.assertEqual(group_title(0, h), "📖小说,待验证")

    def test_status_tag_round_trips_through_inference(self) -> None:
        """分组标签 -> 健康状态 -> 分组标签，必须回到原处。

        之前这里是**不对称**的：认得「需验证」-> AUTH，但 AUTH 写回去却是
        「待验证」，一读一写就丢了。迁移/导入都走这条往返，丢了不会报错。
        """
        for health in (Health.OK, Health.AUTH, Health.GFW, Health.DEAD):
            tag = group_title(0, health).split(",", 1)[1]
            self.assertEqual(infer_health_from_group(tag), health,
                             "「%s」往返后变了" % tag)

    def test_organize_removes_hit_marker_and_original_group_line(self) -> None:
        raw = source("📖小说/✅★★★★★,命中《斗破苍穹》,规则完整")
        raw["bookSourceComment"] = (
            "// Error: timeout\n说明\n"
            "[原分组] 📖小说/✅★★★★★,命中《斗破苍穹》,规则完整"
        )
        record = build_record(raw, 0)
        record.health = Health.OK
        record.quality_stars = 5

        result = organize_sources([record])

        self.assertEqual(result[0]["bookSourceGroup"], "📖小说,可用")
        self.assertNotIn("命中《", result[0]["bookSourceGroup"])
        self.assertNotIn("命中《", result[0]["bookSourceComment"])
        self.assertNotIn("[原分组]", result[0]["bookSourceComment"])
        self.assertNotIn("// Error:", result[0]["bookSourceComment"])

    def test_stable_user_tag_is_preserved(self) -> None:
        raw = source("R18")
        record = build_record(raw, 0)
        record.health = Health.AUTH

        result = organize_sources([record])

        self.assertEqual(result[0]["bookSourceGroup"], "📖小说,需验证,R18")

    def test_legacy_group_health_is_inferred_conservatively(self) -> None:
        self.assertEqual(infer_health_from_group("📖小说/✅★★★☆☆"), Health.OK)
        self.assertEqual(infer_health_from_group("📖小说/🔒需验证"), Health.AUTH)
        self.assertEqual(infer_health_from_group("📖小说/🌐需翻墙"), Health.GFW)

    def test_unrecognizable_group_is_pending_not_auth(self) -> None:
        """认不出来的分组 = **我们不知道**，不能推断成「需验证」。

        兜底曾经返回 Health.AUTH；AUTH 现在显示为「需验证」，那会把"不知道"
        说成"站点要验证"——凭空造结论。保守的方向是「待验证」。
        """
        self.assertEqual(infer_health_from_group("自用"), Health.SKIPPED)
        self.assertEqual(group_title(0, infer_health_from_group("自用")),
                         "📖小说,待验证")

    def test_system_quality_and_user_tags_are_written(self) -> None:
        raw = source("📖小说/✅★★★★★,规则完整,番茄,正版,原创")
        record = build_record(raw, 0)
        record.health = Health.OK
        record.quality_tags = ["规则完整"]

        result = organize_sources([record])

        self.assertEqual(result[0]["bookSourceGroup"],
                         "📖小说,可用,规则完整,番茄,正版,原创")


# ---------------------------------------------------------------- 变异记录
# 实测（改坏 → python -B -m unittest tests.test_organizer → 确认变红 → 还原）：
#
#  M1  STATUS_GROUP_NAMES 里 AUTH 改回「待验证」
#        → test_auth_and_pending_are_different_tags 红
#  M2  infer_health_from_group 的兜底改回 Health.AUTH
#        → test_unrecognizable_group_is_pending_not_auth 红
#  M3  去掉「需代理复检」的识别
#        → test_status_tag_round_trips_through_inference 红
#
# M3 撞出的是一个**既有 bug**，与本轮改动无关：group_title 写出去的是
# 「需代理复检」，而 infer_health_from_group 只认「需翻墙/被墙/🌐」，GFW 的往返
# 同样是断的（写出去再读回来落成「待验证」）。是写往返测试时才发现的。
# 这类断裂不会报错，只会静默丢状态——导入、迁移、重建分组都走这条往返。
