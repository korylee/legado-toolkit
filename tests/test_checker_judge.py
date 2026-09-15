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
import sys
import types
import unittest

# 本文件用假的 _request 替掉网络层，不依赖真实客户端（与 test_checker_cache 同约定）
sys.modules.setdefault("aiohttp", types.ModuleType("aiohttp"))

from core import checker
from core.checker import AsyncChecker
from core.loader import fingerprint
from core.models import BookSourceRecord, Health


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

    async def _request(self, session, url, method="GET", headers=None,
                       allow_redirects=True):
        self.requested.append(url)
        page = self.pages.get(url)
        if page is None:
            return 404, b"", 1.0, ""        # 未预置的 URL 一律 404
        return 200, page.encode("utf-8"), 1.0, ""


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
        """判定逻辑变了，缓存必须整体作废，否则收拢等于没做。"""
        self.assertEqual(checker.CACHE_VERSION, 6)

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

if __name__ == "__main__":
    unittest.main()

