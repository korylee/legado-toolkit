# -*- coding: utf-8 -*-
"""页面引用的外部脚本（`core/js_hints.py`）：取哪几份、跳过哪几份、什么时候才去取。

为什么钉这些：这一层的产物**直接改变定层结论**（小爱漫画章节页就是靠它从 L2 改判 L3 的，
2026-09-21），而它的代价是「每页多发几次请求」——所以「取哪几份」（排序 / 上限 / 过滤）与
「什么时候才取」（只在 L2 / 判不了时）都要有断言钉着。取脚本一律走注入的假取页器，
**测试不联网**。
"""
from __future__ import annotations

import unittest

from core.js_hints import (MAX_DOCS, classify_with_scripts, collect_js_docs,
                           script_srcs)

BASE = "https://www.xiaoaimanhua.com/103206/37.html"
PAGE = ("<html><head>"
        "<script src='/static/js/jquery.min.js'></script>"
        "<script src='https://cdn.example.org/lib.js'></script>"
        "<script src='/static/js/cms-2.0.1.min.js?v=1.0.0'></script>"
        "<script src='/static/libs/crypto-js/4.1.1/crypto-js.min.js'></script>"
        "<script src='https://edge.xiaoaimanhua.com/script.js'></script>"
        "</head><body><div id='chapter-images'><img><img></div></body></html>")
#: 同站的子域（`edge.` 那份实测是统计脚本）算「本站」，跨站的那份不算
CMS_URL = "https://www.xiaoaimanhua.com/static/js/cms-2.0.1.min.js?v=1.0.0"
EDGE_URL = "https://edge.xiaoaimanhua.com/script.js"
JS = {
    CMS_URL: "var CMS={chapter:{init:function(r){params=this.decrypt(params)}}};",
    EDGE_URL: "fetch('/report')",
    "https://www.xiaoaimanhua.com/static/libs/crypto-js/4.1.1/crypto-js.min.js":
        "CryptoJS",
    "https://www.xiaoaimanhua.com/static/js/jquery.min.js": "jQuery",
    "https://cdn.example.org/lib.js": "window.lib = 1",
}


class FakeFetcher:
    """记下被问了哪几份、回什么（`boom` 里的抛异常，模拟抓不到）。"""

    def __init__(self, mapping, boom=()):
        self.mapping = mapping
        self.boom = set(boom)
        self.asked = []

    def __call__(self, url):
        self.asked.append(url)
        if url in self.boom:
            raise RuntimeError("连不上")
        return self.mapping.get(url, "")


class ScriptSrcTests(unittest.TestCase):

    def test_srcs_are_absolute_deduped_and_in_document_order(self):
        html = ("<script src='/a.js'></script><script src=\"https://x.com/b.js\"></script>"
                "<script src='/a.js'></script>")
        self.assertEqual(script_srcs(html, BASE),
                         ["https://www.xiaoaimanhua.com/a.js", "https://x.com/b.js"])

    def test_non_http_src_is_dropped(self):
        self.assertEqual(script_srcs("<script src='data:x.js'></script>", BASE), [])


