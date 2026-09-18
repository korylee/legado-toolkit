# -*- coding: utf-8 -*-
"""P1 修复循环的离线测试（注入假 LLM 与假验证器，不联网）。"""

import asyncio
import unittest
from unittest.mock import patch

from core.repair import loop as R

EVIDENCE = {"ok": True, "name": "测试站", "url": "http://x", "keyword": "海贼王",
            "pages": {"search": "DOM-search", "detail": "DOM-detail"},
            "failures": [], "notes": [], "repeats": [], "heuristic": {},
            "chapter_kind": "image"}

SOURCE = {"bookSourceName": "测试站", "bookSourceUrl": "http://x", "bookSourceType": 0,
          "searchUrl": "/search?q={{key}}",
          "ruleSearch": {"bookList": ".old-list", "bookUrl": "a@href"},
          "ruleToc": {"chapterList": ".old-chap", "chapterUrl": "a@href"},
          "ruleContent": {"content": "id.old@text"}}


class FakeLLM:
    enabled = True

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    async def chat(self, system, user, session=None, temperature=0.2):
        self.calls += 1
        return self.replies.pop(0) if self.replies else None


def make_verifier(want):
    """只有 bookList 等于 want 才算验证通过。"""
    def _v(source, keyword, detail_url=""):
        good = (source.get("ruleSearch") or {}).get("bookList") == want
        return {"steps": [{"name": "search", "ok": good, "detail": "n 条"}],
                "all_ok": good}
    return _v


class RepairLoopTests(unittest.TestCase):
    def test_fixes_after_one_retry(self):
        llm = FakeLLM(['{"ruleSearch":{"bookList":"WRONG"},"reason":"第一次猜错"}',
                       '{"ruleSearch":{"bookList":"GOOD"},"reason":"第二次对了"}'.replace(chr(34), chr(34))])
        llm.replies = ["{\"ruleSearch\":{\"bookList\":\"WRONG\"},\"reason\":\"第一次猜错\"}",
                       "{\"ruleSearch\":{\"bookList\":\"GOOD\"},\"reason\":\"第二次对了\"}"]
        res = asyncio.run(R.repair_one(None, llm, SOURCE, "海贼王", max_rounds=3,
                                       evidence=EVIDENCE, verifier=make_verifier("GOOD")))
        self.assertEqual(res["status"], "fixed")
        self.assertEqual(res["rounds"], 2)
        self.assertEqual(res["source"]["ruleSearch"]["bookList"], "GOOD")
        self.assertEqual(len(res["history"]), 2)

    def test_gives_up_after_max_rounds(self):
        llm = FakeLLM([])
        llm.replies = ["{\"ruleSearch\":{\"bookList\":\"A\"}}"] * 3
        res = asyncio.run(R.repair_one(None, llm, SOURCE, "k", max_rounds=3,
                                       evidence=EVIDENCE, verifier=make_verifier("GOOD")))
        self.assertEqual(res["status"], "failed")
        self.assertEqual(res["rounds"], 3)

    def test_already_ok_skips_llm(self):
        llm = FakeLLM([])
        res = asyncio.run(R.repair_one(None, llm, SOURCE, "k", evidence=EVIDENCE,
                                       verifier=make_verifier(".old-list")))
        self.assertEqual(res["status"], "already_ok")
        self.assertEqual(llm.calls, 0)

    def test_dry_run_without_api_key(self):
        class Off:
            enabled = False
        res = asyncio.run(R.repair_one(None, Off(), SOURCE, "k", evidence=EVIDENCE,
                                       verifier=make_verifier("GOOD")))
        self.assertEqual(res["status"], "dry_run")

    def test_no_evidence(self):
        llm = FakeLLM([])
        res = asyncio.run(R.repair_one(None, llm, SOURCE, "k",
                                       evidence={"ok": False, "pages": {}},
                                       verifier=make_verifier("GOOD")))
        self.assertEqual(res["status"], "no_evidence")

    def test_bad_json_recorded_not_crashed(self):
        llm = FakeLLM([])
        llm.replies = ["我不是 JSON", "{\"ruleSearch\":{\"bookList\":\"GOOD\"}}"]
        res = asyncio.run(R.repair_one(None, llm, SOURCE, "k", max_rounds=2,
                                       evidence=EVIDENCE, verifier=make_verifier("GOOD")))
        self.assertEqual(res["status"], "fixed")
        self.assertIn("不是合法 JSON", res["history"][0]["reason"])


