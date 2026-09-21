# -*- coding: utf-8 -*-
"""页面侧的定层（`core/page_layer.py`）：判据一份，两个消费者。

为什么要钉它：层的判据原来只有前端一份，生成链（Python）判不了「这一页要不要引擎取」。
现在页面侧那半搬到这里、由 `core/jvm_debug` 写进 `pages[].layer`，**前端只渲染**——
所以这里的判据错一格，界面上和生成链上会同时错，而且都是「看着正常」的那种错。
"""

from __future__ import annotations

import unittest

from core.page_layer import (WANT_LINK, WANT_LIST, WANT_MEDIA, WANT_TEXT,
                             classify_page, has_wanted, page_stats)

STATIC = "<html><body><ul class='grid'><li><a href='/b/1'>甲</a></li><li><a href='/b/2'>乙</a></li></ul></body></html>"
PAYLOAD = "<html><body><script>var p = '" + "A" * 300 + "';</script><div id='chapter-images'></div></body></html>"


class ClassifyTests(unittest.TestCase):

    def test_static_page_is_l1(self):
        r = classify_page(STATIC, WANT_LIST)
        self.assertEqual(r["layer"], "L1")
        self.assertEqual(r["evidence"], [])
        self.assertTrue(r["has_wanted"])

    def test_encrypted_payload_is_l3_with_the_witness_line(self):
        r = classify_page(PAYLOAD, WANT_MEDIA)
        self.assertEqual(r["layer"], "L3")
        self.assertTrue(r["evidence"])
        ev = r["evidence"][0]
        self.assertIn("base64", ev["why"])
        self.assertGreaterEqual(ev["line"], 1, "证据要能指回原文行号")
        self.assertTrue(ev["snippet"])

    def test_xhr_and_blob_markers_are_l3_too(self):
        for mark in ("xhr_mode", "createObjectURL", "CryptoJS", "_0x1a2b3", "decrypt("):
            with self.subTest(mark=mark):
                r = classify_page("<html><script>var o = {%s: 1};</script></html>" % mark, WANT_LIST)
                self.assertEqual(r["layer"], "L3", mark)

    def test_no_target_but_api_calls_is_l4(self):
        r = classify_page("<div id='app'></div><script>fetch('/api/books')</script>", WANT_LIST)
        self.assertEqual(r["layer"], "L4")

    def test_empty_image_container_is_l2(self):
        # 图片源最常见：`<img>` 在、地址要脚本注入（口袋漫画那类正文页）
        r = classify_page("<div><img src=''><img></div>", WANT_MEDIA)
        self.assertEqual(r["layer"], "L2")

    def test_empty_hydration_mount_is_l2(self):
        r = classify_page("<html><body><div id='app'></div></body></html>", WANT_LIST)
        self.assertEqual(r["layer"], "L2")
        self.assertEqual(r["evidence"][0]["line"], 1)

    def test_login_word_is_a_fact_not_a_verdict(self):
        """L5 要源声明（`loginUrl` / cookie jar）——生成时还没有源，所以这里只给事实。"""
        r = classify_page("<html>请登录后继续</html>", WANT_TEXT)
        self.assertEqual(r["layer"], "")
        self.assertEqual(r["login_marker"], "请登录")

    def test_no_page_and_no_want_say_why(self):
        self.assertIn("没有页面", classify_page("", WANT_LIST)["unsure"])
        self.assertIn("要什么", classify_page(STATIC, "")["unsure"])
        # 有页面、有目标、但原文里没有目标也没有痕迹：不硬猜
        self.assertIn("没有目标", classify_page("<html><p>空</p></html>", WANT_LIST)["unsure"])

    def test_stats_and_thresholds_match_the_frontend(self):
        st = page_stats("<a href='/1'>x</a><img src='a.jpg'><img src=''><img>")
        self.assertEqual(st["links"], 1)
        self.assertEqual(st["images"], 3)
        self.assertEqual(st["images_with_src"], 1, "src 为空的占位不算")
        self.assertFalse(has_wanted(st, WANT_MEDIA), "只有一张真图 → 不算有正文图")
        self.assertTrue(has_wanted(st, WANT_LINK))
        self.assertIsNone(has_wanted(st, ""))
        self.assertIsNone(has_wanted(None, WANT_LIST))

    def test_want_table_covers_the_four_kinds(self):
        for want in (WANT_LIST, WANT_LINK, WANT_TEXT, WANT_MEDIA):
            with self.subTest(want=want):
                self.assertIsNotNone(has_wanted(page_stats(STATIC), want))


if __name__ == "__main__":
    unittest.main()