class CollectTests(unittest.TestCase):

    def test_cross_site_and_common_libs_are_skipped_with_a_reason(self):
        f = FakeFetcher(JS)
        got = collect_js_docs(PAGE, BASE, fetcher=f)
        self.assertEqual([d["url"] for d in got["docs"]], [CMS_URL, EDGE_URL],
                         "站点自己的 bundle 排在同站子域前面")
        why = {s["url"]: s["why"] for s in got["skipped"]}
        self.assertIn("不是本站的脚本", why["https://cdn.example.org/lib.js"])
        self.assertIn("常见库", why["https://www.xiaoaimanhua.com/static/js/jquery.min.js"])
        self.assertIn("常见库",
                      why["https://www.xiaoaimanhua.com/static/libs/crypto-js/4.1.1/crypto-js.min.js"])
        self.assertEqual(got["considered"], 5, "页面上引用的总数（含跳过）")

    def test_the_cap_keeps_the_page_sown_bundle_first(self):
        html = "".join("<script src='/static/js/%s.js'></script>" % n
                       for n in ("zzz", "yyy", "cms-2.0", "xxx"))
        f = FakeFetcher({})
        got = collect_js_docs(html, BASE, fetcher=f)
        self.assertEqual(len(got["docs"]), MAX_DOCS)
        self.assertIn("cms-2.0", got["docs"][0]["url"], "带题材词的排进上限之内")
        self.assertTrue(any("上限" in s["why"] for s in got["skipped"]))

    def test_no_script_means_no_request_at_all(self):
        f = FakeFetcher({})
        got = collect_js_docs("<html><body>没有脚本</body></html>", BASE, fetcher=f)
        self.assertEqual((got["docs"], f.asked), ([], []))

    def test_fetch_failure_is_a_reason_not_an_exception(self):
        got = collect_js_docs(PAGE, BASE, fetcher=FakeFetcher(JS, boom=[CMS_URL, EDGE_URL]))
        self.assertEqual(got["docs"], [])
        self.assertTrue(any("抓不到" in s["why"] for s in got["skipped"]))

    def test_long_script_is_truncated_and_says_so(self):
        got = collect_js_docs(PAGE, BASE, fetcher=FakeFetcher({CMS_URL: "A" * 500}),
                              max_bytes=100)
        self.assertEqual(len(got["docs"][0]["text"]), 100)
        self.assertTrue(got["docs"][0]["truncated"])


class DeepJudgementTests(unittest.TestCase):
    """什么时候才值得为脚本多发请求——`classify_with_scripts` 的那道闸。"""

    #: L1 页上也挂一份脚本——**不挂的话「L1 不取脚本」这条断言是白写的**：
    #: 没有脚本时本来就不会发请求，闸门删掉也看不出来（变异验证就是这么抓到的）
    L1 = ("<html><head><script src='/static/js/app.js'></script></head>"
          "<body><ul><li><a href='/b/1'>甲</a></li></ul></body></html>")
    L2 = "<html><body><div id='chapter-images'><img><img></div></body></html>"

    def test_l1_never_pays_for_scripts(self):
        f = FakeFetcher(JS)
        got = classify_with_scripts(self.L1, "list", BASE, fetcher=f)
        self.assertEqual(got["verdict"]["layer"], "L1")
        self.assertEqual(f.asked, [], "L1 已经定了，不该再看脚本")

    def test_l2_is_rejudged_with_the_scripts(self):
        page = self.L2 + "<script src='/static/js/cms-2.0.1.min.js'></script>"
        f = FakeFetcher({"https://www.xiaoaimanhua.com/static/js/cms-2.0.1.min.js":
                         "var p=params;CryptoJS.decrypt(p)"})
        got = classify_with_scripts(page, "media", BASE, fetcher=f)
        self.assertEqual(got["light_layer"], "L2")
        self.assertEqual(got["verdict"]["layer"], "L3", "脚本改了结论")
        self.assertEqual(len(f.asked), 1, "只问了一次")

    def test_bundle_noise_does_not_make_every_spa_an_l4(self):
        """**反例**：bundle 里遍地是 `fetch(`，照页面那套判会把每个 SPA 都判成 L4。"""
        page = self.L2 + "<script src='/static/js/app.js'></script>"
        f = FakeFetcher({"https://www.xiaoaimanhua.com/static/js/app.js":
                         "function load(e){return fetch(e).then(function(r){return r.json()})}"})
        got = classify_with_scripts(page, "media", BASE, fetcher=f)
        self.assertEqual(got["verdict"]["layer"], "L2")

    def test_a_real_call_in_the_bundle_is_l4(self):
        page = ("<html><body><div id='app'></div>"
                "<script src='/static/js/app.js'></script></body></html>")
        f = FakeFetcher({"https://www.xiaoaimanhua.com/static/js/app.js":
                         "$.ajax({url:'/index/ajax/view',type:'POST',data:{aid:1}})"})
        got = classify_with_scripts(page, "list", BASE, fetcher=f)
        self.assertEqual(got["verdict"]["layer"], "L4")
        self.assertEqual(got["verdict"]["evidence"][0]["source"],
                         "https://www.xiaoaimanhua.com/static/js/app.js",
                         "证据要指回是哪一份脚本")


if __name__ == "__main__":
    unittest.main()