# ------------------------------------------------------------------ 剥离点覆盖
#
# 证据原文按**真实形状与体积**构造：正文 3000 字符、命中 HTML 5000 字符、整页 HTML
# 6000 字符。这一点是本用例能不能抓住变异的关键——上面那个 make_verifier 返回的瘦
# 结果连 `pages` 都没有，剥与不剥完全同形，所以 loop.py 的两处剥离点长期零覆盖
# （实测：去掉 `:150` 的 strip_evidence，214 条旧测试全绿）。
FAT_VALUES = ["正文" * 1000]
FAT_MATCHED_HTML = "<div>" * 1000
FAT_PAGE_HTML = "<html>" * 1000


def make_fat_verifier(pass_when):
    """返回带真实证据形状的验证器：bookList 等于 pass_when 才算通过。

    形状对齐 `services.add_source.verify_chain` 的实际返回，steps[] 的键口径见
    `quality.Judgement.as_step_dict`——剥离点的正确性完全取决于这个形状，所以
    不能自己发明一个「差不多」的返回体。
    """
    def _v(source, keyword, detail_url=""):
        good = (source.get("ruleSearch") or {}).get("bookList") == pass_when
        # 每次调用都新建嵌套对象：剥离点会同时持有剥离前后两份，共用可变对象
        # 会让用例把「就地改写」误判成「已剥离」。
        step = {
            "name": "content",
            "ok": good,
            "detail": "1 个节点，3000 字符" if good else "规则命中 0 个节点",
            "verdict": "pass" if good else "fail",
            "has_notes": True,
            "notes": ["正文较短"],
            "reason": "" if good else "规则命中 0 个节点",
            "shape": "text",
            "evidence": {"chars": len(FAT_VALUES[0])},
            "rule_error": "",
            "url": "http://x/ch/1",
            "page_id": "chapter",
            "values": list(FAT_VALUES),
            "matched_html": FAT_MATCHED_HTML,
        }
        page = {"id": "chapter", "url": "http://x/ch/1", "status": 200, "charset": "",
                "html": FAT_PAGE_HTML, "len": len(FAT_PAGE_HTML), "truncated": False}
        return {"steps": [step], "pages": [page], "all_ok": good}
    return _v


