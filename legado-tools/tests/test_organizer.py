# -*- coding: utf-8 -*-
"""面向使用的书源分组与备注清理测试。"""

from __future__ import annotations

import unittest

from models import Health, build_record
from organizer import group_title, infer_health_from_group, organize_sources


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
        self.assertEqual(group_title(0, Health.AUTH), "📖小说,待验证")
        self.assertEqual(group_title(0, Health.GFW), "📖小说,需代理复检")

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

    def test_stable_r18_tag_is_preserved(self) -> None:
        raw = source("H漫")
        record = build_record(raw, 0)
        record.health = Health.AUTH

        result = organize_sources([record])

        self.assertEqual(result[0]["bookSourceGroup"], "📖小说,待验证,R18")

    def test_legacy_group_health_is_inferred_conservatively(self) -> None:
        self.assertEqual(infer_health_from_group("📖小说/✅★★★☆☆"), Health.OK)
        self.assertEqual(infer_health_from_group("📖小说/🔒需验证"), Health.AUTH)
        self.assertEqual(infer_health_from_group("📖小说/🌐需翻墙"), Health.GFW)
        self.assertEqual(infer_health_from_group("自用"), Health.AUTH)

    def test_unstable_quality_tag_is_not_written_to_group(self) -> None:
        raw = source("📖小说/✅★★★★★,规则完整,番茄,正版")
        record = build_record(raw, 0)
        record.health = Health.OK
        record.quality_tags = ["规则完整", "原创"]

        result = organize_sources([record])

        self.assertEqual(result[0]["bookSourceGroup"], "📖小说,可用,正版,原创")
