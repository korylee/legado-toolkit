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
#: 小爱漫画章节页的形状（2026-09-21 实测原文）：负载是**逗号连写**的赋值
#: （`var config = {…}, params = '…'`），图片列表在解密后的 `params.chapter_images` 里
XIAOAI = ("<html><body><script>var config = {index_url: '/'}, params = '%s';</script>"
          "<div id='chapter-images'><img><img></div></body></html>" % ("s4pZ" * 80))
#: 加密库是外部脚本时，页面上只有这一行 `<script src>`，没有 `CryptoJS` 这个标识符
CRYPTO_LIB = ("<html><head><script src='/static/libs/crypto-js/4.1.1/crypto-js.min.js'>"
              "</script></head><body><div id='chapter-images'><img><img></div></body></html>")


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

    def test_comma_chained_payload_is_l3(self):
        """小爱漫画那种写法（2026-09-21）：`var config = {…}, params = '…'`。

        判据是「一整段 200+ 字符的 base64 字面量」，**不是**「`var X = '…'`」——
        按后一种写法这一页会被判成 L2（图片容器在、图没地址），真档位是 L3。
        """
        r = classify_page(XIAOAI, WANT_MEDIA)
        self.assertEqual(r["layer"], "L3")
        self.assertIn("base64", r["evidence"][0]["why"])

    def test_crypto_library_in_a_script_src_is_l3(self):
        """库是外部脚本时页面上没有 `CryptoJS` 标识符，只有那一行 `<script src>`。"""
        r = classify_page(CRYPTO_LIB, WANT_MEDIA)
        self.assertEqual(r["layer"], "L3")
        self.assertIn("加密库", r["evidence"][0]["why"])

    def test_script_evidence_points_back_at_the_script(self):
        """脚本里的证据与页面里的长得一样——所以要标出是哪一份（否则用户没法复核）。"""
        url = "https://www.xiaoaimanhua.com/static/js/cms-2.0.1.min.js"
        r = classify_page(XIAOAI, WANT_MEDIA,
                          js_docs=[{"url": url, "text": "params=this.decrypt(params)"}])
        self.assertEqual(r["layer"], "L3")
        self.assertEqual(r["evidence"][0]["source"], "")
        srcs = [e["source"] for e in r["evidence"]]
        self.assertIn(url, srcs)
        self.assertTrue(r["evidence"][srcs.index(url)]["snippet"])

    def test_a_plain_bundle_call_does_not_make_it_l4(self):
        """**反例**：bundle 里 `fetch(` 遍地都是，照页面那套判会把每个 SPA 都判成 L4。"""
        r = classify_page("<div><img src=''><img></div>", WANT_MEDIA,
                          js_docs=[{"url": "https://a.com/app.js",
                                    "text": "function load(e){return fetch(e)}"}])
        self.assertEqual(r["layer"], "L2")

    def test_unsure_says_it_looked_at_the_scripts(self):
        r = classify_page("<html><p>空</p></html>", WANT_LIST,
                          js_docs=[{"url": "https://a.com/app.js", "text": "var a=1"}])
        self.assertIn("脚本", r["unsure"])

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
