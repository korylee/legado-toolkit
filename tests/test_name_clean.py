# -*- coding: utf-8 -*-
"""名称清洗纯规则测试（不碰数据库）。"""

from __future__ import annotations

import unittest

from core.name_clean import clean_source_name, clean_sources


class CleanNameTests(unittest.TestCase):
    def test_examples_from_todo(self):
        cases = {
            "🍩笔趣阁[自写/wap.biqige.info]": "笔趣阁",
            "最大资源网-http://zuidazy.org": "最大资源网",
            "♛ 夜伴书屋 #一程1101": "夜伴书屋",
            "去读书🎃#3": "去读书🎃#3",
            "PO18文学🎃#12": "PO18文学🎃#12",
            "52书库（po5.net）": "52书库",
            "中文书城~app-inter-bookstore.cread.com": "中文书城",
            "🐣醉读小说#书源.com": "醉读小说",
            "晴天小说4.0": "晴天小说4.0",
            "大灰狼小说5.0(vip兼容版)": "大灰狼小说5.0",
            "  塔读文学  ": "塔读文学",
            "小说123": "小说123",
            "R 369小说网##小猫咪🐶": "R 369小说网",
        }
        for name, expected in cases.items():
            res = clean_source_name(name)
            self.assertEqual(res['name'], expected, name)


    def test_timestamps_are_suggested_but_numeric_badges_stay(self):
        timestamp = clean_source_name("站点（2024-05-01）")
        self.assertEqual(timestamp["name"], "站点")
        self.assertIn("remove_timestamp", timestamp["reasons"])
        self.assertLessEqual(timestamp["confidence"], 0.85)
        self.assertEqual(clean_source_name("站点2")["name"], "站点2")

    def test_configured_rules_share_one_regex_executor(self):
        rules = [{"id": "remove_signature", "pattern": "分享者甲", "replacement": "",
                  "enabled": True, "confidence": 0.85}]
        custom = clean_source_name("作品-分享者甲", rules=rules)
        self.assertEqual(custom["name"], "作品-")
        self.assertIn("remove_signature", custom["reasons"])
        self.assertLessEqual(custom["confidence"], 0.85)
        timestamp_rules = [{"id": "timestamp", "pattern": "-20\\d{2}-\\d{2}-\\d{2}$",
                            "replacement": "", "enabled": True, "confidence": 0.85}]
        timestamp = clean_source_name("站点-2024-05-01", rules=timestamp_rules)
        self.assertEqual(timestamp["name"], "站点")
        disabled = clean_source_name("站点-2024-05-01", rules=[])
        self.assertEqual(disabled["name"], "站点-2024-05-01")

    def test_bare_domain_after_hash_is_preserved_as_ambiguous_name(self):
        result = clean_source_name("⭐️飞速中文•m#书源.com\r")
        self.assertEqual(result["name"], "飞速中文•m#书源.com")
        self.assertNotIn("remove_address", result["reasons"])

    def test_edge_decoration_keeps_bracket_rating_content(self):
        result = clean_source_name("天涯知识（优+）\r")
        self.assertEqual(result["name"], "天涯知识（优+）")
        self.assertIn("strip_decor", result["reasons"])

    def test_independent_edition_marker_is_removed_without_losing_version(self):
        from core.name_clean import load_config
        result = clean_source_name("🍅大灰狼漫画5.0(vip独立版)\r", load_config()["rules"])
        self.assertEqual(result["name"], "大灰狼漫画5.0")
        self.assertTrue(result["changed"])
        self.assertIn("remove_tech_marker", result["reasons"])

        for name in ("52书库", "SF轻小说", "PO18", "PO18文学"):
            res = clean_source_name(name)
            self.assertEqual(res["name"], name)
            self.assertFalse(res["changed"], name)

    def test_csharp_is_not_treated_as_hash_marker(self):
        for name in ("C#", "C#.NET"):
            res = clean_source_name(name)
            self.assertEqual(res["name"], name)
            self.assertFalse(res["changed"])

    def test_trailing_numbers_are_preserved_as_source_distinguishers(self):
        for name in ("小说123", "小说🎃12", "小说#3", "站点4.0"):
            self.assertEqual(clean_source_name(name)["name"], name)

    def test_empty_result_keeps_original_name(self):
        rules = [{"id": "remove_all", "pattern": ".+", "replacement": "",
                  "enabled": True, "confidence": 0.85}]
        result = clean_source_name("书源", rules=rules)
        self.assertEqual(result["name"], "书源")
        self.assertFalse(result["changed"])
        self.assertIn("no_safe_fallback", result["reasons"])
        self.assertLess(result["confidence"], 0.5)

    def test_unicode_property_decor_cleanup_preserves_brackets_and_badges(self):
        self.assertEqual(clean_source_name("🎧书源#2")["name"], "书源#2")
        bracketed = clean_source_name("天涯知识（优+）\r")
        self.assertEqual(bracketed["name"], "天涯知识（优+）")
        self.assertIn("strip_decor", bracketed["reasons"])
        self.assertEqual(clean_source_name("去读书#3")["name"], "去读书#3")

    def test_clean_sources_shapes_preview_rows(self):
        rows = clean_sources([
            {"bookSourceName": "♛ 夜伴书屋 #一程1101", "bookSourceUrl": "https://a.com"},
            {"bookSourceName": "52书库", "bookSourceUrl": "https://b.com"},
        ])
        changed = [r for r in rows if r["changed"]]
        self.assertEqual(len(changed), 1)
        self.assertEqual(changed[0]["old_name"], "♛ 夜伴书屋 #一程1101")
        self.assertEqual(changed[0]["new_name"], "夜伴书屋")
        self.assertIn("reasons", changed[0])


if __name__ == "__main__":
    unittest.main()
