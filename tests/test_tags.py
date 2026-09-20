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


class SystemStatusTableTests(unittest.TestCase):
    """两张状态标签表必须同集合。

    `core/tags.SYSTEM_STATUS_TAG_ORDER` 是**判定表**（`is_system_tag` 查它的派生
    set），`core/organizer.STATUS_GROUP_NAMES` 是写出去的那一侧（`group_title`
    用它拼分组名）。少登记一个的后果不是「少显示一个标签」，而是那个标签被当成
    **用户标签**：写进 user_tags、界面上可编辑、源修好之后也不会自动清掉。

    实测过的现场：`证书问题` 一度只在 organizer 那张表里，于是
    `extract_user_tags_from_group('📖小说,证书问题')` 返回 `['证书问题']`。
    """

    def test_every_group_name_is_a_system_tag(self):
        from core import organizer, tags
        missing = set(organizer.STATUS_GROUP_NAMES.values()) - tags.SYSTEM_STATUS_TAGS
        self.assertEqual(
            missing, set(), "organizer 会写出这些标签，但判定表不认它们 → 会落进 user_tags")

    def test_no_status_group_leaks_into_user_tags(self):
        """逐个健康态验：拼成分组名再拆回来，用户标签必须是空的。"""
        from core import organizer, tags
        for health in organizer.STATUS_GROUP_NAMES:
            with self.subTest(health=health):
                group = organizer.group_title(0, health)
                self.assertEqual(tags.extract_user_tags_from_group(group), [], group)

    def test_retired_words_map_onto_current_tags(self):
        """退役词 → 现役词的表：**值必须是现役标签，键不能是现役标签**。

        这张表有两个用法（存量换词迁移 + 旧分组回读），写错的后果与上面那条同源：
        值不在 `SYSTEM_STATUS_TAG_ORDER` 里 → 迁移把一个**判定表不认的词**写进
        group_name，前端随即把它当用户标签——正是这张表要修的那件事。
        键是现役标签的话，「换名」就成了把 A 状态改成 B 状态，属于改词写反了。
        """
        from core import tags

        bad_values = (set(tags.RETIRED_STATUS_TAG_RENAMES.values())
                      - set(tags.SYSTEM_STATUS_TAG_ORDER))
        self.assertEqual(bad_values, set(),
                         "映射值不是现役状态标签 → 换完会落进 user_tags")
        bad_keys = (set(tags.RETIRED_STATUS_TAG_RENAMES)
                    & set(tags.SYSTEM_STATUS_TAG_ORDER))
        self.assertEqual(bad_keys, set(), "映射的键是现役标签 → 改词写反了")

    def test_every_retired_word_is_recognized_as_system(self):
        """退役词必须仍被认成系统侧——**遍历映射表**，不写死具体词。

        漏认的后果不是「显示错」，而是旧分组里的它被 `extract_user_tags_from_group`
        当成用户标签写进 user_tags 且不再纠正。写死「需验证 / 需代理复检」时，
        下次改词新词不进覆盖而用例照旧全绿，所以这里跟着表长。
        """
        from core import tags
        for old in tags.RETIRED_STATUS_TAG_RENAMES:
            with self.subTest(word=old):
                self.assertEqual(
                    tags.extract_user_tags_from_group("📖小说,%s" % old), [],
                    "「%s」被当成用户标签了" % old)


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------- 变异记录
# 以下为实测（照项目惯例：改坏 → 跑 → 确认变红 → 改回）。
#
#  M1  SYSTEM_STATUS_TAG_ORDER 去掉「证书问题」（它只在 organizer 那张表里）
#        → SystemStatusTableTests 红 2 条；现场表现是那个标签落进 user_tags
