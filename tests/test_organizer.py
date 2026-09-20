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
        self.assertEqual(group_title(0, Health.AUTH), "📖小说,需登录")
        self.assertEqual(group_title(0, Health.GFW), "📖小说,需翻墙")

    def test_auth_and_pending_are_different_tags(self) -> None:
        """「有结论但被站点拒绝」与「我们没结论」必须是两个标签。

        合并过一阵子：AUTH 也写「待验证」，结果一个真在用的源被判 AUTH 后，
        在编辑弹窗里和三千多条从没校验过的源显示一模一样，看不出区别。

        2026-09 档位重设计后「没结论」只有 PENDING 一档（timeout / error /
        no_search / skipped 已并入，判据是下一步动作相同）。
        """
        self.assertNotEqual(group_title(0, Health.AUTH), group_title(0, Health.PENDING))
        self.assertEqual(group_title(0, Health.PENDING), "📖小说,待验证")

    def test_status_tag_round_trips_through_inference(self) -> None:
        """分组标签 -> 健康状态 -> 分组标签，必须回到原处。

        之前这里是**不对称**的：认得「需验证」-> AUTH，但 AUTH 写回去却是
        「待验证」，一读一写就丢了。迁移/导入都走这条往返，丢了不会报错。

        **这份名单要跟着健康态枚举一起长**：新增一个健康态而忘了这两张表里的
        任何一张，症状都是"它悄悄落进「待验证」"——和三千多条从没校验过的源
        显示成同一个标签（CERT 就是这么被漏过一次）。
        """
        for health in (Health.OK, Health.AUTH, Health.GFW, Health.DEAD, Health.CERT,
                       Health.PENDING):
            tag = group_title(0, health).split(",", 1)[1]
            self.assertEqual(infer_health_from_group(tag), health,
                             "「%s」往返后变了" % tag)

    def test_cert_is_a_conclusion_not_pending(self) -> None:
        """证书问题与「待验证」必须分开，理由同 AUTH 那条：
        它是**有结论**的（站点可达、只是证书不被信任），落进「待验证」就等于
        和从没校验过的源显示成同一个标签。"""
        self.assertEqual(group_title(0, Health.CERT), "📖小说,证书问题")
        self.assertNotEqual(group_title(0, Health.CERT), group_title(0, Health.PENDING))

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

        self.assertEqual(result[0]["bookSourceGroup"], "📖小说,需登录,R18")

    def test_legacy_group_health_is_inferred_conservatively(self) -> None:
        self.assertEqual(infer_health_from_group("📖小说/✅★★★☆☆"), Health.OK)
        self.assertEqual(infer_health_from_group("📖小说/🔒需验证"), Health.AUTH)
        self.assertEqual(infer_health_from_group("📖小说/🌐需翻墙"), Health.GFW)

    def test_retired_tag_names_still_read_back(self) -> None:
        """旧分组里的退役词，`infer_health_from_group` 仍要认出来。

        这是**导入/迁移期读旧分组**那条路：认不出就落到兜底「待验证」，把本来能
        确认的状态降级（`test_unrecognizable_group_is_pending_not_auth` 说的是
        另一件事——真认不出来的才该保守）。

        「代理复检」这个裸形式是旧分组真出现过的写法，所以断言它而不只是
        「需代理复检」。至于「旧词不许漏成用户标签」那一侧，由
        `tests/test_tags.py` 遍历 `RETIRED_STATUS_TAG_RENAMES` 守着，不在这里重复。
        """
        self.assertEqual(infer_health_from_group("📖小说,需验证"), Health.AUTH)
        self.assertEqual(infer_health_from_group("📖小说,需代理复检"), Health.GFW)
        self.assertEqual(infer_health_from_group("📖小说,代理复检"), Health.GFW)

    def test_unrecognizable_group_is_pending_not_auth(self) -> None:
        """认不出来的分组 = **我们不知道**，不能推断成「需登录」。

        兜底曾经返回 Health.AUTH；AUTH 现在显示为「需登录」，那会把"不知道"
        说成"站点要验证"——凭空造结论。保守的方向是「待验证」。
        """
        self.assertEqual(infer_health_from_group("自用"), Health.PENDING)
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


