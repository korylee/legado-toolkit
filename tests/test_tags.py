# -*- coding: utf-8 -*-
"""标签规范化与系统/用户拆分测试。"""

from __future__ import annotations

import unittest

from core.tags import (
    canonical_tags,
    extract_user_tags_from_group,
    is_system_tag,
    merge_group,
    normalize_group,
    normalize_tags,
    parse_group_tags,
    split_system_user,
)


class TagUtilsTests(unittest.TestCase):
    def test_normalize_tags_dedupes_and_keeps_order(self) -> None:
        self.assertEqual(normalize_tags("原创, 精排，原创;;R18"), ["原创", "精排", "R18"])

    def test_parse_legacy_group_splits_star_markers(self) -> None:
        self.assertEqual(parse_group_tags("📖小说/✅★★★★★,原创"), ["📖小说", "✅★★★★★", "原创"])

    def test_split_system_user(self) -> None:
        tags = parse_group_tags("📖小说,可用,原创,R18")
        self.assertEqual(split_system_user(tags), (["📖小说", "可用"], ["原创", "R18"]))

    def test_merge_group_keeps_system_first(self) -> None:
        self.assertEqual(merge_group(["📖小说", "可用", "原创"], ["R18", "原创"]), "📖小说,可用,原创,R18")

    def test_extract_user_tags_skips_legacy_system_markers(self) -> None:
        self.assertEqual(extract_user_tags_from_group("📖小说/✅★★★★★,原创,H漫"), ["原创", "R18"])

    def test_normalize_group_reorders(self) -> None:
        self.assertEqual(normalize_group("原创,📖小说,可用"), "📖小说,可用,原创")

    def test_system_tag_exact_match(self) -> None:
        self.assertTrue(is_system_tag("可用"))
        self.assertFalse(is_system_tag("原创"))

    def test_download_source_type_tag_is_system(self) -> None:
        # 类型标签对齐 Legado：3 是「只提供下载服务的网站」，标签为「📥下载」
        self.assertTrue(is_system_tag("📥下载"))
        self.assertEqual(
            split_system_user(parse_group_tags("📥下载,可用,原创")),
            (["📥下载", "可用"], ["原创"]),
        )

    def test_alias_mapping(self) -> None:
        self.assertEqual(canonical_tags("H漫,精品排版,原创"), ["R18", "精排", "原创"])

    def test_extract_user_tags_maps_aliases(self) -> None:
        self.assertEqual(extract_user_tags_from_group("📖小说,可用,H漫,精品排版"), ["R18", "精排"])


if __name__ == "__main__":
    unittest.main()
