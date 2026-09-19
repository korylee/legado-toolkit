# -*- coding: utf-8 -*-
"""fetch 的请求头 / 编码 / 代理支持。"""

import io
import unittest
from urllib.error import HTTPError
from unittest.mock import patch

from core.fetch import parse_source_header
from core import fetch as F


def setUpModule():
    """页面缓存是**模块级**状态，会跨用例、跨文件串味（同 ``_rate_last``）。
    进本模块先清一次，走的时候再清一次，别把实抓的页面留给后面的模块。"""
    F.page_cache_clear()


def tearDownModule():
    F.page_cache_clear()


class ParseSourceHeaderTests(unittest.TestCase):
    def test_json_form(self):
        h, why = parse_source_header('{"User-Agent":"X","Referer":"https://a.com"}')
        self.assertEqual(h["User-Agent"], "X")
        self.assertEqual(h["Referer"], "https://a.com")
        self.assertEqual(why, "")

    def test_line_form(self):
        h, why = parse_source_header("User-Agent: X\nReferer: https://a.com")
        self.assertEqual(h["User-Agent"], "X")
        self.assertEqual(h["Referer"], "https://a.com")
        self.assertEqual(why, "")

    def test_js_header_reported_not_applied(self):
        h, why = parse_source_header('<js>return {"a":"b"}</js>')
        self.assertEqual(h, {})
        self.assertTrue(why)

    def test_empty(self):
        h, why = parse_source_header("")
        self.assertEqual(h, {})
        self.assertEqual(why, "")

    def test_garbage_does_not_raise(self):
        h, why = parse_source_header("这不是 header")
        self.assertEqual(h, {})

    def test_none_means_unset(self):
        """None 等价于「字段未设置」，返回空头且不给原因。"""
        h, why = parse_source_header(None)
        self.assertEqual(h, {})
        self.assertEqual(why, "")

    def test_dirty_strings_do_not_raise(self):
        """半截 JSON / 非对象 JSON / 空 key / 只有冒号等，都不能抛异常。"""
        for raw in ('{"a":', "[1,2]", '"str"', "123", "null", "true",
                    ":", ": value", "A:", "   ", "abc\ndef"):
            with self.subTest(raw=raw):
                h, why = parse_source_header(raw)
                self.assertIsInstance(h, dict)

    def test_half_json_reports_reason(self):
        """半截 JSON 要给出原因，而不是静默当成换行写法丢掉。"""
        h, why = parse_source_header('{"a":')
        self.assertEqual(h, {})
        self.assertTrue(why)

    def test_non_string_does_not_raise(self):
        """非字符串是真实的脏值来源（书源 JSON 里的类型没人保证）。"""
        for bad in (123, 1.5, True, [1, 2], {"a": 1}, b"User-Agent: X"):
            with self.subTest(bad=repr(bad)):
                h, why = parse_source_header(bad)
                self.assertEqual(h, {})
                self.assertTrue(why, "不能静默吞掉：%r" % (bad,))

    def test_bom_json_is_parsed_not_shredded(self):
        """BOM 开头的 JSON 必须先去 BOM。

        不去的话 `startswith("{")` 为 False，会落到换行分隔分支，被解析成
        key='\\ufeff{"a"' / value='"b"}' —— 一个**垃圾头会被真的发给服务器**，
        比丢掉更糟（最终会表现成「源坏了」）。
        """
        h, why = parse_source_header('\ufeff{"User-Agent":"X"}')
        self.assertEqual(h, {"User-Agent": "X"})
        self.assertEqual(why, "")

    def test_bom_after_leading_whitespace_is_parsed(self):
        """BOM 前有空格时同样要剥离。

        lstrip("\\ufeff") 只剥开头**连续**的 BOM：raw 前若带空格，BOM 就不在首位，
        剥不掉 → startswith("{") 为 False → 落进换行分支 → 又是垃圾头，而且这次是
        静默的（why 仍为 ""）。
        """
        h, why = parse_source_header('  \ufeff{"User-Agent":"X"}')
        self.assertEqual(h, {"User-Agent": "X"})
        self.assertEqual(why, "")

    def test_non_object_json_reports_reason(self):
        """不是对象的 JSON 要给原因，不能静默返回空头。"""
        for bad in ('[1, 2]', '"str"', '123', 'null'):
            with self.subTest(bad=bad):
                h, why = parse_source_header(bad)
                self.assertEqual(h, {})
                self.assertTrue(why, "必须说明为什么没用上：%s" % bad)

    def test_value_containing_colon_is_kept(self):
        """换行写法里值本身含冒号（URL 带端口）不能被截断；CRLF 同验。"""
        h, why = parse_source_header(
            "User-Agent: X\r\nReferer: https://a.com:8080/x")
        self.assertEqual(h["User-Agent"], "X")
        self.assertEqual(h["Referer"], "https://a.com:8080/x")
        self.assertEqual(why, "")


