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

