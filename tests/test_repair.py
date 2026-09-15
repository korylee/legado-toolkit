# -*- coding: utf-8 -*-
"""P1 修复循环的离线测试（注入假 LLM 与假验证器，不联网）。"""

import asyncio
import unittest

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
        out = R.merge_proposal(SOURCE, {"ruleSearch": {"bookList": "NEW", "bookUrl": ""},
                                        "bookSourceType": 2, "reason": "x"})
        self.assertEqual(out["bookSourceName"], "测试站")
        self.assertEqual(out["bookSourceUrl"], "http://x")
        self.assertEqual(out["ruleSearch"]["bookList"], "NEW")
        self.assertEqual(out["ruleSearch"]["bookUrl"], "a@href")
        self.assertEqual(out["bookSourceType"], 2)


class PromptTests(unittest.TestCase):
    def test_prompt_carries_evidence_rules_and_failure(self):
        p = R.build_user_prompt(EVIDENCE, SOURCE,
                                {"steps": [{"name": "search", "ok": False, "detail": "无结果"}]})
        self.assertIn("测试站", p)
        self.assertIn(".old-list", p)
        self.assertIn("DOM-search", p)
        self.assertIn("无结果", p)
        self.assertIn("image", p)


if __name__ == "__main__":
    unittest.main()