class RepairLoopStripEvidenceTests(unittest.TestCase):
    """守 `core/repair/loop.py` 的两处剥离点（`:150` 的 `before`、`:192` 轮内的 `v`）。

    这两处本任务对修复路径的**全部价值**：`before` / `out["after"]` / `history[].verify`
    最多同时持有 3 份完整证据，`repair_many` 还用 `asyncio.gather` 把全部源的结果留在
    内存里。没有用例守着，将来重构掉它们不会有任何提示，几百 MB 的内存问题静默回归。
    """

    def assert_stripped(self, v):
        """证据原文一律清空，判定结论一律保留。"""
        self.assertEqual(v["pages"], [])
        self.assertTrue(v["steps"])          # 断言的前提：确实拿到了带 step 的结果
        for s in v["steps"]:
            self.assertEqual(s["values"], [])
            self.assertEqual(s["matched_html"], "")

    def test_before_stripped_on_already_ok(self):
        """早退路径：第一次验证就通过，此时 `out["after"]` / `history` 不参与。"""
        llm = FakeLLM([])
        res = asyncio.run(R.repair_one(None, llm, SOURCE, "k", evidence=EVIDENCE,
                                       verifier=make_fat_verifier(".old-list")))
        self.assertEqual(res["status"], "already_ok")
        self.assert_stripped(res["before"])
        self.assertIsNone(res["after"])
        self.assertEqual(res["history"], [])

    def test_after_and_history_stripped_on_fixed(self):
        """循环路径（第 2 轮修好）：`after` 与每轮的 verify 都不能留证据。"""
        llm = FakeLLM([])
        llm.replies = ["{\"ruleSearch\":{\"bookList\":\"WRONG\"},\"reason\":\"第一次猜错\"}",
                       "{\"ruleSearch\":{\"bookList\":\"GOOD\"},\"reason\":\"第二次对了\"}"]
        res = asyncio.run(R.repair_one(None, llm, SOURCE, "k", max_rounds=3,
                                       evidence=EVIDENCE, verifier=make_fat_verifier("GOOD")))
        self.assertEqual(res["status"], "fixed")
        self.assert_stripped(res["before"])
        self.assert_stripped(res["after"])
        self.assertEqual(len(res["history"]), 2)
        for h in res["history"]:
            self.assert_stripped(h["verify"])
        # 判定结论不能被一起剥掉——否则「只要剥光就算过」也能绿，那是另一种假测试
        self.assertIs(res["after"]["all_ok"], True)
        self.assertEqual(res["after"]["steps"][0]["verdict"], "pass")
        self.assertEqual(res["after"]["steps"][0]["detail"], "1 个节点，3000 字符")
        self.assertIs(res["history"][0]["verify"]["all_ok"], False)
        self.assertEqual(res["history"][0]["verify"]["steps"][0]["verdict"], "fail")
        self.assertEqual(res["history"][0]["verify"]["steps"][0]["reason"], "规则命中 0 个节点")

    def test_after_stripped_on_failure(self):
        """失败路径：`out["after"]` 来自 `cur_verify`（最后一轮的 v），同样必须已剥。"""
        llm = FakeLLM([])
        llm.replies = ["{\"ruleSearch\":{\"bookList\":\"WRONG\"}}"] * 3
        res = asyncio.run(R.repair_one(None, llm, SOURCE, "k", max_rounds=3,
                                       evidence=EVIDENCE, verifier=make_fat_verifier("GOOD")))
        self.assertEqual(res["status"], "failed")
        self.assert_stripped(res["after"])
        for h in res["history"]:
            self.assert_stripped(h["verify"])
        self.assertIs(res["after"]["all_ok"], False)
        self.assertEqual(res["after"]["steps"][0]["verdict"], "fail")


class MergeTests(unittest.TestCase):
    def test_keeps_metadata_and_ignores_empty_values(self):
        out, skipped = R.merge_proposal(SOURCE, {"ruleSearch": {"bookList": "NEW",
                                                               "bookUrl": ""},
                                                 "bookSourceType": 2, "reason": "x"})
        self.assertEqual(out["bookSourceName"], "测试站")
        self.assertEqual(out["bookSourceUrl"], "http://x")
        self.assertEqual(out["ruleSearch"]["bookList"], "NEW")
        self.assertEqual(out["ruleSearch"]["bookUrl"], "a@href")
        self.assertEqual(out["bookSourceType"], 2)
        self.assertEqual(skipped, [])


#: 「混合」源：搜索段可回放（对，但规则已失效），目录/正文段是 JS（本地回放不了）。
#: 这是护栏真正起作用的形状——全不可回放的源在 verify_chain 那里就是 all_ok
#: （unknown 算 ok），修复循环根本不会启动。
MIXED_SOURCE = {
    "bookSourceName": "混合站", "bookSourceUrl": "http://y", "bookSourceType": 0,
    "searchUrl": "/s?q={{key}}",
    "ruleSearch": {"bookList": ".old-list", "bookUrl": "a@href"},
    "ruleToc": {"chapterList": ".old-chap",
                "chapterUrl": "<js>return baseUrl + '/c/' + id</js>"},
    "ruleContent": {"content": "id.content@text"},
}


