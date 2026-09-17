# -*- coding: utf-8 -*-
"""批量校验的判定收拢：缓存版本、三态落点与死分支删除。

本文件覆盖 Task 6 的三个动作：
  1. `_probe_content` / `_probe_toc` 的判定改调 `core.quality`（与试跑同口径）
  2. 删除 `ruleContent.image` 死分支（Legado 的 ContentRule 里没有这个字段）
  3. `CACHE_VERSION` 5 → 6（旧缓存的 toc_complete / content_ok 是旧逻辑算的）

**三态字段一律用 assertIs / assertIsNone 断言**：`assertFalse(record.content_ok)`
对 False 与 None 同时成立，被测分支生效与否都过——那正是本项目已产出 7 条的假测试形态。

本文件每条关键断言都做过变异验证（改坏被它守的那行 → 必须变红），变异表见文件末尾。
"""

import asyncio
from datetime import datetime, timedelta
import unittest
from unittest import mock

# 本文件用假的 _request 替掉网络层，不依赖真实客户端。
#
# 这里曾经用 `sys.modules.setdefault("aiohttp", types.ModuleType("aiohttp"))` 把
# aiohttp 换成空壳，理由是「隔离未安装的可选运行时依赖」。但 aiohttp 是
# pyproject.toml 里的**硬依赖**，那个理由不成立；而 `sys.modules` 是全局的，
# 全量 discover 时后面的测试文件也会拿到空壳——踩过两次：真的要跑
# `AsyncChecker.run()` / `_request()` 的用例单独跑没事、一起跑就报
# `module 'aiohttp' has no attribute 'ClientSession'`。已删。
from core import checker
from core.checker import (AsyncChecker, calc_stars, classify_http_status,
                          evaluate_stars, parse_search_request,
                          split_url_options)
from core.loader import fingerprint
from core.models import BookSourceRecord, Health, build_record


# ------------------------------------------------------------ 固定页面

SEARCH_PAGE = '<div class="item"><a href="https://site/book/1">测试书</a></div>'

TOC_PAGE = ('<div class="chapters">'
            '<a href="/read/1.html">第1章</a>'
            '<a href="/read/2.html">第2章</a>'
            '<a href="/read/3.html">第3章</a>'
            '</div>')

#: 只有 3 个中文字符的正文——旧实现要求 >100 字符，收拢后底线是「非空即通过」
SHORT_CONTENT_PAGE = '<div id="content">短正文</div>'

#: 带 4 张图的正文页——给「image 兜底」的变异验证用
IMAGES_PAGE = ('<div class="imgs">'
               '<img src="/a.jpg"><img src="/b.jpg">'
               '<img src="/c.jpg"><img src="/d.jpg">'
               '</div>')

#: 中位章节（3 条取 urls[1]）→ 这条 URL 必须被请求到
MEDIAN_CHAPTER_URL = "https://site/read/2.html"

TEST_TITLES = {"测试书": {"type": "novel", "chapters": 3}}


class _StubChecker(AsyncChecker):
    """把网络层换成固定页面表，专测「判定结论落在三态字段的哪一格」。"""

    def __init__(self, pages, **kw):
        kw.setdefault("use_store", False)
        super().__init__(**kw)
        self.pages = pages          # {url: 页面字符串}
        self.requested = []         # 实际请求过的 URL，用于钉住取哪一章

    async def _request(self, session, record, url, method="GET", headers=None,
                       allow_redirects=True, body=""):
        # 签名必须与 AsyncChecker._request 一致（record 是第二个位置参数）。
        # 漏改的话 url 会绑到 record 上——桩照样"能跑"，只是测的不是真东西。
        # `body` 是 2026-09-16 加的（书源可声明 `url,{"method":"POST","body":…}`）：
        # 漏了它 `_probe_search` 传关键字参数会直接 TypeError。
        # **返回值是五元组**（第五个是底层异常类名，2026-09-16 加的）：少一个会在
        # 调用点解包时报 ValueError，而报出来的是"验证异常：ValueError"——
        # 看着像被测的判定逻辑坏了，其实是桩没跟上
        self.requested.append(url)
        page = self.pages.get(url)
        if page is None:
            return 404, b"", 1.0, "", ""     # 未预置的 URL 一律 404
        return 200, page.encode("utf-8"), 1.0, "", ""


def make_raw(content_rule="id.content", toc_rule="class.chapters@tag.a"):
    """每次调用都新建 dict（含嵌套），避免用例之间互相污染。"""
    return {
        "bookSourceUrl": "https://site",
        "bookSourceName": "测试源",
        "bookSourceType": 0,
        "ruleSearch": {"bookList": "class.item", "bookUrl": "tag.a@href"},
        "ruleToc": {"chapterList": toc_rule, "chapterUrl": "tag.a@href"},
        "ruleContent": {"content": content_rule},
    }


def make_record(raw, source_type=0, search_hit="测试书"):
    return BookSourceRecord(index=0, url="https://site", name="测试源", raw=raw,
                            source_type=source_type, search_hit=search_hit)


def run_toc(rec, pages=None, test_titles=None):
    """跑一次 _probe_toc，返回 stub（便于断言请求过的 URL）。"""
    stub = _StubChecker(pages or {"https://site/book/1": TOC_PAGE},
                        test_titles=test_titles or TEST_TITLES)
    asyncio.run(stub._probe_toc(None, rec, "https://site", SEARCH_PAGE.encode("utf-8")))
    return stub


def run_content(rec, pages=None):
    """跑一次 _probe_content，toc_body 用固定目录页。"""
    stub = _StubChecker(pages or {MEDIAN_CHAPTER_URL: SHORT_CONTENT_PAGE})
    asyncio.run(stub._probe_content(None, rec, "https://site", TOC_PAGE.encode("utf-8")))
    return stub


# ------------------------------------------------------------ 缓存版本