class FetchSignatureTests(unittest.TestCase):
    def test_accepts_new_kwargs(self):
        """只验证签名，不发真实请求。"""
        import inspect
        sig = inspect.signature(F.fetch)
        for name in ("headers", "charset", "proxy", "source"):
            self.assertIn(name, sig.parameters)

    def test_signature_defaults_and_order(self):
        """新参数必须都有默认值，且追加在原 timeout 之后——否则既有位置调用会错位。"""
        import inspect
        sig = inspect.signature(F.fetch)
        self.assertEqual(list(sig.parameters),
                         ["url", "timeout", "headers", "charset", "proxy", "source"])
        for name in ("headers", "charset", "proxy", "source"):
            self.assertIsNot(sig.parameters[name].default, inspect.Parameter.empty)
        self.assertEqual(sig.parameters["timeout"].default, 15)
        # source 默认 None：不传就只是那条链路不受限速约束，不能报错
        self.assertIsNone(sig.parameters["source"].default)


class CacheIsolatedTestCase(unittest.TestCase):
    """页面缓存是**模块级**状态，每条用例都从空缓存开始。

    不清的话，前一条用例抓过的页面会让后一条用例**不再发请求**——`urlopen 被
    调用` 这类断言随即失效，而失效原因与它要验的行为毫无关系（实测：本文件里
    `test_no_proxy_uses_urlopen_directly` 等 8 处会因此变红）。
    """

    def setUp(self):
        F.page_cache_clear()
        self.addCleanup(F.page_cache_clear)


