import { test } from "node:test";
import assert from "node:assert/strict";
import { assessRuleQuality } from "./ruleQuality.js";

test("优质规则需要本地命中、真实值、去重和数量一致", () => {
  const q = assessRuleQuality({
    rule: ".chapters@tag.a@href",
    preview: { hits: 10, uniq: 10 },
    values: ["/1", "/2", "/3"],
    expectedCount: 3,
    verdict: "pass",
    kind: "link",
  });
  assert.equal(q.key, "good");
  assert.equal(q.metrics.uniqueValues, 3);
});

test("DOM 命中但没有真实值不能算优质", () => {
  const q = assessRuleQuality({
    rule: ".content@text",
    preview: { hits: 1, uniq: 1 },
    values: [],
    verdict: "unknown",
    kind: "text",
  });
  assert.notEqual(q.key, "good");
  assert.ok(q.reasons.includes("真实引擎没有返回有效值"));
});

test("重复值和宽选择器会降低质量", () => {
  const q = assessRuleQuality({
    rule: "tag.a@href",
    preview: { hits: 20, uniq: 20 },
    values: ["/same", "/same", "/same"],
    expectedCount: 3,
    verdict: "pass",
    kind: "link",
  });
  assert.notEqual(q.key, "good");
  assert.ok(q.reasons.includes("结果重复较多"));
  assert.ok(q.reasons.includes("选择范围过宽，容易混入导航或噪声"));
});
