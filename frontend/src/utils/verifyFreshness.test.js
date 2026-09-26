// verifyFreshness 的断言（strengthen-src）：过期判据是「分步」的——改哪组规则只
// 过期映射到的步骤，没改的结论继续可用；点过「重新调试本步」的步骤不再标过期。
// 这三条就是验收原文，倒着写会让「改一个字段整份作废」或「改了还显示绿色」复活。

import assert from "node:assert/strict";
import test from "node:test";

import { snapshotRuleGroups, staleVerifySteps } from "./verifyFreshness.js";

const RULES = {
  ruleSearch: { bookList: ".item", name: ".name@text", bookUrl: "a@href" },
  ruleBookInfo: { name: ".title@text" },
  ruleToc: { chapterList: ".chapters" },
  ruleContent: { content: ".content@text" },
};

test("改了哪组规则只过期映射到的步骤", () => {
  const snap = snapshotRuleGroups(RULES);
  const changed = { ...RULES, ruleToc: { chapterList: ".chapter-list" } };
  const stale = staleVerifySteps(snap, changed);
  assert.deepEqual([...stale], ["toc"]);
});

test("bookUrl 规则改动同时波及 search 与 bookUrl（同页求值）", () => {
  const snap = snapshotRuleGroups(RULES);
  const changed = {
    ...RULES,
    ruleSearch: { ...RULES.ruleSearch, bookUrl: "a@data-url" },
  };
  const stale = staleVerifySteps(snap, changed);
  assert.ok(stale.has("search"));
  assert.ok(stale.has("bookUrl"));
});

test("未改动的步骤结论保留可用", () => {
  const snap = snapshotRuleGroups(RULES);
  const changed = { ...RULES, ruleContent: { content: "#content@text" } };
  const stale = staleVerifySteps(snap, changed);
  assert.ok(stale.has("content"));
  assert.ok(!stale.has("search"));
  assert.ok(!stale.has("toc"));
});

test("重新调试过的步骤不再标过期；没重验的仍过期", () => {
  const snap = snapshotRuleGroups(RULES);
  const changed = {
    ...RULES,
    ruleToc: { chapterList: ".chapter-list" },
    ruleContent: { content: "#content@text" },
  };
  const stale = staleVerifySteps(snap, changed, ["toc"]);
  assert.ok(!stale.has("toc"));
  assert.ok(stale.has("content"));
});

test("没有快照（没生成过验证）时永远不过期", () => {
  assert.equal(staleVerifySteps(null, RULES).size, 0);
});