class FetchBehaviorTests(CacheIsolatedTestCase):
    """三个卖点的行为覆盖。没有这些用例，整段实现删空也不会有人发现。"""

    def _capture(self, **kw):
        """跑一次 fetch，返回实际发出去的 Request 对象与 opener 是否被用过。"""
        sent = {}
        body = "正文".encode("utf-8")

        class FakeResp:
            def read(self): return body
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_urlopen(req, timeout=None):
            sent["req"] = req
            return FakeResp()

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            F.fetch("https://a.com/x", **kw)
        return sent["req"]

    def test_default_ua_present(self):
        req = self._capture()
        self.assertIn("User-agent", req.headers)
        self.assertEqual(req.headers["User-agent"], F.DEFAULT_UA)

    def test_non_ascii_url_is_percent_encoded(self):
        """URL 里的中文要编码，否则 urllib 发送时按 ascii 编码直接抛。

        实测来源：连 App 调试时 App 给的搜索 URL 就是 `...?q=我` 这种**未编码**
        原样形态，不补这一步的话 search 页永远抓不到（报 'ascii' codec can't
        encode character）。
        """
        sent = {}

        class FakeResp:
            def read(self): return b"x"

            def __enter__(self): return self

            def __exit__(self, *a): return False

        def fake_urlopen(req, timeout=None):
            sent["req"] = req
            return FakeResp()

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            F.fetch("https://a.com/so/search.php?q=我")
        self.assertIn("%E6%88%91", sent["req"].full_url)
        self.assertNotIn("我", sent["req"].full_url)

    def test_already_encoded_url_is_not_double_encoded(self):
        """已经编码好的 %XX 不能再编一遍（否则变成 %25XX，URL 就错了）。

        这条与上一条是一对：编码必须**幂等**。
        """
        sent = {}

        class FakeResp:
            def read(self): return b"x"

            def __enter__(self): return self

            def __exit__(self, *a): return False

        def fake_urlopen(req, timeout=None):
            sent["req"] = req
            return FakeResp()

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            F.fetch("https://a.com/so/search.php?q=%E6%88%91&p=2#frag")
        self.assertEqual(
            sent["req"].full_url,
            "https://a.com/so/search.php?q=%E6%88%91&p=2#frag")

    def test_headers_are_merged(self):
        req = self._capture(headers={"Referer": "https://a.com"})
        self.assertEqual(req.headers["Referer"], "https://a.com")

    def test_empty_header_value_does_not_wipe_default_ua(self):
        """值为空的键不能覆盖默认 UA。用 if v 挡不住纯空白——urllib 发送前会 strip，
        结果服务端收到空 UA。"""
        for empty in ("", "   ", None):
            with self.subTest(empty=repr(empty)):
                # 三轮是**同一个请求身份**（空 UA 被丢掉 → 都等于默认 UA），
                # 不清缓存的话第二、三轮直接命中，Request 压根不会被构造出来
                F.page_cache_clear()
                req = self._capture(headers={"User-Agent": empty})
                self.assertEqual(req.headers["User-agent"], F.DEFAULT_UA)

    def test_charset_gbk_decodes(self):
        gbk_body = "绍宋".encode("gbk")

        class FakeResp:
            def read(self): return gbk_body
            def __enter__(self): return self
            def __exit__(self, *a): return False

        with patch("urllib.request.urlopen", return_value=FakeResp()):
            html = F.fetch("https://a.com/x", charset="gbk")
        self.assertIn("绍宋", html)

    def test_dirty_charset_does_not_raise(self):
        """charset 与 header 同源，都是书源 JSON 里的脏值。"""
        for bad in (123, ["gbk"], {"a": 1}, "no-such-codec"):
            with self.subTest(bad=repr(bad)):
                # 非字符串的几种在缓存键里都算「没传」（解码分支也只认字符串），
                # 所以要各自从空缓存起跑——否则后几轮直接命中，走不到解码那一步
                F.page_cache_clear()
                html = None
                class FakeResp:
                    def read(self): return "x".encode("utf-8")
                    def __enter__(self): return self
                    def __exit__(self, *a): return False
                with patch("urllib.request.urlopen", return_value=FakeResp()):
                    html = F.fetch("https://a.com/x", charset=bad)
                self.assertEqual(html, "x")

    def test_proxy_uses_opener(self):
        """proxy 非空必须走 build_opener(ProxyHandler)，不能直连。"""
        called = {}

        class FakeOpener:
            def open(self, req, timeout=None):
                called["proxy_opener"] = True
                class R:
                    def read(self): return b"ok"
                    def __enter__(self): return self
                    def __exit__(self, *a): return False
                return R()

        with patch("urllib.request.build_opener", return_value=FakeOpener()) as bo:
            html = F.fetch("https://a.com/x", proxy="http://127.0.0.1:1")
        self.assertTrue(called.get("proxy_opener"), "proxy 非空却没走 opener")
        self.assertTrue(bo.called, "没调用 build_opener")

    def test_no_proxy_uses_urlopen_directly(self):
        """proxy 留空必须直连。"""
        class FakeResp:
            def read(self): return b"ok"
            def __enter__(self): return self
            def __exit__(self, *a): return False

        with patch("urllib.request.urlopen", return_value=FakeResp()) as uo, \
             patch("urllib.request.build_opener") as bo:
            F.fetch("https://a.com/x")
        self.assertTrue(uo.called, "没走直连")
        self.assertFalse(bo.called, "不该建 opener")


