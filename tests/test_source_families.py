# -*- coding: utf-8 -*-
"""批量改名与 Unicode 角标分配规则。"""
from pathlib import Path
import unittest

from core.rename_badges import RENAME_BADGES, assign_rename_badges, renamed_source_name


class RenameBadgeTests(unittest.TestCase):
    def test_incremental_badges_follow_selected_order(self):
        badges = assign_rename_badges(["first", "second", "third"], "incremental")
        self.assertEqual(badges, ["ᵃ", "ᵇ", "ᶜ"])

    def test_manual_badges_allow_omitting_badge_for_a_source(self):
        badges = assign_rename_badges(
            ["first", "second", "third"], "manual",
            {"first": "¹", "second": "", "third": "ᵇ"},
        )
        self.assertEqual(badges, ["¹", "", "ᵇ"])

    def test_frontend_badge_list_matches_python_authority(self):
        root = Path(__file__).resolve().parents[1]
        javascript = (root / "frontend/src/utils/renameBadges.js").read_text(encoding="utf-8")
        for badge in RENAME_BADGES:
            self.assertIn(f'"{badge}"', javascript)
        # 只数词表段（export function 之前）：全文计数会把函数体里的字符串也抓进来
        vocabulary = javascript.split("export function")[0]
        self.assertEqual(vocabulary.count('"'), len(RENAME_BADGES) * 2)

    def test_name_suffix_is_trimmed_and_unicode_badge_is_appended(self):
        self.assertEqual(renamed_source_name("书旗小说", " 旧版 ", "ᵃ"), "书旗小说旧版ᵃ")
        self.assertEqual(renamed_source_name("书旗小说", "", ""), "书旗小说")

    def test_tidy_drawer_routes_to_rename_tool_inside_existing_workspace(self):
        root = Path(__file__).resolve().parents[1]
        drawer = (root / "frontend/src/components/TidyDrawer.vue").read_text(encoding="utf-8")
        self.assertIn('{ id: "rename", label: "批量改名" }', drawer)
        # 三栏骨架的 .tidy-main 收进抽屉的 unscoped 样式块后，工具组件不再自己带类
        self.assertIn("<SourceFamilyManager", drawer)
        self.assertNotIn("同源分组", drawer)


if __name__ == "__main__":
    unittest.main()
