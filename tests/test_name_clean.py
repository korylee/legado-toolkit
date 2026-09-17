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
            "去读书🎃#3": "去读书",
            "PO18文学🎃#12": "PO18文学",
            "52书库（po5.net）": "52书库",
            "中文书城~app-inter-bookstore.cread.com": "中文书城",
            "🐣醉读小说#书源.com": "醉读小说",
            "晴天小说4.0": "晴天小说",
            "大灰狼小说5.0(vip兼容版)": "大灰狼小说",
            "  塔读文学  ": "塔读文学",
            "小说123": "小说",
            "R 369小说网##小猫咪🐶": "R 369小说网",
        }
        for name, expected in cases.items():
            res = clean_source_name(name, 'https://a.com', existing_names={'小说'})
            self.assertEqual(res['name'], expected, name)


    def test_brand_numbers_are_protected(self):
        for name in ("52书库", "SF轻小说", "PO18", "PO18文学"):
            res = clean_source_name(name, "https://a.com")
            self.assertEqual(res["name"], name)
            self.assertFalse(res["changed"], name)

    def test_csharp_is_not_treated_as_hash_marker(self):
        for name in ("C#", "C#.NET"):
            res = clean_source_name(name, "https://a.com")
            self.assertEqual(res["name"], name)
            self.assertFalse(res["changed"])

    def test_trailing_number_needs_marker_or_existing_base(self):
        # 没有分隔标记、去掉数字后的名字也不在库里 -> 保留
        self.assertEqual(
            clean_source_name("小说123", "https://a.com", existing_names=set())["name"],
            "小说123")
        # 去掉数字后的名字已在库里 -> 删除
        self.assertEqual(
            clean_source_name("小说123", "https://a.com", existing_names={"小说"})["name"],
            "小说")
        # emoji / # 这类分隔标记 -> 删除
        self.assertEqual(clean_source_name("测试名🎃12", "https://a.com")["name"], "测试名")

    def test_empty_or_short_falls_back_to_domain_main(self):
        res = clean_source_name("[api]", "https://m.example.com/path",
                                existing_names=set())
        self.assertEqual(res["name"], "example")
        self.assertIn("fallback_domain", res["reasons"])
        res2 = clean_source_name("[api]", "", existing_names=set())
        self.assertEqual(res2["name"], "[api]")
        self.assertIn("no_safe_fallback", res2["reasons"])
        self.assertLess(res2["confidence"], 0.5)

    def test_protected_type_emoji_is_not_stripped(self):
        res = clean_source_name("🎧听书", "https://a.com")
        self.assertEqual(res["name"], "🎧听书")
        self.assertFalse(res["changed"])

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