class RateLimitTests(CacheIsolatedTestCase):
    """fetch 侧的限速：源声明了 concurrentRate，每条抓取链路都得遵守。

    checker 走异步 aiohttp、其余三条（全链路试跑 / 连 App 调试补抓 / 快速新增源）
    走本模块。**两条链路必须用同一份解析**——复制一份到自己模块里就是第二个口径。
    """

    def setUp(self):
        super().setUp()          # 页面缓存同为空（见 CacheIsolatedTestCase）
        # 模块级状态，用例之间必须清干净，否则会互相插间隔
        F._rate_last.clear()
        self.addCleanup(F._rate_last.clear)

    @staticmethod
    def _resp(*_a, **_k):
        class FakeResp:
            def read(self): return b"ok"
            def __enter__(self): return self
            def __exit__(self, *exc): return False
        return FakeResp()

    def test_parser_is_the_same_object_checker_uses(self):
        from core import checker, quality
        self.assertIs(quality.rate_interval_ms, checker.rate_interval_ms)

    def test_rate_key_for_absent_or_unparsable_rates(self):
        # 不传 source 不能报错（add_source 的探测阶段就是没有源的状态），
        # 也不能凭空插一个间隔
        for source in (None, {}, [], "x",
                       {"bookSourceUrl": "https://a/"},
                       {"bookSourceUrl": "https://a/", "concurrentRate": "0"},
                       {"bookSourceUrl": "https://a/", "concurrentRate": "abc"}):
            with self.subTest(source=source):
                self.assertEqual(F._rate_key(source), ("", 0))

    def test_rate_key_keeps_the_raw_url(self):
        # 键必须是导入原文：规范化过的 URL（去尾斜杠 / 转小写）会把两个不同的源
        # 合成一个键，等于给它们共用一份限速预算
        self.assertEqual(
            F._rate_key({"bookSourceUrl": "https://A.com/Path/",
                         "concurrentRate": "1/2000"}),
            ("https://A.com/Path/", 2000))

    def test_fetch_hands_the_source_to_the_throttle(self):
        # 接线验证：只测 _throttle 本身的话，把 fetch 里那行删掉也照样全绿
        calls = []
        with patch.object(F, "_throttle", side_effect=lambda *a: calls.append(a)), \
             patch("urllib.request.urlopen", side_effect=self._resp):
            F.fetch("https://a.com/x",
                    source={"bookSourceUrl": "https://a.com/",
                            "concurrentRate": "1/500"})
        self.assertEqual(calls, [("https://a.com/", 500)])

    def test_fetch_without_source_still_works(self):
        calls = []
        with patch.object(F, "_throttle", side_effect=lambda *a: calls.append(a)), \
             patch("urllib.request.urlopen", side_effect=self._resp):
            self.assertEqual(F.fetch("https://a.com/x"), "ok")
        self.assertEqual(calls, [("", 0)])     # 键为空 → 限速函数立刻返回

    def test_throttle_waits_between_consecutive_calls(self):
        # 假时钟，**单位必须是秒**：time.monotonic() 返回秒，_throttle 内部自己乘 1000。
        # 假时钟若按毫秒累加就是双重放大（200 → 200 秒），等待次数会算错。
        # 每次 sleep 要把时钟推着走，否则冻结的时钟会让等待越算越长
        clock = [0.0]
        slept = []

        def fake_sleep(seconds):
            slept.append(seconds)
            clock[0] += seconds

        with patch.object(F.time, "sleep", side_effect=fake_sleep), \
             patch.object(F.time, "monotonic", side_effect=lambda: clock[0]):
            F._throttle("k", 200)      # 第一次：不等
            F._throttle("k", 200)      # 第二次：等满间隔
            F._throttle("k", 200)      # 第三次：同样
        self.assertEqual(len(slept), 2)
        for seconds in slept:
            self.assertAlmostEqual(seconds, 0.2, places=3)

    def test_throttle_is_per_key(self):
        slept = []
        with patch.object(F.time, "sleep", side_effect=lambda s: slept.append(s)), \
             patch.object(F.time, "monotonic", return_value=0.0):
            F._throttle("a", 5000)
            F._throttle("b", 5000)     # 另一个源，不该被 a 拖住
        self.assertEqual(slept, [])

    def test_throttle_is_a_noop_without_interval(self):
        slept = []
        with patch.object(F.time, "sleep", side_effect=lambda s: slept.append(s)):
            F._throttle("k", 0)
            F._throttle("", 5000)
        self.assertEqual(slept, [])


