import { test } from "node:test";
import assert from "node:assert/strict";
import {
  actionFromCandidate,
  actionFromEvidence,
  actionFromFix,
  actionFromProbe,
  actionFromRerun,
  actionAvailability,
  actionWithAvailability,
} from "./debugAction.js";

test("fix 统一成编辑规则动作并从步骤推导字段", () => {
  const action = actionFromFix(
    { kind: "edit_rule", target: "selector" },
    { step: "content", draftRevision: 3, sourceUrl: "https://a.test" },
  );
  assert.deepEqual(action, {
    kind: "edit_rule", step: "content", sourceUrl: "https://a.test", draftRevision: 3,
    origin: "manual", evidenceRefs: [], requiresConfirmation: false,
    requiresVerification: true, target: "selector", field: "ruleContent.content",
  });
});

test("候选和应用并重调动作保持同一形状", () => {
  const action = actionFromCandidate(
    { css: ".article", samples: ["正文"] },
    { step: "content", rerun: true, candidateId: "c1", evidenceRefs: ["page:content"] },
  );
  assert.equal(action.kind, "apply_and_rerun");
  assert.equal(action.field, "ruleContent.content");
  assert.equal(action.rule, ".article");
  assert.equal(action.candidateId, "c1");
  assert.deepEqual(action.evidenceRefs, ["page:content"]);
});

test("Agent 候选需要确认，已通过引擎的候选仍可标为无需再次验收", () => {
  const proposed = actionFromCandidate({ rule: ".content" }, {
    step: "content", origin: "agent", candidateId: "a1",
  });
  const verified = actionFromCandidate({ rule: ".content" }, {
    step: "content", origin: "agent", verified: true,
  });
  assert.equal(proposed.requiresConfirmation, true);
  assert.equal(proposed.requiresVerification, true);
  assert.equal(verified.requiresConfirmation, true);
  assert.equal(verified.requiresVerification, false);
});

test("取证动作保留通道，App 取证需要确认", () => {
  const app = actionFromProbe({ kind: "app" }, { step: "content" });
  const jvm = actionFromProbe({ kind: "jvm" }, { step: "content" });
  assert.equal(app.kind, "probe");
  assert.equal(app.channel, "app");
  assert.equal(app.requiresConfirmation, true);
  assert.equal(jvm.channel, "jvm");
  assert.equal(jvm.requiresConfirmation, false);
});

test("重调动作带步骤，未知 fix 不进入协议", () => {
  assert.equal(actionFromRerun({ name: "content" }, { canRerun: true }).step, "content");
  assert.equal(actionFromFix({ kind: "unknown" }, { step: "content" }), null);
});

test("证据引用统一成 open_evidence 动作", () => {
  const action = actionFromEvidence("matched:content", {
    step: "content", draftRevision: 4,
  });
  assert.equal(action.kind, "open_evidence");
  assert.equal(action.step, "content");
  assert.equal(action.tab, "matched");
  assert.deepEqual(action.evidenceRefs, ["matched:content"]);
  assert.equal(actionAvailability(action, {}).enabled, true);
  assert.equal(actionFromEvidence("bad-ref", {}), null);
  assert.equal(actionAvailability({ kind: "open_evidence" }, {}).reasonCode,
    "evidence_missing");
});
test("首次 run_debug 不要求已有可重调链接", () => {
  const action = actionFromFix(
    { kind: "run_debug", target: "search", query: "关键词" },
    { step: "search", sourceUrl: "https://a.test" },
  );
  assert.equal(actionAvailability(action, {
    running: false,
    canRerun: false,
    sourceUrl: "https://a.test",
  }).enabled, true);
});

test("已有结果的 rerun 仍要求可用链接", () => {
  const action = actionFromRerun({ name: "content" }, { sourceUrl: "https://a.test" });
  assert.equal(actionAvailability(action, {
    running: false,
    canRerun: false,
    sourceUrl: "https://a.test",
  }).reasonCode, "rerun_unavailable");
});

test("缺少 probe 时不猜测 App 通道", () => {
  assert.equal(actionFromProbe(null, { step: "content" }), null);
});
test("可执行性只做入口校验，不判定规则结果", () => {
  const action = actionFromCandidate({ rule: ".content" }, { step: "content" });
  assert.equal(actionAvailability(action, { running: false, canRerun: true }).enabled, true);
  assert.equal(actionAvailability(action, { running: true, canRerun: true }).reasonCode, "run_in_progress");
  assert.equal(actionAvailability({ kind: "apply_candidate", field: "", rule: "" }, {}).reasonCode,
    "candidate_incomplete");
  assert.equal(actionAvailability(action, { running: false, appAvailable: false }).enabled, true,
    "候选动作不应被 App 取证能力阻塞");
});

test("可执行性结果附加在动作上，原始协议字段保留", () => {
  const action = actionWithAvailability(
    actionFromProbe({ kind: "app" }, { step: "content" }),
    { running: false, appAvailable: false },
  );
  assert.equal(action.enabled, false);
  assert.equal(action.reasonCode, "app_unavailable");
  assert.equal(action.channel, "app");
});

test("没有单字段可改的步骤给编辑视图出口，而不是点了没用的动作", () => {
  const action = actionFromFix({ kind: "edit_rule", target: "selector" }, { step: "explore" });
  assert.equal(action.field, "");
  assert.equal(action.fallback, "basic_editor");
  assert.equal(actionAvailability(action, {}).enabled, true);

  const orphan = { kind: "edit_rule", field: "" };
  assert.equal(actionAvailability(orphan, {}).reasonCode, "rule_field_missing",
    "既没有字段也没有编辑视图出口时仍要给出结构化原因");
});

test("空源只在明确给出空地址时拦截，缺该事实不算空", () => {
  const rerun = actionFromRerun({ name: "content" }, { canRerun: true });
  assert.equal(actionAvailability(rerun, { sourceUrl: "" }).reasonCode, "source_missing");
  assert.equal(actionAvailability(rerun, {}).enabled, true,
    "没有 sourceUrl 这个事实时不当成源没有地址");
  assert.equal(actionAvailability(rerun, { sourceUrl: "https://a.test" }).enabled, true);
});

test("候选过期：草稿改过就拦候选，但不拦重调", () => {
  const candidate = actionFromCandidate({ rule: ".content" }, {
    step: "content", candidateId: "c1", draftRevision: 3,
  });
  assert.equal(actionAvailability(candidate, { draftRevision: 3 }).enabled, true);
  assert.equal(actionAvailability(candidate, { draftRevision: 4 }).reasonCode, "candidate_stale");

  const rerun = actionFromRerun({ name: "content" }, { draftRevision: 3, canRerun: true });
  assert.equal(actionAvailability(rerun, { draftRevision: 4 }).enabled, true,
    "改完规则再重调正是主流程，不能按候选过期拦掉");
});
