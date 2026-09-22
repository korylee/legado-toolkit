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
from core.checker import (calc_stars, classify_http_status, evaluate_stars,
                          parse_search_request, split_url_options)
from core.loader import fingerprint
from core.models import BookSourceRecord, Engine, Health, build_record


# ------------------------------------------------------------ 固定页面

SEARCH_PAGE = '<div class="item"><a href="https://site/book/1">测试书</a></div>'

#: 目录页。**列表项与字段要分层**（`li` 里放 `a`）：App 的求值语义是
#: 「先 getElements(chapterList) 取节点，再在**每个节点内**求 chapterUrl」
#: （`BookChapterList.kt` 的 elements.forEachIndexed { setContent(item) … }）。
#: 旧 fixture 写成 `<a>` 直接挂在 `.chapters` 下、chapterList 又选到 `<a>`——
#: 那种形状下 App 也取不到 url（在 a 里找 a），只有「对整页求值」的旧本地实现
#: 才能跑通。fixture 按能跑通的那套语义写，测出来的结论就是假的。
TOC_PAGE = ('<div class="chapters">'
            '<li><a href="/read/1.html">第1章</a></li>'
            '<li><a href="/read/2.html">第2章</a></li>'
            '<li><a href="/read/3.html">第3章</a></li>'
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

#: 「目录在独立页上」的形态（实测库里 1617 条）：详情页里没有章节列表，
#: 目录页地址写在 `ruleBookInfo.tocUrl` 规则里，要在详情页上求值取出来。
DETAIL_PAGE_NO_TOC = ('<div class="info"><a class="toc-link" href="/book/1/toc/">'
                      '查看目录</a></div>')
SEPARATE_TOC_PAGE = ('<div class="chapters">'
                     '<li><a href="1.html">第1章</a></li>'
                     '<li><a href="2.html">第2章</a></li>'
                     '<li><a href="3.html">第3章</a></li>'
                     '</div>')
SEPARATE_TOC_URL = "https://site/book/1/toc/"

TEST_TITLES = {"测试书": {"type": "novel", "chapters": 3}}




def make_raw(content_rule="id.content", toc_rule="class.chapters@tag.li"):
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
        标成 `static` 是如实呈现；**要不要连星级本身也收紧**已于 2026-09-17
        定案为「不收紧」（理由见 `skills/legado-source-lessons` §二十六）。
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




class JudgeMappingTests(unittest.TestCase):
    """quality 的三态映射必须落到 checker 既有的 None/True/False 语义上。"""

    def test_mapping_is_stable(self):
        from core import quality as Q
        self.assertIs(Q.Judgement(Q.VERDICT_PASS).checker_state, True)
        self.assertIs(Q.Judgement(Q.VERDICT_FAIL).checker_state, False)
        self.assertIsNone(Q.Judgement(Q.VERDICT_UNKNOWN).checker_state)












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