class PageCacheTests(CacheIsolatedTestCase):
    """页面缓存：命中/回源、键的身份、TTL、LRU、单页上限与三种策略。

    「改一次选择器试一次」的反馈环原来是重新联网（一条链 2.4 秒），缓存把它压到
    毫秒级——但**代价是「你看到的可能不是刚抓的」**，所以每条边界都要有用例守着：
    只缓存成功的、超大页不缓存、只读模式绝不偷偷联网。
    """

    def setUp(self):
        super().setUp()
        self.calls = []

    def _fake_urlopen(self, body=None):
        """假响应：正文里带上 URL、UA 与**第几次抓取**。

        带上序号是为了让「这份到底是缓存里的旧内容、还是刚抓的新内容」可直接
        断言——只按 URL 生成正文的话，两份内容一模一样，「重抓后有没有回填」
        根本区分不出来。
        """
        calls = self.calls

        class FakeResp:
            def read(self): return payload

            def __enter__(self): return self

            def __exit__(self, *exc): return False

        def fake_urlopen(req, timeout=None):
            calls.append(req.full_url)
            nonlocal payload
            payload = (body if body is not None else
                       ("BODY:%s|UA=%s|n=%d" % (req.full_url,
                                                req.headers.get("User-agent", ""),
                                                len(calls)))).encode("utf-8")
            return FakeResp()

        payload = b""
        return fake_urlopen

    def _fetch(self, url, **kw):
        with patch("urllib.request.urlopen", side_effect=self._fake_urlopen()):
            return F.fetch_ex(url, **kw)

    def test_second_call_is_served_from_cache(self):
        first = self._fetch("https://a.com/x")
        again = self._fetch("https://a.com/x")
        self.assertIs(first.cached, False)
        self.assertIs(again.cached, True)
        self.assertEqual(again.html, first.html)      # 内容必须是同一份
        self.assertEqual(len(self.calls), 1)          # 只发了一次请求
        self.assertTrue(again.fetched_at)             # 抓取时刻要带出来（抽屉要标）

    def test_key_includes_request_identity(self):
        """换 UA / 换 charset 就是另一次请求：抓到的不是同一份页面。

        只按 URL 做键会把两种身份的内容串在一起——带登录态与不带登录态的源、
        换过 UA 的源，缓存互相污染（同一类坑见 lessons §五）。
        """
        self._fetch("https://a.com/x")
        self._fetch("https://a.com/x", headers={"Referer": "https://b.com"})
        self.assertEqual(len(self.calls), 2)
        self._fetch("https://a.com/x", charset="gbk")
        self.assertEqual(len(self.calls), 3)

    def test_key_normalizes_url_encoding(self):
        """未编码与已编码的中文是**同一个请求**：不该各占一格。

        `?q=我` 这种原样 URL 是真实来源（连 App 调试时 App 给的就是这个形态），
        而 fetch 内部会先 quote 再发——键必须跟着用编码后的那份。
        """
        self._fetch("https://a.com/so?q=我")
        self._fetch("https://a.com/so?q=%E6%88%91")
        self.assertEqual(len(self.calls), 1)

    def test_key_includes_proxy_and_charset(self):
        """键直接断言：proxy / charset 进键，而 URL 只有编码差异时**不进两次**。"""
        base = ("https://a.com/x", {"User-Agent": "UA"}, "", "")
        self.assertEqual(F._cache_key(*base),
                         F._cache_key("https://a.com/x", {"user-agent": "UA"}, "", ""))
        self.assertNotEqual(F._cache_key(*base),
                            F._cache_key("https://a.com/x", {"User-Agent": "UA"}, "", "http://127.0.0.1:7890"))
        self.assertNotEqual(F._cache_key(*base),
                            F._cache_key("https://a.com/x", {"User-Agent": "UA"}, "gbk", ""))
        # 非字符串 charset 一律当「没传」——解码分支本来也只认字符串
        self.assertEqual(F._cache_key("https://a.com/x", {"User-Agent": "UA"}, 123, ""),
                         F._cache_key(*base))

    def test_expired_entry_is_refetched(self):
        clock = [1000.0]
        with patch.object(F.time, "time", side_effect=lambda: clock[0]):
            self._fetch("https://a.com/x")
            self.assertIs(self._fetch("https://a.com/x").cached, True)
            clock[0] += F.PAGE_CACHE_TTL + 1
            stale = self._fetch("https://a.com/x")
        self.assertIs(stale.cached, False)
        self.assertEqual(len(self.calls), 2)

    def test_oversized_page_is_not_cached(self):
        """超上限的页面**不缓存**，而不是截断后缓存。

        截断会让解析看到半页 HTML——同一个源两次试跑给出不同判定，而界面上没有
        任何东西说明为什么。宁可每次都重抓。
        """
        with patch.object(F, "PAGE_CACHE_MAX_BYTES", 8):
            self._fetch("https://a.com/x")
            second = self._fetch("https://a.com/x")
        self.assertIs(second.cached, False)
        self.assertEqual(len(self.calls), 2)

    def test_lru_evicts_least_recently_used(self):
        with patch.object(F, "PAGE_CACHE_MAX_PAGES", 2):
            self._fetch("https://a.com/1")
            self._fetch("https://a.com/2")
            self._fetch("https://a.com/3")
            self.assertEqual(F.page_cache_size(), 2)   # 上限真的生效
            self._fetch("https://a.com/1")             # 最久没用过的已被逐出
        self.assertEqual(len(self.calls), 4)

    def test_failed_fetch_is_not_cached(self):
        """只缓存抓成功的：失败页缓存下来会让「站点恢复了」看不见。"""
        def flaky(req, timeout=None):
            if len(self.calls) == 0:
                self.calls.append(req.full_url)
                # fp 给 BytesIO 而不是 None：None 会让 HTTPError 自己开临时文件，
                # 被 GC 时在用例输出里留下一串 ResourceWarning
                raise HTTPError(req.full_url, 403, "Forbidden", {}, io.BytesIO(b""))
            return self._fake_urlopen()(req, timeout)

        with patch("urllib.request.urlopen", side_effect=flaky):
            with self.assertRaises(HTTPError):
                F.fetch_ex("https://a.com/x")
            ok = F.fetch_ex("https://a.com/x")
        self.assertIs(ok.cached, False)
        self.assertEqual(len(self.calls), 2)

    def test_cache_only_never_touches_the_network(self):
        with patch("urllib.request.urlopen") as uo:
            with self.assertRaises(F.CacheMiss):
                F.fetch_ex("https://a.com/x", cache=F.CACHE_ONLY)
        self.assertFalse(uo.called, "只读缓存必须一个请求都不发")

    def test_cache_only_serves_cached_copy(self):
        self._fetch("https://a.com/x")
        with patch("urllib.request.urlopen") as uo:
            hit = F.fetch_ex("https://a.com/x", cache=F.CACHE_ONLY)
        self.assertIs(hit.cached, True)
        self.assertFalse(uo.called)

    def test_refresh_bypasses_and_refills(self):
        first = self._fetch("https://a.com/x")
        fresh = self._fetch("https://a.com/x", cache=F.CACHE_REFRESH)
        self.assertIs(fresh.cached, False)
        self.assertEqual(len(self.calls), 2)
        # 重抓的那份要**写回去**：否则「忽略缓存」变成「这一页从此不再缓存」。
        # 断言内容而不只是 cached 标志——旧条目还在缓存里时，标志看上去也是对的
        again = self._fetch("https://a.com/x")
        self.assertIs(again.cached, True)
        self.assertNotEqual(again.html, first.html)
        self.assertEqual(again.html, fresh.html)
        self.assertEqual(len(self.calls), 2)

    def test_unknown_mode_raises_instead_of_falling_back(self):
        """拼错/未知的策略必须显式报错：静默退回默认等于「用户以为不联网、其实在联网」。"""
        with patch("urllib.request.urlopen") as uo:
            with self.assertRaises(ValueError):
                F.fetch_ex("https://a.com/x", cache="cached")
        self.assertFalse(uo.called)

    def test_fetch_wrapper_still_returns_plain_html(self):
        """既有 13 个调用点读的是字符串——包装层不能把它们变成 Fetched。"""
        with patch("urllib.request.urlopen", side_effect=self._fake_urlopen()):
            html = F.fetch("https://a.com/x")
        self.assertIsInstance(html, str)
        self.assertIn("BODY:https://a.com/x", html)