class MergeGuardTests(unittest.TestCase):
    """本地回放不了的字段：不许覆盖、不许采纳，但**要报出来**（口径同 quality：
    unknown = 我们不判，不是「没问题」）。两道拦各挡一种把源改坏的方式。"""

    def test_unreplayable_current_is_not_overwritten(self):
        """模型会把 App 里能用的 JS 规则换成 CSS 规则——那等于把好的改坏，
        而 all_ok 只看回放结果，没有别的检查会拦。"""
        merged, skipped = R.merge_proposal(
            MIXED_SOURCE, {"ruleToc": {"chapterUrl": ".new a@href"}})
        self.assertEqual(merged["ruleToc"]["chapterUrl"],
                         "<js>return baseUrl + '/c/' + id</js>")
        self.assertEqual([s["field"] for s in skipped], ["ruleToc.chapterUrl"])
        self.assertIn("当前规则本地回放不了", skipped[0]["why"])

    def test_unreplayable_proposal_is_not_adopted(self):
        """提议本身回放不了时不能落地：落地了本地验不了它，而 verify_chain 对
        「回放不了」判 unknown（ok=True）——它会假装成修好了。"""
        merged, skipped = R.merge_proposal(
            SOURCE, {"ruleSearch": {"bookList": "@js:return doc.select('.x')"}})
        self.assertEqual(merged["ruleSearch"]["bookList"], ".old-list")
        self.assertIn("提议的规则本地回放不了", skipped[0]["why"])

    def test_replayable_field_is_still_updated(self):
        """反向保护：能回放的字段必须照常覆盖，否则护栏把修复本身也挡住了。"""
        merged, skipped = R.merge_proposal(
            MIXED_SOURCE, {"ruleSearch": {"bookList": ".new-list"}})
        self.assertEqual(merged["ruleSearch"]["bookList"], ".new-list")
        self.assertEqual(skipped, [])

    def test_skipped_reaches_result_and_report(self):
        """跳过清单必须走到结果与报告里——静默丢弃等于「模型修好了」的错觉。"""
        llm = FakeLLM(["{\"ruleSearch\":{\"bookList\":\".new-list\"},"
                       "\"ruleToc\":{\"chapterUrl\":\".new a@href\"}}"])
        res = asyncio.run(R.repair_one(None, llm, MIXED_SOURCE, "k", max_rounds=1,
                                       evidence=EVIDENCE,
                                       verifier=make_verifier(".new-list")))
        self.assertEqual(res["status"], "fixed")
        self.assertEqual([s["field"] for s in res["skipped"]], ["ruleToc.chapterUrl"])
        report = R.build_report([res])
        self.assertIn("未改动 `ruleToc.chapterUrl`", report)
        self.assertIn("本地回放不了", report)

    def test_skipped_is_an_empty_list_when_clean(self):
        """键必须常在（消费方按它判断有没有留下验不了的部分），哪怕是空的。"""
        llm = FakeLLM(["{\"ruleSearch\":{\"bookList\":\"GOOD\"}}"])
        res = asyncio.run(R.repair_one(None, llm, SOURCE, "k", max_rounds=1,
                                       evidence=EVIDENCE, verifier=make_verifier("GOOD")))
        self.assertEqual(res["skipped"], [])


class PromptTests(unittest.TestCase):
    def test_prompt_carries_evidence_rules_and_failure(self):
        p = R.build_user_prompt(EVIDENCE, SOURCE,
                                {"steps": [{"name": "search", "ok": False, "detail": "无结果"}]})
        self.assertIn("测试站", p)
        self.assertIn(".old-list", p)
        self.assertIn("DOM-search", p)
        self.assertIn("无结果", p)
        self.assertIn("image", p)


