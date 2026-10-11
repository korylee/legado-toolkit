import { test } from "node:test";
import assert from "node:assert/strict";
import {
  evidenceAction,
  evidenceExpected,
  parseEvidenceRef,
  recommendedEvidence,
} from "./debugEvidenceNavigation.js";

test("证据引用解析为当前步骤和 Tab", () => {
  assert.deepEqual(parseEvidenceRef("matched:content"), {
    ref: "matched:content",
    kind: "matched",
    step: "content",
    tab: "matched",
    label: "看命中源码",
  });
  assert.equal(parseEvidenceRef("unknown:content"), null);
  assert.equal(parseEvidenceRef("page:"), null);
});

test("no_hit 优先推荐命中源码和整页源码", () => {
  const actions = recommendedEvidence("no_hit", [
    "step:content", "page:content", "matched:content", "values:content",
  ]);
  assert.deepEqual(actions.map((item) => item.evidenceRef), [
    "matched:content", "page:content", "values:content",
  ]);
  assert.equal(actions[0].kind, "open_evidence");
  assert.equal(actions[0].tab, "matched");
});

test("fail_content 推荐提取值，缺少推荐类型时保留已有材料顺序", () => {
  const content = recommendedEvidence("fail_content", ["step:content", "values:content"]);
  assert.deepEqual(content.map((item) => item.evidenceRef), ["values:content"]);

  const fallback = recommendedEvidence("unknown_code", ["step:content", "events:content"]);
  assert.deepEqual(fallback.map((item) => item.evidenceRef), ["events:content"]);
});

test("只对需要材料的缺口显示缺证据状态", () => {
  assert.equal(evidenceExpected("no_hit"), true);
  assert.equal(evidenceExpected("fail_content"), true);
  assert.equal(evidenceExpected("rule_missing"), false);
  assert.equal(evidenceExpected("unknown_code"), false);
});
test("证据动作带当前草稿版本且不要求确认或验证", () => {
  const action = evidenceAction("page:content", {
    sourceUrl: "https://a.test",
    draftRevision: 4,
  });
  assert.equal(action.kind, "open_evidence");
  assert.equal(action.step, "content");
  assert.equal(action.tab, "page");
  assert.equal(action.draftRevision, 4);
  assert.equal(action.requiresConfirmation, false);
  assert.equal(action.requiresVerification, false);
});