# ---------------------------------------------------------------- 变异记录
# 以下为实测（改坏 → `python -B -m unittest tests.test_fetch` → 确认变红 → 还原）。
#
#  M1  fetch 里删掉 `_throttle(*_rate_key(source))`
#        → test_fetch_hands_the_source_to_the_throttle
#          test_fetch_without_source_still_works 红
#  M2  _rate_key 改用规范化 URL（.rstrip("/").lower()）
#        → test_rate_key_keeps_the_raw_url
#          test_fetch_hands_the_source_to_the_throttle 红
#  M3  _rate_key 不再读 concurrentRate（interval 恒 0）
#        → test_rate_key_keeps_the_raw_url
#          test_fetch_hands_the_source_to_the_throttle 红
#
#  页面缓存（PageCacheTests）：
#  M4  fetch_ex 里整段缓存查询删掉（不再命中）
#        → test_second_call_is_served_from_cache 红
#  M5  _cache_key 不再把请求头算进去
#        → test_key_includes_request_identity 红
#  M6  _cache_get 的 TTL 判断恒假（永不过期）
#        → test_expired_entry_is_refetched 红
#  M7  超上限页面改成「截断后缓存」
#        → test_oversized_page_is_not_cached 红
#  M8  CACHE_ONLY 的 miss 分支从 raise 改成 pass（退回联网）
#        → test_cache_only_never_touches_the_network 红
#  M9  CACHE_REFRESH 抓完直接返回、不回填
#        → test_refresh_bypasses_and_refills 红
#
#  接线（改的是别的模块，把 cache 那一层摘掉）：
#  M10 fetch_debug_pages 不把 cache 透传给 fetch_ex
#        → test_app_debug.test_cache_mode_reaches_fetch 红
#  M11 fetch_debug_pages 不再单独接 CacheMiss（并与抓取失败合流）
#        → test_app_debug.test_cache_miss_is_noted_as_such 红
#  M12 verify_chain 调 _new_page 时不传 fetched_at / cached
#        → test_verify_chain.test_pages_carry_the_html_source 红
#  M13 quality.new_page 不把这两个字段写进页面字典
#        → test_verify_chain.test_pages_carry_the_html_source 红
#
#  **没覆盖的**：
#    - ``_rate_lock`` 与 ``_page_cache_lock`` 的并发正确性：本文件的用例都是单线程的，
#      把锁去掉、或把 sleep 挪进锁里都不会变红——这类行为要写并发用例才能守，而那种
#      用例容易抖动。这里明确记下来，免得下次以为"有测试守着"。
#    - 「真去抓一次站点、确认缓存回的是同一份页面」：要联网，不适合放在回归测试里。
#      本地验的是**同一段逻辑**（命中即返回、不重发请求），真机验证得手工做。