class CacheVersionTests(unittest.TestCase):
    def test_version_bumped(self):
        """判定逻辑变了，缓存必须整体作废，否则改了等于没改。

        v10：探测深度合并成一根四档轴，缓存里那一列的**编号含义**随之平移
        （旧 1 档 = 域名+搜索 = 新 2 档）。旧行不会放出错误结论（只会更保守地
        重验），但「旧 1 档 + 搜索开」在新口径下必然判深度不够、等于整体重跑——
        把它标成版本变化，好过让用户以为"什么都没改，怎么又全量跑了"。
        v9：4xx 不再算「可达」——原来判 ok 的源现在判 dead（实测库里 91 条
        ok+4xx）。不作废的话那些源会在 14 天 TTL 内一直命中旧缓存里的 ok。
        v8：判定口径新增「验过搜索」这一维——旧条目没有 search_probed 字段，
        对开着搜索探测的用户必须整体重验，否则快速体检写下的结论会一直被复用。
        v7：反爬词表删掉裸 "cloudflare"——原来判 auth 的源现在判 ok，
        不作废的话那条 auth 会在 TTL 内一直命中缓存，看起来像修复失效。
        """
        self.assertEqual(checker.CACHE_VERSION, 11)

    def test_old_cache_item_rejected(self):
        raw = {"bookSourceUrl": "https://a.com", "bookSourceName": "x",
               "bookSourceType": 0}
        rec = BookSourceRecord(index=0, url="https://a.com", name="x", raw=raw)
        item = {
            "v": 5,                                     # 旧版本
            "fingerprint": fingerprint(raw),
            "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "health": Health.OK,
        }
        self.assertFalse(checker.is_cache_item_valid(rec, item))

    def test_current_cache_item_accepted(self):
        raw = {"bookSourceUrl": "https://a.com", "bookSourceName": "x",
               "bookSourceType": 0}
        rec = BookSourceRecord(index=0, url="https://a.com", name="x", raw=raw)
        item = {
            "v": checker.CACHE_VERSION,
            "fingerprint": fingerprint(raw),
            "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "health": Health.OK,
        }
        self.assertTrue(checker.is_cache_item_valid(rec, item))

    def test_expired_cache_item_rejected(self):
        raw = {"bookSourceUrl": "https://a.com", "bookSourceName": "x",
               "bookSourceType": 0}
        rec = BookSourceRecord(index=0, url="https://a.com", name="x", raw=raw)
        item = {
            "v": checker.CACHE_VERSION,
            "fingerprint": fingerprint(raw),
            "checked_at": (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S"),
            "health": Health.OK,
        }
        self.assertFalse(checker.is_cache_item_valid(rec, item))


class BuildRecordSearchUrlTests(unittest.TestCase):
    """`searchUrl` 的 `@` 后缀必须**先剥、再判 `has_search`**。

    顺序写反的后果（实测 **252 条**）：`@js:` 形态的 searchUrl 整段都落在 `@`
    之后，剥完是空串——而 `has_search` 是在剥之前算的，于是留下
    「有搜索规则、模板却是空」的**矛盾态**。checker 拿这个空模板去拼
    `domain + "/"`，**把站点首页当搜索页打**：白费一次请求，而且结论无从解释。

    修法只是把剥挪到判定之前——不用新增字段，也不是「特殊处理 @js:」。
    """

    def test_js_search_url_counts_as_no_search(self):
        """回放不了的搜索规则不能算「有搜索」。"""
        rec = build_record({"bookSourceUrl": "https://a.com",
                            "searchUrl": "@js:return 'https://a.com/s?q=' + key"}, 0)
        self.assertEqual(rec.search_url_template, "")
        self.assertFalse(rec.has_search,
                         "模板是空的却报 has_search=True → checker 会去打首页")

    def test_at_post_suffix_is_stripped_but_keeps_the_template(self):
        """反向断言：`@POST` 形态**要保住模板**——别把上一条的修法扩大化。"""
        rec = build_record({"bookSourceUrl": "https://a.com",
                            "searchUrl": "https://a.com/s?q={{key}}@POST"}, 0)
        self.assertEqual(rec.search_url_template, "https://a.com/s?q={{key}}")
        self.assertTrue(rec.has_search)

    def test_plain_search_url_is_untouched(self):
        """反向断言：不带 `@` 的普通 searchUrl（3800+ 条）一个字符都不能动。"""
        rec = build_record({"bookSourceUrl": "https://a.com",
                            "searchUrl": "https://a.com/s?q={{key}}"}, 0)
        self.assertTrue(rec.has_search)
        self.assertEqual(rec.search_url_template, "https://a.com/s?q={{key}}")


class UrlOptionTests(unittest.TestCase):
    """Legado 的 URL 选项语法 `url,{json}`——`searchUrl` 里 43% 的源在用。

    切法必须照抄 `AnalyzeUrl.kt:776`：

        val paramPattern: Regex = Regex("\s*,\s*(?=\{)")

    即**切第一个** `,{`（逗号前后可有空白），不是最后一个——URL 的查询串里
    完全可能出现 `,{`。切错了的后果不是「少传个参数」，是**整个请求变形**。

    **为什么要修**：选项没人解析时，整段 JSON 被留在 URL 里用 GET 发出去——
    请求是**畸形**的，而且与 App 的行为不一致（`AnalyzeUrl` 就是按这套语法发的）。
    实测选项分布：method 1167 / body 1141 / charset 627 / headers 53。

    ⚠️ **别把「相关」当「因果」——这是我在这条上犯过的错**：带该语法的源搜索
    命中率 **1.0%（17/1674）**、不带的是 **20.5%（446/2175）**，差 20 倍，看着
    像「就是它造成的」（我当时甚至写了「它解释了那 1612 条 3★ 仅规则」）。
    修完之后抽样做 A/B（25 个源，新旧行为对照）——只**救回 3 个**、还**弄坏 1 个**
    （净 +2）。那 20 倍的差距主要是**混淆**：用这套语法的源本身就偏 API 型/较新/
    已失效，跟语法无关。

    **结论**：修它是因为它畸形、且与 App 不一致，**不是因为它能救回 43%**。
    """

    def test_splits_url_and_options(self):
        url, opt = split_url_options('https://a.com/s,{"method":"POST"}')
        self.assertEqual(url, "https://a.com/s")
        self.assertEqual(opt, {"method": "POST"})

    def test_splits_at_the_first_comma_brace_not_the_last(self):
        """**切第一个**，不是最后一个。

        切最后一个的话 URL 里会残留一段 `,{...}` —— 请求就变形了。这里故意让
        「第一个」与「最后一个」不同，**只断言 URL 干净**（不要求选项可解析：
        切第一个之后剩下的那截本来就不是合法 JSON，Legado 也当没有选项）。
        """
        url, _opt = split_url_options('https://a.com/s?q=x,{"a":1},{"method":"POST"}')
        self.assertEqual(url, "https://a.com/s?q=x")

    def test_whitespace_around_the_comma_is_allowed(self):
        """`,` 与 `{` 之间可以有空白——实测语料里的 JSON 是多行缩进的。"""
        url, opt = split_url_options('https://a.com/s  ,\n  {"method": "POST"}')
        self.assertEqual(url, "https://a.com/s")
        self.assertEqual(opt["method"], "POST")

    def test_no_options_returns_the_whole_thing(self):
        self.assertEqual(split_url_options("https://a.com/s?q=1"),
                         ("https://a.com/s?q=1", {}))

    def test_broken_json_still_cuts_the_url(self):
        """JSON 解不出来时**照样切**、选项当空——这条是**对齐 App**，不是随手。

        Legado 先按 `paramPattern` 切出 `urlNoOption` 并拿去发请求，之后才去解析
        选项；解析失败只是不应用选项，**URL 已经被切了**（`AnalyzeUrl.kt:219-231`）。
        我们跟着切，才是同一个行为。
        """
        url, opt = split_url_options('https://a.com/s,{"body": "id"="x"}')
        self.assertEqual(url, "https://a.com/s")
        self.assertEqual(opt, {})


class SearchRequestOptionTests(unittest.TestCase):
    """选项怎么落到请求上——这一步决定搜索能不能真的跑起来。"""

    def test_method_and_body_come_from_options(self):
        """`method` / `body` 必须真的作用到请求上。

        库里 1167 条源声明了 POST、1141 条带 body。修之前它们全被当成 GET 发，
        **关键词根本没到服务端**。
        """
        url, method, headers, body = parse_search_request(
            'https://a.com/s.php,{"method":"POST","body":"q={{key}}"}', "测试")
        self.assertEqual(url, "https://a.com/s.php")
        self.assertEqual(method, "POST")
        self.assertEqual(body, "q=%E6%B5%8B%E8%AF%95")   # 关键词要 URL 编码
        self.assertEqual(headers, {})

    def test_headers_come_from_options(self):
        url, _m, headers, _b = parse_search_request(
            'https://a.com/s,{"headers":{"Referer":"https://a.com/"}}', "x")
        self.assertEqual(headers, {"Referer": "https://a.com/"})

    def test_the_json_never_stays_in_the_url(self):
        """**这条是本次修复的命门**：选项 JSON 绝不能留在 URL 里。

        留着的话请求 URL 变成 `.../s.php?q=测试,{"method":"POST"}` ——
        服务端认不出，且关键词是错的。
        """
        url, _m, _h, _b = parse_search_request(
            'https://a.com/s.php?q={{key}},{"method":"POST"}', "测试")
        self.assertNotIn("{", url)
        self.assertNotIn("method", url)

    def test_legacy_at_post_still_works(self):
        """反向断言：旧的 `@POST` 形态不能因为加了新语法就坏掉（库里还有 1 条在用）。"""
        url, method, _h, _b = parse_search_request("https://a.com/s?q={{key}}@POST", "x")
        self.assertEqual(url, "https://a.com/s?q=x")
        self.assertEqual(method, "POST")

    def test_plain_search_url_is_untouched(self):
        """反向断言：不带选项的普通 searchUrl（2175 条）必须一字不改。"""
        url, method, headers, body = parse_search_request("https://a.com/s?q={{key}}", "x")
        self.assertEqual(url, "https://a.com/s?q=x")
        self.assertEqual(method, "GET")
        self.assertEqual(headers, {})
        self.assertEqual(body, "")


class StarBasisTests(unittest.TestCase):
    """星级旁边要能说清「这一级是实测来的，还是按规则推的」。

    3★ 有**两种完全不同的来源**，界面上长得一模一样：

        (a) 搜索实测命中              → 实测
        (b) 没验过，只是静态规则齐全   → 仅规则

    用户看到「可用 3★」分不出这是验出来的还是看规则推的。海豚书屋（404 却报 3★）
    本质就撞在这一格上。

    **判据要跟 `calc_stars` 同源**：另写一份必然漂移，所以这里测的是同一个阶梯的
    两个出口——`evaluate_stars` 返回 `(星级, 来源)`，`calc_stars` 只是取星级。

    来源的判定规则只有一条：**这个星级所依赖的每一级，中间有没有哪一级是靠
    `_static_*` 回退通过的**。有 → `static`，没有 → `measured`。
    """

    #: 目录 + 正文规则都齐全（静态判据只要求非空）
    RAW_COMPLETE = {"ruleToc": {"chapterList": "class.c"},
                    "ruleContent": {"content": "class.content"}}
    RAW_EMPTY: dict = {}

    def _args(self, **over):
        base = dict(health=Health.OK, has_search=True, search_response_ms=120,
                    search_hit="", toc_complete=None, content_ok=None,
                    raw=self.RAW_COMPLETE)
        base.update(over)
        return base

    def test_three_stars_when_toc_is_unverifiable_and_rules_are_incomplete(self):
        """目录验不了，静态目录规则**也**不齐 → 停在 3★，来源仍是**实测**。

        这一级的依据是「搜索实测命中」（实测），静态判据虽然被问过但**没放行**——
        所以不能标成「仅规则」。这条守的是 `used_static` 只在**回退成功**时才置位：
        写成「只要回退过就算推的」的话，这里会被误标。
        """
        stars, basis = evaluate_stars(**self._args(search_hit="斗破苍穹",
                                                   toc_complete=None,
                                                   raw=self.RAW_EMPTY))
        self.assertEqual(stars, 3)
        self.assertEqual(basis, "measured")

    def test_three_stars_from_rules_alone_is_static(self):
        """**核心用例**：没命中、全靠静态规则齐全拿到的 3★，必须标成「仅规则」。"""
        stars, basis = evaluate_stars(**self._args(search_hit=""))
        self.assertEqual(stars, 3)
        self.assertEqual(basis, "static")

    def test_three_stars_from_a_real_failed_toc_is_measured(self):
        """反向断言：目录**真的验过**且结论是不完整 → 仍是实测，不是「没验」。"""
        stars, basis = evaluate_stars(**self._args(search_hit="斗破苍穹",
                                                   toc_complete=False))
        self.assertEqual(stars, 3)
        self.assertEqual(basis, "measured")

    def test_four_stars_with_a_measured_toc_is_measured(self):
        # 目录实测通过、正文实测不通过 → 4★，两级都有实测支撑
        stars, basis = evaluate_stars(**self._args(search_hit="斗破苍穹",
                                                   toc_complete=True,
                                                   content_ok=False))
        self.assertEqual(stars, 4)
        self.assertEqual(basis, "measured")

    def test_four_stars_with_an_unverified_toc_is_static(self):
        """4★ 也可能是推的：目录验不了（None）→ 回退静态目录规则。"""
        stars, basis = evaluate_stars(**self._args(search_hit="斗破苍穹",
                                                   toc_complete=None,
                                                   content_ok=False))
        self.assertEqual(stars, 4)
        self.assertEqual(basis, "static")

    def test_five_stars_fully_measured(self):
        stars, basis = evaluate_stars(**self._args(search_hit="斗破苍穹",
                                                   toc_complete=True,
                                                   content_ok=True))
        self.assertEqual(stars, 5)
        self.assertEqual(basis, "measured")

    def test_five_stars_with_both_depth_levels_unverified_is_static(self):
        """**这条最重要**：命中 + 规则齐全，但**目录和正文一次都没验**，照样 5★。

        现有的阶梯就是这样的——`calc_stars` 的注释写着「None=无法验证→回退静态」。
        5★ 的含义是「正文可用」，可这个 5★ 里没有一格正文是实测的。
        标成 `static` 是如实呈现；**要不要连星级本身也收紧，是另一个问题**
        （见 `TODO.md`：本项只做呈现，不改判定）。
        """
        stars, basis = evaluate_stars(**self._args(search_hit="斗破苍穹",
                                                   toc_complete=None,
                                                   content_ok=None))
        self.assertEqual(stars, 5)
        self.assertEqual(basis, "static")

    def test_unreachable_has_no_basis(self):
        """0★（不可达）没什么可标注的——空串，别硬凑一个词。"""
        stars, basis = evaluate_stars(**self._args(health=Health.DEAD))
        self.assertEqual(stars, 0)
        self.assertEqual(basis, "")

    def test_low_levels_are_measured(self):
        """1★（域名）/ 2★（搜索）都来自真实请求 → 实测。"""
        self.assertEqual(evaluate_stars(**self._args(search_response_ms=0))[1],
                         "measured")
        self.assertEqual(evaluate_stars(**self._args(has_search=False))[1],
                         "measured")

    def test_two_stars_when_nothing_is_measured_and_rules_are_incomplete(self):
        # 没命中、规则也不全 → 2★。这时静态判据被问过但没给分，不算「推的」
        stars, basis = evaluate_stars(**self._args(raw=self.RAW_EMPTY))
        self.assertEqual(stars, 2)
        self.assertEqual(basis, "measured")

    def test_calc_stars_still_returns_a_plain_int(self):
        """`calc_stars` 的签名与返回类型不能变——调用点不止一处。"""
        self.assertIsInstance(calc_stars(**self._args()), int)


class RestoreCacheTests(unittest.TestCase):
    def test_tri_state_survives_restore_without_error(self):
        """版本变了之后，新版本缓存里的三态必须原样恢复并重算星级。

        None（无法验证）回退静态规则，False（实测不达标）不回退——这是
        `_probe_*` 收拢后**新产生**的一批取值，恢复路径不能把它们丢了。
        """
        raw = make_raw()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        rec = make_record(raw)
        rec.has_search = True                       # 星级第 2 级的输入，不走缓存
        checker.restore_from_cache(rec, {
            "v": checker.CACHE_VERSION, "health": Health.OK, "checked_at": now,
            "search_response_ms": 120, "search_hit": "测试书", "probe_depth": 3,
            "toc_complete": None, "content_ok": None,
        })
        self.assertIsNone(rec.toc_complete)
        self.assertIsNone(rec.content_ok)
        # 两个维度都无法验证 → 回退静态规则（raw 里目录/正文规则齐全）→ 5★
        self.assertEqual(rec.quality_stars, 5)

        rec2 = make_record(raw)
        rec2.has_search = True
        checker.restore_from_cache(rec2, {
            "v": checker.CACHE_VERSION, "health": Health.OK, "checked_at": now,
            "search_response_ms": 120, "search_hit": "测试书", "probe_depth": 3,
            "toc_complete": True, "content_ok": False,
        })
        self.assertIs(rec2.toc_complete, True)
        self.assertIs(rec2.content_ok, False)
        # 实测 NotNone 优先，正文 False 不回退静态 → 卡在 4★
        self.assertEqual(rec2.quality_stars, 4)


class JudgeMappingTests(unittest.TestCase):
    """quality 的三态映射必须落到 checker 既有的 None/True/False 语义上。"""

    def test_mapping_is_stable(self):
        from core import quality as Q
        self.assertIs(Q.Judgement(Q.VERDICT_PASS).checker_state, True)
        self.assertIs(Q.Judgement(Q.VERDICT_FAIL).checker_state, False)
        self.assertIsNone(Q.Judgement(Q.VERDICT_UNKNOWN).checker_state)


# ------------------------------------------------------------ 目录判定

class TocProbeTests(unittest.TestCase):
    def test_toc_pass_reaches_ratio_check(self):
        """judge_list_step 必须收到 rule=chapter_list_rule。

        不传 rule 时它把「规则为空」当**源的配置错误**判 fail（源如果真缺规则，
        整批源会被误杀）——这条用例的目录能解析出 3 章，只有 rule 传对了才走得到
        后面的参考表比对。
        """
        rec = make_record(make_raw())
        run_toc(rec)
        self.assertEqual(rec.chapter_count, 3)
        self.assertIs(rec.toc_complete, True)       # 3 章 ≥ 参考 3×0.8=2
        self.assertEqual(rec.toc_fail_reason, "")

    def test_toc_empty_parse_is_false_not_none(self):
        """解析为空 = 源的规则跑不出东西 → False（不达标），不是 None（无法验证）。"""
        rec = make_record(make_raw(toc_rule="class.nothing@tag.a"))
        run_toc(rec)
        self.assertEqual(rec.chapter_count, 0)
        self.assertIs(rec.toc_complete, False)
        self.assertIn("解析结果为空", rec.toc_fail_reason)

    def test_toc_unreplayable_rule_is_unknown(self):
        """JS 规则是我们的能力边界 → None，且原因必须是「回放不了」而不是「解析为空」。"""
        rec = make_record(make_raw(toc_rule="class.chapters@tag.a@js:result"))
        run_toc(rec)
        self.assertIsNone(rec.toc_complete)
        self.assertTrue(rec.toc_fail_reason)
        self.assertNotIn("解析结果为空", rec.toc_fail_reason)

    def test_download_source_toc_is_unknown_not_pass(self):
        """bookSourceType==3（文件下载源）不解析目录（Debug.kt:329-332）。

        这条同时钉住 `judge_list_step` 的**步骤名**与 **source_type** 两个入参：
        两者任一处漏传，一个能解析出章节的下载源就会走到比例比对、被判成
        「目录完整」——而它根本不需要目录。
        """
        rec = make_record(make_raw(), source_type=3)
        run_toc(rec)
        self.assertIsNone(rec.toc_complete)     # 不是 True（走到比例比对了）
        self.assertIn("文件类书源", rec.toc_fail_reason)


# ------------------------------------------------------------ 正文判定

class ContentProbeTests(unittest.TestCase):
    def test_short_content_passes(self):
        """底线是「非空即通过」（Legado 只判 isBlank）——短正文不该被判坏。"""
        rec = make_record(make_raw())
        stub = run_content(rec)
        # 取中位章节（3 条取第 2 条），钉住抽样位置没被改坏
        self.assertEqual(stub.requested, [MEDIAN_CHAPTER_URL])
        self.assertIs(rec.content_ok, True)
        self.assertEqual(rec.content_fail_reason, "")

    def test_empty_rule_on_novel_source_fails(self):
        """正文规则为空：文本源（0）在 Legado 里会把章节链接当正文 → 不可读 → fail。"""
        rec = make_record(make_raw(content_rule=""), source_type=0)
        run_content(rec)
        self.assertIs(rec.content_ok, False)        # 不是 None
        self.assertIn("正文规则为空", rec.content_fail_reason)

    def test_empty_rule_on_image_source_passes(self):
        """空规则按类型分派：图片源（2）用章节链接本身是正常配置 → pass。"""
        rec = make_record(make_raw(content_rule=""), source_type=2)
        run_content(rec)
        self.assertIs(rec.content_ok, True)

    def test_unreplayable_rule_is_unknown(self):
        """@js: 正文规则回放不了 → None（我们的能力边界），不是 False（源坏了）。"""
        rec = make_record(make_raw(content_rule="id.content@js:result"))
        run_content(rec)
        self.assertIsNone(rec.content_ok)
        self.assertTrue(rec.content_fail_reason)
        self.assertNotIn("正文提取为空", rec.content_fail_reason)

    def test_dirty_source_type_does_not_break_the_verdict(self):
        """bookSourceType 来自外部 JSON，脏值不能让判定变成「验证异常」。

        core/sanitize.py 的 int 字段清单不含 bookSourceType，quality.safe_int 的
        docstring 自称是该字段的「唯一防线」——checker 这层必须走它，不能用裸 int()。
        """
        rec = make_record(make_raw(content_rule=""), source_type="abc")
        run_content(rec)
        self.assertIs(rec.content_ok, False)        # 降级为类型 0 判定，而不是 None
        self.assertNotIn("验证异常", rec.content_fail_reason)

    def test_legacy_image_rule_does_not_rescue(self):
        """删除 ruleContent.image 死分支的行为钉子。

        这个字段 Legado 的 ContentRule 里不存在（ContentRule.kt:12-25），本项目也没有
        生产者（analyzer.py 把图片规则写进 content），所以 image_rule 恒为空、兜底从未
        执行过。删掉之后：正文规则为空的文本源该 fail，哪怕页面里有一堆图。
        """
        raw = make_raw(content_rule="")
        raw["ruleContent"]["image"] = "class.imgs@tag.img@src"
        rec = make_record(raw, source_type=0)
        run_content(rec, pages={MEDIAN_CHAPTER_URL: IMAGES_PAGE})
        self.assertIs(rec.content_ok, False)


# ------------------------------------------------------------ 命中判定降级留痕

class HitDowngradeTests(unittest.TestCase):
    """``_confirm_hit`` 的异常分支必须留痕。

    该分支把「规则回放不了」当成「命中了」——方向是对的（保守不误杀），但它同时
    是 lessons §二 那次 bs4 缺失事故的入口：ModuleNotFoundError 被吞掉之后，命中
    判定退化成「响应体里含关键词就算命中」，而且**没有任何痕迹**。

    留痕不等于翻案：这里只验证「记下来了」，不改变它仍然返回 True。
    """

    def _record(self, rule="class.book"):
        return build_record({"bookSourceUrl": "https://a.example/",
                             "bookSourceName": "A",
                             "ruleSearch": {"bookList": rule}}, 0)

    def test_exception_is_recorded_but_still_counts_as_hit(self):
        ck = AsyncChecker(concurrency=1)
        with mock.patch.object(checker, "apply_css_rule",
                               side_effect=RuntimeError("模拟依赖缺失")):
            self.assertTrue(ck._confirm_hit(b"<html>x</html>", self._record()))
        self.assertEqual(len(ck.hit_downgrades), 1)
        self.assertIn("RuntimeError", ck.hit_downgrades[0])
        self.assertIn("https://a.example/", ck.hit_downgrades[0])

    def test_normal_rule_records_nothing(self):
        ck = AsyncChecker(concurrency=1)
        with mock.patch.object(checker, "apply_css_rule", return_value=["书名"]):
            self.assertTrue(ck._confirm_hit(b"<html>x</html>", self._record()))
        self.assertEqual(ck.hit_downgrades, [])

    def test_js_rule_is_a_known_tradeoff_not_a_downgrade(self):
        # JS 规则回放不了是**明知的能力边界**。每次都记会变成噪音，
        # 恰好淹没真正需要看见的那种异常（依赖缺失、解析器坏掉）
        ck = AsyncChecker(concurrency=1)
        # 两个断言缺一不可：只断言「没记降级」的话，把 JS 那条早退删掉也能过
        # （<js> 规则走 apply_css_rule 同样取不到值 → 同样不记降级），
        # 但那时行为已经变了：本该「保守算命中」的源会被判成未命中
        self.assertTrue(ck._confirm_hit(b"<html>x</html>",
                                        self._record(rule="<js>x</js>")))
        self.assertEqual(ck.hit_downgrades, [])


# ClassifyHttpStatusTests（2026-09-16 收拢「状态码 → 健康态」的判定表）：
#
#  M41  `_classify` 自己另写一份（不走 `classify_http_status`）
#         → test_classify_goes_through_the_shared_table 红
#  M43  `_probe_search` 自己另写一份（`s_status in (403,401,429)`）
#         → test_search_probe_auth_also_comes_from_the_shared_table 红
#         **这两条是接线断言，不是函数断言**：只测 `classify_http_status` 本身的话，
#         把任何一个调用点改回私有实现都照样全绿——而三处分叉正是这么来的。
#
# BuildRecordSearchUrlTests（2026-09-16 修「剥 @ 的时序」）：
#
#  M40  把剥 `@` 挪回 `has_search` 判定**之后**（= 还原原来的顺序）
#         → test_js_search_url_counts_as_no_search 红
#         （这就是那 252 条的全部成因：一行顺序。两条反向断言在 M40 下保持绿——
#           它们守的是「别把修法扩大化」，方向相反）
#
# 本组用例（UrlOptionTests / SearchRequestOptionTests，2026-09-16 修 URL 选项语法）：
#
#  M37  `split_url_options` 切**最后一个** `,{`（而不是第一个）
#         → test_splits_at_the_first_comma_brace_not_the_last 红
#  M38  不读 `method` / `body` 选项（解析了但不作用到请求上）
#         → test_method_and_body_come_from_options 红
#  M39  JSON 解析失败时**不切 URL**（返回原串）
#         → test_broken_json_still_cuts_the_url 红（连带第一条也红——同一处）
#
#  ⚠️ **M38 还原时又踩了「多处改动只还原一部分」**：那次变异一次动三行
#  （`m = …` / `if m in (…)` / `method = m`），我只还原了第一行，于是 `method`
#  永远停在 "GET"——全量跑出一条红，正是本文件上方与 lessons §十二 写下的那条。
#  判据不变：**变异之后先问「这真的是改动前的状态吗」，再去跑。**

# ------------------------------------------------------------------ 变异表
#
# 每条变异都真实执行过：改坏 core/checker.py 的一处 → 跑全量
# `unittest discover -s tests -t .` → 记录变红的用例 → 从备份精确还原
# （还原后 md5 与基线一致，209 条全绿）。
#
#  M1  CACHE_VERSION 6 → 5                                  | test_version_bumped
#                                                            | test_old_cache_item_rejected
#  M2  _probe_toc 不再传 rule=chapter_list_rule             | test_toc_pass_reaches_ratio_check
#      （退回默认空规则）                                    | test_toc_empty_parse_is_false_not_none
#                                                            | test_toc_unreplayable_rule_is_unknown
#  M3  _probe_toc 的 toc_verdict.checker_state → 恒 None    | test_toc_empty_parse_is_false_not_none
#      （即回到旧实现）                                      |
#  M4  _probe_toc 不再把 rule_error 传给 judge_list_step    | test_toc_unreplayable_rule_is_unknown
#  M5  _probe_content 判定整块改回旧实现                    | test_short_content_passes
#      （>100 阈值、不调 quality、无 image 分支）            | test_empty_rule_on_image_source_passes
#                                                            | test_empty_rule_on_novel_source_fails
#                                                            | test_unreplayable_rule_is_unknown
#  M6  恢复 ruleContent.image 兜底分支（死分支复活）        | test_legacy_image_rule_does_not_rescue
#  M7  record.content_ok = verdict.ok（unknown 当 pass）    | test_unreplayable_rule_is_unknown
#  M8  Q.safe_int → 裸 int(record.source_type or 0)         | test_dirty_source_type_does_not_break_the_verdict
#      （3 处全改）                                          |
#  M9  空规则分支假装有规则（judge_content(..., [], "text"))| test_empty_rule_on_novel_source_fails
#                                                            | test_empty_rule_on_image_source_passes
#  M10 judge_list_step 的步骤名 "toc" → "search"            | test_download_source_toc_is_unknown_not_pass
#  M11 judge_list_step 的 source_type 写死 0                | test_download_source_toc_is_unknown_not_pass
#  M12 两处，都守 _confirm_hit 的降级留痕：
#      a) 删掉 hit_downgrades.append（还原成静默吞异常）     | test_exception_is_recorded_but_still_counts_as_hit
#      b) 去掉 `<js` 那条早退（JS 规则也走回放）             | test_js_rule_is_a_known_tradeoff_not_a_downgrade
#
#  M12b **第一次跑是绿的**：当时那条用例只断言「没记降级」，而删掉早退后
#  `<js>` 规则走 apply_css_rule 同样取不到值、同样不记降级——两个实现都能过，
#  但行为已经变了（本该「保守算命中」的源会被判成未命中）。补上
#  `assertTrue(...)` 守返回值之后才变红。**只断言副作用、不断言返回值的用例，
#  守不住分支被删。**
#
# 前向钉子（改坏之前就已经通过，不区分新旧实现，只区分上面那批变异）：
#   test_toc_pass_reaches_ratio_check（旧实现同样 True，它守的是 M2）
#   test_toc_unreplayable_rule_is_unknown（旧实现同样 None，它守的是 M4）
#   test_dirty_source_type_does_not_break_the_verdict（旧实现压根不用 safe_int，
#     但布尔值相同——它守的是以后有人把 M8 那种写法引进来）
#   test_tri_state_survives_restore_without_error / test_mapping_is_stable
#
# 记两条容易看错的地方：
#  - test_empty_rule_on_novel_source_fails 在旧实现下**布尔值也是 False**，
#    真正把它顶红的是理由文案（旧：「无正文规则且图片不足」/ 新：「正文规则为空…」）。
#    M5 变红靠的正是这条 assertIn，不是那句 assertIs。
#  - M2 会一次顶红三条：rule 不传时 judge_list_step 先命中「规则为空」，
#    于是「解析为空」和「回放不了」两种原因同时被顶掉。
#
# 已知覆盖缺口（写出来，免得后来人当成漏测）：
#  - `extract_all_nodes` 的 limit / max_chars **写反顺序没有被任何用例抓住**：
#    hits 只进 evidence 与 notes，而 checker 这层在 pass 时不落任何附注、
#    在 fail 时只落 reason（reason 非空时 notes 也不进）。也就是说这两个参数
#    传错在本模块里**没有可观测出口**，只能靠「故意不设默认值」那层保护。
#  - 同理，收拢之后「正文较短 / 疑似错误页」这类附注在 pass 时被整体丢弃
#    （试跑那边会经 as_step_dict 落进 steps[].notes）。本 Task 没有为 checker
#    新开字段承载它们——计划未要求，记在这里。
#  - `_probe_toc` 里 bookUrl 规则仍走 apply_css_rule 老路（未收拢到 quality），
#    计划只要求收拢 chapterList 的判定，这一条保持原样。

# ------------------------------------------------------------ 域名 URL 构造

class BuildDomainUrlTests(unittest.TestCase):
    """书源 URL 常带 `#署名`，构造探测用的根 URL 时必须剥掉。

    下面这些值取自真实库（40.9% 的源带这种后缀），不是编的。
    """

    def test_signature_without_slash_is_stripped(self) -> None:
        """`#` 前没有 `/` 的形式——正则 [^/]+ 原本会把它一起吞进"域名"。"""
        self.assertEqual(checker.build_domain_url("https://m.qidian.com##时间排序发现规则"),
                         "https://m.qidian.com")

    def test_signature_with_slash_is_stripped(self) -> None:
        self.assertEqual(checker.build_domain_url("https://www.htmanga9.top/##旅途"),
                         "https://www.htmanga9.top")

    def test_signature_with_emoji_and_space_is_stripped(self) -> None:
        self.assertEqual(checker.build_domain_url("http://www.yuedsk.com###ʕ ᵔᴥᵔ ʔ喜静"),
                         "http://www.yuedsk.com")

    def test_plain_fragment_is_stripped(self) -> None:
        self.assertEqual(checker.build_domain_url("https://www.blquge99.cc/#pb1101"),
                         "https://www.blquge99.cc")

    def test_path_is_still_dropped(self) -> None:
        self.assertEqual(checker.build_domain_url("https://a.com/search/x#frag"),
                         "https://a.com")

    def test_bare_trailing_hash(self) -> None:
        self.assertEqual(checker.build_domain_url("http://www.wtzw.com#"),
                         "http://www.wtzw.com")

    def test_missing_scheme_gets_http(self) -> None:
        self.assertEqual(checker.build_domain_url("book.sfacg.com"), "http://book.sfacg.com")

    def test_empty_and_blank(self) -> None:
        self.assertEqual(checker.build_domain_url(""), "")
        self.assertEqual(checker.build_domain_url("   "), "")
        self.assertEqual(checker.build_domain_url(None), "")

    def test_normalize_url_keeps_the_signature(self) -> None:
        """跟上面正好相反：**源身份 key 必须保留署名**。

        署名不同即两个源，与 Legado 的 getSourceKey() 一致。两处用途不同，
        别为了"统一"把它们改成一样。
        """
        from core.loader import _normalize_url
        self.assertEqual(_normalize_url("https://a.com##签名"),
                         "https://a.com##签名")
        self.assertNotEqual(_normalize_url("https://a.com##甲"),
                            _normalize_url("https://a.com##乙"))


# ------------------------------------------------------------ 反爬特征词

#: Cloudflare 的邮箱保护脚本。站点把 Cloudflare 当 CDN 就会被注入到**每一个**
#: 正常页面里，与反爬无关——但裸词 "cloudflare" 匹配的正是它。
CF_EMAIL_DECODE = ('<html><body><div class="item">'
                   '<a href="https://site/book/1">测试书</a></div>'
                   '<script data-cfasync="false" '
                   'src="/cdn-cgi/scripts/5c5dd728/cloudflare-static/email-decode.min.js">'
                   '</script></body></html>')

#: 真正的 Cloudflare 挑战页（__cf_chl_* 是挑战流程的 token）
CF_CHALLENGE = ('<html><body><form id="challenge-form" '
                'action="/cdn-cgi/l/chk_jschl?__cf_chl_tk=abc"></form></body></html>')


class ClassifyHttpStatusTests(unittest.TestCase):
    """状态码 + 响应体 → 健康态。**全仓库唯一的判定表**。

    原来这三份各写一遍（`_classify` 域名探测 / `_probe_search` 搜索探测 /
    `reclassify.diagnose_source` 失效归因），实测已在 5 种输入上分叉：

        503（无响应体）        域名探测=dead   归因=需验证
        404 / 500 / 406        域名探测=dead   归因=继续判
        200 + 登录页 + cookie  域名探测=auth   归因=继续判

    所以这里**逐格**钉住——任何一格改动都会同时影响三个调用点。

    **注意「判定」与「后果」的边界**：本函数只回答「这个响应算什么」；
    各调用点自己决定怎么用。最典型的是搜索入口 404：那不能推出「源死了」
    （可能只是搜索规则过期），所以 `_probe_search` **不**套用这里的 4xx → DEAD。
    """

    def _c(self, status, body="", cookie_jar=False):
        return classify_http_status(status, body, cookie_jar)

    def test_no_status_is_dead(self):
        self.assertEqual(self._c(None), Health.DEAD)

    def test_auth_statuses(self):
        """401 / 403 / 429 一律 AUTH——它们本身就是「要登录 / 被拒」。"""
        for s in (401, 403, 429):
            with self.subTest(status=s):
                self.assertEqual(self._c(s), Health.AUTH)

    def test_503_with_anti_bot_is_auth_otherwise_dead(self):
        """503 要看响应体：带反爬特征才算「需验证」，否则是服务端挂了。"""
        self.assertEqual(self._c(503, "人机验证"), Health.AUTH)
        self.assertEqual(self._c(503, "Service Unavailable"), Health.DEAD)

    def test_server_errors_are_dead(self):
        for s in (500, 502, 504):
            with self.subTest(status=s):
                self.assertEqual(self._c(s), Health.DEAD)

    def test_200_with_anti_bot_markers_is_auth(self):
        self.assertEqual(self._c(200, "<p>请完成验证码</p>"), Health.AUTH)

    def test_200_login_wall_needs_the_source_to_declare_cookie_jar(self):
        """登录墙要看源有没有声明 cookie jar——**没声明就不能断言「需验证」**。

        「请登录」这两个字在正常页面的导航栏里太常见了。
        """
        self.assertEqual(self._c(200, "请登录后使用", True), Health.AUTH)
        self.assertEqual(self._c(200, "请登录后使用", False), Health.OK)

    def test_clean_200_is_ok(self):
        self.assertEqual(self._c(200, "<html>正常页面</html>"), Health.OK)

    def test_2xx_and_3xx_are_ok(self):
        """反向断言：204 / 301 / 302 是**大量正常源**的形态（不带尾斜杠的域名常回 301）。"""
        for s in (204, 301, 302):
            with self.subTest(status=s):
                self.assertEqual(self._c(s), Health.OK)

    def test_search_probe_auth_also_comes_from_the_shared_table(self) -> None:
        """`_probe_search` 的 AUTH 判定同样必须走那张共享表。

        **测接线**：它原来自己写了一份（`s_status in (403, 401, 429)`，外加 200 时的
        反爬词判断），与域名探测在「200 + 登录页 + 声明了 cookie jar」这类输入上
        分叉过——搜索响应是登录页时，域名探测判 AUTH、它却当没事发生继续找关键词。

        判法同 `_classify`：把共享表打桩成恒返回 AUTH，看它认不认。
        """
        raw = make_raw()
        raw["searchUrl"] = "https://site/search?q={{key}}"
        rec = build_record(raw, 0)
        search_url = checker.parse_search_request(rec.search_url_template, "测试书")[0]
        stub = _StubChecker(
            {search_url: '<div class="item"><a href="/b/1">测试书</a></div>'},
            testset={"novel": ["测试书"]})
        rec.health = Health.OK
        with mock.patch.object(checker, "classify_http_status",
                               return_value=Health.AUTH):
            body = asyncio.run(stub._probe_search(None, rec, "https://site"))
        self.assertIsNone(body, "判成 AUTH 就该停下，不再找下一个关键词")
        self.assertEqual(rec.health, Health.AUTH)

    def test_classify_goes_through_the_shared_table(self):
        """**测接线**：`_classify` 必须真的走那张共享表。

        只测 `classify_http_status` 本身的话，把 `_classify` 改回一份私有实现
        照样全绿——**而三处分叉正是这么来的**（这个仓库见过这个形状：
        「测了函数、没测接线」）。
        判法：把共享表打桩成恒返回 AUTH，看 `_classify` 会不会跟着变。
        """
        ck = _StubChecker({})
        with mock.patch.object(checker, "classify_http_status",
                               return_value=Health.AUTH):
            self.assertEqual(ck._classify(200, b"", make_record(make_raw()))[0],
                             Health.AUTH)

    def test_other_4xx_are_dead(self):
        """400 / 404 / 410 / 451：服务端明确说「这个入口拿不到东西」——不是可达。"""
        for s in (400, 404, 410, 451):
            with self.subTest(status=s):
                self.assertEqual(self._c(s), Health.DEAD)


class HttpStatusClassifyTests(unittest.TestCase):
    """HTTP 状态码 → 健康度。**注释与代码必须一致**。

    `_classify` 的最后一行原来是：

        return Health.OK  # 其他 2xx/3xx 视为可达

    ——注释写「2xx/3xx」，代码却没有范围判断。**404 一路落到这里被判成「可用」**，
    400 / 410 / 451 同样。

    实测影响：3861 条源里 91 条是「health=ok 且 4xx」（其中 404 有 78 条）。
    用户看到的是「站点明明打不开了，还报可用 + 3 星」。

    真实案例：`m.haitunsw.com`（海豚书屋）status=404、搜索无命中，却报 ok / 3★——
    3★ 是「未命中源封顶」那档，本身是有意设计（未命中≠源差），但它建在
    「404 算可达」这个错误地基上。
    """

    def _classify(self, status, body=""):
        ck = _StubChecker({})
        return ck._classify(status, body.encode("utf-8"), make_record(make_raw()))[0]

    def test_not_found_is_not_available(self):
        self.assertNotEqual(self._classify(404), Health.OK)

    def test_other_client_errors_are_not_available(self):
        # 400/410/451 都表示「这个入口拿不到东西了」，不是「可达」
        for s in (400, 410, 451):
            with self.subTest(status=s):
                self.assertNotEqual(self._classify(s), Health.OK)

    def test_redirects_and_no_content_are_still_available(self):
        """反向断言：2xx/3xx 仍然可达。

        少了这条，把范围判断写成 `status == 200` 也会全绿——而 204/301/302
        是**大量正常源**的形态（不带尾斜杠的域名常回 301）。
        """
        for s in (200, 204, 301, 302):
            with self.subTest(status=s):
                self.assertEqual(self._classify(s), Health.OK)

    def test_auth_and_server_errors_keep_their_own_buckets(self):
        """反向断言：401/403/429 仍归 AUTH、5xx 仍归 DEAD，没被新分支吞掉。"""
        for s in (401, 403, 429):
            with self.subTest(status=s):
                self.assertEqual(self._classify(s), Health.AUTH)
        for s in (500, 502, 503):
            with self.subTest(status=s):
                self.assertEqual(self._classify(s), Health.DEAD)

    def test_not_found_records_an_http_reason_not_a_connection_failure(self):
        """判死的原因要写「HTTP 404」，不能写成「连接失败/超时/DNS错误」。

        传输是通的——服务端明确回了 404。报成连接失败会把人引向错误的排查方向，
        而这正是 lessons §二 说的「异常吞掉之后必须留下痕迹」的同一类问题：
        结论对了、理由错了，同样没用。
        """
        ck = _StubChecker({})           # 未预置的 URL 一律 404
        ck._sem = asyncio.Semaphore(1)  # 平时由 run() 建，这里直接调 check_one
        rec = make_record(make_raw(), source_type=0)
        rec.search_hit = ""
        asyncio.run(ck.check_one(None, rec))
        self.assertEqual(rec.health, Health.DEAD)
        self.assertIn("404", rec.error or "",
                      "原因要指向 HTTP 状态：%r" % rec.error)


class AntiBotMarkerTests(unittest.TestCase):
    """一个裸词怎么把一个 5★ 正常源判成 auth——实测复盘，见 models.py 的注释。"""

    def _classify(self, status, body):
        ck = _StubChecker({})
        return ck._classify(status, body.encode("utf-8"), make_record(make_raw()))[0]

    def test_cf_email_decode_script_is_not_anti_bot(self) -> None:
        """Cloudflare 的邮箱保护脚本出现在正常页面里，不能被当成反爬。

        真实后果：m.manhuahao.com 因此被判 auth、星级从 5★ 压到 3★。
        """
        self.assertEqual(self._classify(200, CF_EMAIL_DECODE), Health.OK)

    def test_real_cf_challenge_is_still_anti_bot(self) -> None:
        """收紧不能收过头：真正的挑战页仍须判为反爬。"""
        self.assertEqual(self._classify(200, CF_CHALLENGE), Health.AUTH)

    def test_cf_email_decode_does_not_block_search_hit_judgement(self) -> None:
        """搜索响应带这个脚本时，必须继续走 bookList 命中判定。

        原先 _probe_search 在反爬分支**直接 return**，连命中判定都不做——
        所以 search_hit 为空并不是「搜索失败」，而是根本没测。
        """
        raw = make_raw()
        raw["searchUrl"] = "https://site/search?q={{key}}"
        # 必须经 build_record：search_url_template 是它填的，直接构造
        # BookSourceRecord 会得到空模板，拼出的 URL 对不上预置的页面
        rec = build_record(raw, 0)
        # 关键词会被 parse_search_request **URL 编码**，桩的 key 得用同一个函数算，
        # 手写 /search?q=测试书 是匹配不上的
        search_url = checker.parse_search_request(rec.search_url_template, "测试书")[0]
        stub = _StubChecker({search_url: CF_EMAIL_DECODE},
                            testset={"novel": ["测试书"]})
        # _probe_search 只会把健康度**改差**（失败时降级），置 OK 是 check_one
        # 里域名探测的职责——所以按真实调用顺序先给 OK，再断言它没被降级
        rec.health = Health.OK
        body = asyncio.run(stub._probe_search(None, rec, "https://site"))
        self.assertIsNotNone(body, "搜索应命中并返回响应体")
        self.assertEqual(rec.search_hit, "测试书")
        self.assertNotEqual(rec.health, Health.AUTH,
                            "邮箱保护脚本不该触发反爬降级")


class LoginWallTests(unittest.TestCase):
    """登录墙判据：**页面里有登录入口 ≠ 要登录**。

    原来词表里有裸的 `login` / `sign in`，它们匹配的是登录**入口**。实测
    `m.cread.com`（中文书城）的 33KB 首页零反爬词，唯一命中的是
    `<a href="/user/login.aspx">` —— 配上有 cookieJar 的源，**797 条**被判「需验证」，
    而它们在校验里本该是 ok。这与 v7 删掉裸 `cloudflare` 是同一类错。
    """

    def _c(self, body, cookie_jar=True):
        from core.checker import classify_http_status
        return classify_http_status(200, body, cookie_jar)

    def test_login_link_is_not_a_login_wall(self):
        self.assertEqual(self._c('<a href="/user/login.aspx">登录</a>'), Health.OK)
        self.assertEqual(self._c("<p>sign in</p>"), Health.OK)
        self.assertEqual(self._c('<a href="/login">Login</a>'), Health.OK)

    def test_real_wall_phrases_still_trigger(self):
        """收紧不能收过头：真正的登录墙仍要判出来（中文与英文短语）。"""
        self.assertEqual(self._c("<p>请登录后使用</p>"), Health.AUTH)
        self.assertEqual(self._c("<p>sign in to continue</p>"), Health.AUTH)
        self.assertEqual(self._c("<p>Login required</p>"), Health.AUTH)

    def test_auth_verdict_records_the_matched_word(self):
        """判「需验证」时必须留下**命中了哪个词**。

        实测教训：797 条源被判「需验证」而用户完全不知道为什么（那时依据没留痕），
        只能怀疑程序。依据进 `record.error`，界面上就看得见。
        """
        ck = _StubChecker({})
        rec = make_record(make_raw())
        health, why = ck._classify(200, "<p>请完成验证码</p>".encode("utf-8"), rec)
        self.assertEqual(health, Health.AUTH)
        self.assertIn("验证码", why)

        # 登录墙判据要求源声明了 cookieJar（没声明时「请登录」在导航栏里太常见）。
        # **这里必须走 `build_record`**：本文件那个 `make_record` 是直接构造
        # `BookSourceRecord`，而 `enabled_cookie_jar` 是 build_record 从 raw 里填的。
        from core.models import build_record

        raw = make_raw()
        raw["enabledCookieJar"] = True
        health, why = ck._classify(200, "<p>请登录后使用</p>".encode("utf-8"),
                                   build_record(raw, 0))
        self.assertEqual(health, Health.AUTH)
        self.assertIn("请登录", why)

    def test_non_auth_verdict_has_no_reason(self):
        ck = _StubChecker({})
        health, why = ck._classify(200, "<html>正常页</html>".encode("utf-8"),
                                   make_record(make_raw()))
        self.assertEqual(health, Health.OK)
        self.assertEqual(why, "")


if __name__ == "__main__":
    unittest.main()

# ---------------------------------------------------------------- 本类变异记录
# 实测（改坏 → `python -B -m unittest tests.test_checker_judge.AntiBotMarkerTests`
#       → 确认变红 → 还原）：
#
#  M1  ANTI_BOT_MARKERS 里加回裸 "cloudflare"
#        → test_cf_email_decode_script_is_not_anti_bot 红
#        → test_cf_email_decode_does_not_block_search_hit_judgement 红
#  M2  去掉 cf-challenge / __cf_chl（收紧过头）
#        → test_real_cf_challenge_is_still_anti_bot 红
#
# BuildDomainUrlTests 的变异：
#  M3  去掉 build_domain_url 里的 `url.split("#", 1)[0]`
#        → test_signature_without_slash_is_stripped 红
#        → test_signature_with_emoji_and_space_is_stripped 红
#        → test_bare_trailing_hash 红
#      **test_signature_with_slash_is_stripped 实测不会红**——`#` 前有 `/` 时
#      正则本来就截对了。这正是这个坑容易漏的原因：只在部分数据上显形。
#      （上面「实测」是按 M3 跑出来的 FAIL 清单，不是推的。）
#
# **core/quality.py 的 CONTENT_NOISE_MARKERS 里同样有裸 "cloudflare"，但没动**：
# 那一处作用在**提取出来的正文值**上（`_noise_hit(joined)`），而 Cloudflare 的
# 邮箱脚本在页面 head 里，根本不会进正文值——机制不成立。而且它只写 notes、
# 不改 verdict（见 `_content_notes` 的 docstring）。同形缺陷不等于已证实的缺陷，
# 没有证据就不改。哪天有真实误判再来。
#
# HttpStatusClassifyTests 的变异（2026-09-16，修「404 算可达」）：
#  M14  把末尾改回 `return Health.OK`（= 原始 bug：注释说 2xx/3xx，代码没判范围）
#         → test_not_found_is_not_available
#           test_other_client_errors_are_not_available(400/410/451)
#           test_not_found_records_an_http_reason_not_a_connection_failure 红
#  M15  范围写成 `200 <= status < 300`（把 3xx 也判死）
#         → test_redirects_and_no_content_are_still_available(301/302) 红
#         （M14 与 M15 是一对：只做 M14 会让人以为「随便判个死就行」，
#           M15 证明 3xx 必须继续算可达——不带尾斜杠的域名常回 301）
#  M16  去掉 `elif status:` 那条原因分支（判死的原因退回「连接失败/超时/DNS错误」）
#         → test_not_found_records_an_http_reason_not_a_connection_failure 红
#         （结论不变、只有理由变——**没有这条断言就完全看不出来**）
#
# StarBasisTests 的变异（2026-09-16，「实测 / 仅规则」这一维）：
#
#  M20  `_basis()` 恒返 "measured"
#         → test_five_stars_with_both_depth_levels_unverified_is_static
#           test_four_stars_with_an_unverified_toc_is_static 红
#         （`test_three_stars_from_rules_alone_is_static` **不红**——那条走的是
#           3★ 处的另一个分支，直接硬编码返回 "static"，不经过 `_basis`。
#           这意味着「static」有两处来源，两处都得有断言，别只看 `_basis`）
#  M21  回退过就算「推的」（`used_static = True` 提到判断静态回退**成功**之前）
#  M22  LOGIN_MARKERS 加回裸 "login"
#        → LoginWallTests.test_login_link_is_not_a_login_wall 红
#          （页头一个 /user/login.aspx 链接就把 797 条判成「需验证」）
#  M23  `_classify` 不再返回判定依据（reason 恒空串）
#        → LoginWallTests.test_auth_verdict_records_the_matched_word 红
#         → test_three_stars_when_toc_is_unverifiable_and_rules_are_incomplete 红
#         ⚠️ **这条变异还原的就是实现时的第一版写法**。当时是先被
#         `test_three_stars_from_a_real_hit_is_measured` 的 `5 != 3` 引过去的——
#         但那次失败其实是**我的测试预期写错了**（`toc_complete=None` 会回退静态，
#         规则齐全就是 5★）。顺着看代码才发现实现另有一处不对：`toc_complete`
#         是 None 就置位，可静态回退**没放行**时这一级根本没靠它通过。
#         两件事被我一开始混成一件——所以这条断言单独存在，且措辞写「回退**成功**」。

