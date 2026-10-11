import { test } from "node:test";
import assert from "node:assert/strict";
import { buildDebugSummary } from "./debugSummary.js";

const baseDecision = {
  stepLabel: "正文",
  state: { verdict: "fail", reason: "正文内容不完整" },
  gap: { code: "fail_content", reason: "取到了 3 条，但判定不达标" },
  fix: { kind: "rerun", label: "重新调试本步" },
  probe: { kind: "app", label: "连 App 取运行时材料" },
  deferred_gaps: [{ code: "runtime_missing", reason: "缺少运行时材料", todo: "连 App 取证" }],
};

const step = {
  name: "content",
  values: ["正文"],
  matched_html: "<p>正文</p>",
};

test("完成结果：主动作来自 decision.fix，取证动作独立保留", () => {
  const summary = buildDebugSummary({
    result: { source: "jvm" },
    decision: baseDecision,
    step,
    page: { html: "<p>正文</p>" },
    events: [{ text: "content" }],
    channel: "jvm",
    target: "content",
    targetLabel: "正文",
    query: "https://example.test/chapter/1",
    draftRevision: 4,
    resultRevision: 4,
  });

  assert.equal(summary.status, "completed");
  assert.equal(summary.statusLabel, "调试已完成");
  assert.equal(summary.primaryAction.kind, "rerun");
  assert.equal(summary.primaryAction.type, "fix");
  assert.equal(summary.probeAction.kind, "app");
  assert.equal(summary.run.targetLabel, "正文");
  assert.equal(summary.source.stale, false);
  assert.deepEqual(summary.evidenceActions.map((item) => item.evidenceRef), [
    "values:content", "page:content", "matched:content",
  ]);
});

test("没有解决动作时才把取证提升为主动作", () => {
  const summary = buildDebugSummary({
    result: { source: "jvm" },
    decision: {
      state: { verdict: "unknown", reason: "缺少运行时材料" },
      gap: { code: "runtime_missing", reason: "缺少运行时材料" },
      fix: null,
      probe: { kind: "app", label: "连 App 取运行时材料" },
      deferred_gaps: [],
    },
    step: { name: "content" },
  });

  assert.equal(summary.primaryAction.kind, "app");
  assert.equal(summary.primaryAction.type, "probe");
  assert.equal(summary.probeAction, null);
});

test("运行中优先展示运行事实，不把旧结论当成当前结论", () => {
  const summary = buildDebugSummary({
    running: true,
    result: { source: "jvm", error: "旧结果" },
    decision: baseDecision,
    step,
    channel: "jvm",
    target: "search",
    runLine: { text: "正在排队：JVM 引擎被占用" },
    elapsedMs: 1200,
  });

  assert.equal(summary.status, "running");
  assert.equal(summary.reason, "正在排队：JVM 引擎被占用");
  assert.equal(summary.run.elapsedMs, 1200);
});

test("草稿 revision 不一致时摘要明确为过期", () => {
  const summary = buildDebugSummary({
    result: { source: "jvm" },
    decision: baseDecision,
    step,
    stale: true,
    draftRevision: 5,
    resultRevision: 4,
  });

  assert.equal(summary.status, "stale");
  assert.equal(summary.statusLabel, "结果已过期");
  assert.match(summary.reason, /当前草稿/);
  assert.equal(summary.source.draftRevision, 5);
  assert.equal(summary.source.resultRevision, 4);
});

test("草稿与结果 revision 不一致时摘要自行判过期（不依赖调用方传 stale）", () => {
  const summary = buildDebugSummary({
    result: { source: "jvm" },
    decision: baseDecision,
    step,
    draftRevision: 5,
    resultRevision: 4,
  });

  assert.equal(summary.status, "stale");
  assert.equal(summary.source.stale, true);
  assert.match(summary.reason, /当前草稿/);
});

test("没有 plan 时不自行生成缺口或动作", () => {
  const summary = buildDebugSummary({
    result: { source: "jvm" },
    decision: null,
    step: { name: "search", verdict: "fail" },
    planReady: false,
  });

  assert.equal(summary.primaryAction, null);
  assert.equal(summary.probeAction, null);
  assert.equal(summary.planReady, false);
  assert.match(summary.reason, /尚无可显示/);
});

test("取消等待保留用户可见的取消状态", () => {
  const summary = buildDebugSummary({
    result: { error: "已不再等待", cancelled: true },
    decision: null,
  });

  assert.equal(summary.status, "cancelled");
  assert.equal(summary.statusLabel, "已停止等待");
  assert.equal(summary.reason, "已不再等待");
});