if __name__ == "__main__":
    unittest.main()


class MethodBodyTests(CacheIsolatedTestCase):
    """method / body 是补抓（core/app_debug）带来的请求维度。

    没有它们，带 ``,{"method":"POST",...}`` 选项的链接会被当成 GET 发出去，
    抓回来的往往不是 App 看到的那份——「看源码改规则」就失去地基。
    """

    def _capture(self, **kw):
        """跑一次 fetch_ex，返回实际发出去的 Request 对象。"""
        sent = {}

        class FakeResp:
            def read(self): return b"x"
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_urlopen(req, timeout=None):
            sent["req"] = req
            return FakeResp()

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            F.fetch_ex("https://a.com/x", **kw)
        return sent["req"]

    def test_post_sends_body_and_method(self):
        req = self._capture(method="POST", body="id=1&p=2")
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.data, b"id=1&p=2")

    def test_get_drops_body(self):
        """GET 不带 body（与 App 一致：选项里的 body 仅 POST 生效）。"""
        req = self._capture(method="GET", body="id=1")
        self.assertEqual(req.get_method(), "GET")
        self.assertIsNone(req.data)

    def test_unknown_method_raises(self):
        """不认识的 method 显式报错——静默当 GET 发是「做了另一件事」。"""
        with self.assertRaises(ValueError):
            F.fetch_ex("https://a.com/x", method="PUT")

    def test_post_form_body_gets_default_content_type(self):
        """POST 且 body 非 JSON/XML、无显式 Content-Type → 补表单默认
        （对齐 AnalyzeUrl.kt:278）。"""
        req = self._capture(method="POST", body="id=1")
        self.assertEqual(req.headers["Content-type"],
                         "application/x-www-form-urlencoded")

    def test_post_json_body_keeps_content_type_unset(self):
        req = self._capture(method="POST", body='{"id":1}')
        self.assertNotIn("Content-type", req.headers)

    def test_post_explicit_content_type_wins(self):
        req = self._capture(method="POST", body="id=1",
                            headers={"Content-Type": "text/plain"})
        self.assertEqual(req.headers["Content-type"], "text/plain")

    def test_cache_key_distinguishes_method_and_body(self):
        """同 URL 的 GET/POST、不同 body 是两个请求身份——混用键会静默串页。"""
        h = {"User-Agent": "UA"}
        base = F._cache_key("https://a.com/x", h, "", "")
        self.assertNotEqual(base, F._cache_key("https://a.com/x", h, "", "", "POST", ""))
        self.assertNotEqual(base, F._cache_key("https://a.com/x", h, "", "", "POST", "id=1"))
        # method 大小写不改变身份（与 fetch_ex 里的 upper 归一口径一致）
        self.assertEqual(F._cache_key("https://a.com/x", h, "", "", "post", ""),
                         F._cache_key("https://a.com/x", h, "", "", "POST", ""))