class HealthTableCoverageTests(unittest.TestCase):
    """三张「按健康态分支」的表必须覆盖 `HEALTH_NAMES` 的每个键。

    它们都走 `.get(key, 默认)` 兜底，**漏一个键不报错**，只是静默给出错误分类：

      - `HEALTH_ORDER`：漏了 → 排序落到最后。CERT 就这样排到了「待验证」之后
      - `STATUS_GROUP_NAMES`：漏了 → 落进「待验证」。CERT 也犯过（见那张表的注释）
      - `HEALTH_NAMES` 本身是权威，前两张表由它派生

    新增健康态时，这条用例会红——比在生产里发现「这个档位的源被排到最后」便宜得多。
    """

    def test_order_covers_every_health(self) -> None:
        from core.models import HEALTH_NAMES
        from core.organizer import HEALTH_ORDER

        self.assertEqual(set(HEALTH_ORDER), set(HEALTH_NAMES),
                         "HEALTH_ORDER 与 HEALTH_NAMES 的键集必须一致")

    def test_group_names_cover_every_health(self) -> None:
        from core.models import HEALTH_NAMES
        from core.organizer import STATUS_GROUP_NAMES

        self.assertEqual(set(STATUS_GROUP_NAMES), set(HEALTH_NAMES),
                         "STATUS_GROUP_NAMES 与 HEALTH_NAMES 的键集必须一致")

    def test_cert_sorts_before_pending(self) -> None:
        """CERT 是「可达、有结论、能自己处理」的档，不该排在「待验证」之后。"""
        from core.organizer import HEALTH_ORDER

        self.assertLess(HEALTH_ORDER[Health.CERT], HEALTH_ORDER[Health.PENDING])

    def test_display_name_ends_with_the_group_tag(self) -> None:
        """一个状态只有一个名字：`HEALTH_NAMES` 必须就是「emoji + 分组标签」。

        这两张表描述同一件事的两个落点——统计条/报告读前者，写进 App 的分组读
        后者。措辞一旦分叉（「需验证」vs「需登录」、「证书」vs「证书问题」），
        用户就会在界面上看到同一个状态有两个名字，这正是本轮重设计要消掉的东西。
        """
        from core.models import HEALTH_NAMES
        from core.organizer import STATUS_GROUP_NAMES

        for health, tag in STATUS_GROUP_NAMES.items():
            self.assertTrue(HEALTH_NAMES[health].endswith(tag),
                            "「%s」的显示名 %s 与分组标签 %s 措辞不一致"
                            % (health, HEALTH_NAMES[health], tag))


# ---------------------------------------------------------------- 变异记录
# 实测（改坏 → python -B -m unittest tests.test_organizer → 确认变红 → 还原）：
#
#  M1  STATUS_GROUP_NAMES 里 AUTH 改回「待验证」
#        → test_auth_and_pending_are_different_tags 红
#  M2  infer_health_from_group 的兜底改回 Health.AUTH
#        → test_unrecognizable_group_is_pending_not_auth 红
#  M3  去掉「需代理复检」的识别
#        → test_status_tag_round_trips_through_inference 红
#  M4  HEALTH_ORDER 删掉 CERT
#        → HealthTableCoverageTests.test_order_covers_every_health 红
#        （三张按健康态分支的表都走 `.get(key, 默认)` 兜底，**漏一个键不报错**，
#          只是静默给错分类——CERT 就在 HEALTH_ORDER 与 STATUS_GROUP_NAMES 里
#          各漏过一次）
#
# M3 撞出的是一个**既有 bug**，与本轮改动无关：group_title 写出去的是
# 「需代理复检」，而 infer_health_from_group 只认「需翻墙/被墙/🌐」，GFW 的往返
# 同样是断的（写出去再读回来落成「待验证」）。是写往返测试时才发现的。
# 这类断裂不会报错，只会静默丢状态——导入、迁移、重建分组都走这条往返。