class LoginWallTests(unittest.TestCase):
    """抓到的是登录页时**不许修**。

    修复循环最贵的一种失败不是「修不好」，是「**看着修好了**」：登录墙返回的是
    200 + 登录页，那是「抓取成功」，于是登录页的 DOM 会被当成证据喂给模型，
    模型只能盲改；而本地回放对着登录页跑，还可能判「通过」。
    """

    WALL = "<html><body>请先登录后查看内容<input type='password'></body></html>"
    LIST_PAGE = '<div class="list"><a href="/b/1">斗破苍穹</a></div>'
    #: 声明了 cookie jar 的源：登录墙判定那一档（200 + 登录词）以它为前提。
    #: 不声明的源**故意不判**——「请登录」在正常页面的导航栏里太常见
    #: （实测：裸 login 误伤过 797 条）。
    WALL_SOURCE = dict(SOURCE, enabledCookieJar=True)

    def _evidence(self, source, pages):
        from core.repair import evidence as E

        def fake_fetch(url, *a, **k):
            return pages.get(url, pages.get("*", ""))

        with patch("services.add_source.fetch", side_effect=fake_fetch):
            return asyncio.run(E.build_evidence(source, "斗破苍穹"))

    def test_wall_is_not_registered_as_evidence(self):
        ev = self._evidence(self.WALL_SOURCE, {"*": self.WALL})
        self.assertEqual(ev["pages"], {})              # 登录页不进证据
        self.assertTrue(ev["login_wall"])              # 但要留痕（哪几页撞了）
        self.assertIs(ev["ok"], False)                 # 不算「有证据」
        self.assertTrue([f for f in ev["failures"] if "登录墙" in f])

    def test_normal_page_is_untouched(self):
        """反向保护：正常页不能被判成登录墙（判宽了等于把能修的源全挡掉）。"""
        ev = self._evidence(self.WALL_SOURCE, {"*": self.LIST_PAGE})
        self.assertEqual(ev["login_wall"], [])
        self.assertTrue(ev["pages"])

    def test_source_without_cookie_jar_keeps_working(self):
        """没声明 cookie jar 的源不判登录墙（判定表的前提，别在这里再抄一份）。"""
        ev = self._evidence(dict(SOURCE), {"*": self.WALL})
        self.assertEqual(ev["login_wall"], [])

    def test_repair_stops_with_its_own_status(self):
        """撞墙要给独立 status：用户才知道该去 App 里试，而不是以为站点坏了。"""
        ev = self._evidence(self.WALL_SOURCE, {"*": self.WALL})
        llm = FakeLLM([])
        res = asyncio.run(R.repair_one(None, llm, self.WALL_SOURCE, "k",
                                       evidence=ev, verifier=make_verifier("GOOD")))
        self.assertEqual(res["status"], "login_wall")
        self.assertEqual(llm.calls, 0)                 # 一轮都不烧
        report = R.build_report([res])
        self.assertIn("撞登录墙", report)

    def test_unreplayable_rule_is_not_called_broken(self):
        """「本地跑不了」不能报成「规则已失效」——后者会让人去改一条没坏的规则。"""
        src = dict(SOURCE, ruleSearch={"bookList": "@js:return doc.select('.x')",
                                       "bookUrl": "a@href"})
        ev = self._evidence(src, {"*": self.LIST_PAGE})
        self.assertTrue([f for f in ev["failures"] if "无法回放" in f])
        self.assertFalse([f for f in ev["failures"] if "搜索规则已失效" in f])


# ---------------------------------------------------------------- 变异记录
# 以下为实测（改坏 → `python -B -m unittest tests.test_repair` → 确认变红 → 还原）。
# 前六条是剥离点（见 RepairLoopStripEvidenceTests 上方），后四条是合并护栏：
#
#  M1  merge_proposal 不拦「当前值回放不了」（照覆盖）
#        → MergeGuardTests.test_unreplayable_current_is_not_overwritten 红
#  M2  merge_proposal 不拦「提议值回放不了」（照采纳）
#        → MergeGuardTests.test_unreplayable_proposal_is_not_adopted 红
#  M3  repair_one 不把跳过清单写进结果
#        → MergeGuardTests.test_skipped_reaches_result_and_report 红
#  M4  build_report 不列跳过清单
#        → MergeGuardTests.test_skipped_reaches_result_and_report 红
#
#  修复循环接登录墙（2026-09-18）：
#  M5  build_evidence 不判登录墙（登录页当证据收下）
#        → LoginWallTests.test_wall_is_not_registered_as_evidence 红
#  M6  撞墙时不给独立 status（落回 no_evidence）
#        → LoginWallTests.test_repair_stops_with_its_own_status 红
#  M7  本地跑不了的规则报成「搜索规则已失效」
#        → LoginWallTests.test_unreplayable_rule_is_not_called_broken 红
#        （M7 断的是**话术方向**：结论都对，但用户会去改一条没坏的规则）

if __name__ == "__main__":
    unittest.main()

