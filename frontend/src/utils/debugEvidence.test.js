import { test } from "node:test";
import assert from "node:assert/strict";
import { buildEvidenceSummary, buildStepSummary } from "./debugEvidence.js";

test("App 结果区分真实引擎和另抓页面", () => {
  const s = buildEvidenceSummary({
    channel: "app",
    step: { verdict: "pass" },
    page: { origin: "http", cached: true },
  });
  assert.deepEqual(s.sources.map((x) => x.label), ["App 实测", "缓存页面"]);
  assert.ok(s.boundaries.some((x) => x.includes("Cookie")));
});

test("本地投影和 unknown 必须显示边界", () => {
  const s = buildEvidenceSummary({
    channel: "jvm",
    step: { verdict: "unknown" },
    replay: { rule_error: "不支持 xpath" },
    layer: { layer: "L3" },
  });
  assert.deepEqual(s.sources.map((x) => x.label), ["本机引擎", "本地规则投影"]);
  assert.equal(s.boundaries.length, 3);
});

test("步骤摘要区分本机引擎与辅助材料的可信等级", () => {
  const s = buildStepSummary({
    channel: "jvm",
    step: { name: "content", verdict: "unknown", reason: "正文未取到" },
    page: { origin: "http", cached: true },
    replay: { rule_error: "不支持 xpath" },
    layer: { layer: "L3" },
    action: { kind: "diagnosis", label: "查看本机缺口" },
  });
  assert.equal(s.verdictLabel, "无法判定");
  assert.equal(s.reason, "正文未取到");
  assert.equal(s.sources.find((x) => x.key === "jvm").trust, "authoritative");
  assert.equal(s.sources.find((x) => x.key === "page").trust, "supporting");
  assert.equal(s.sources.find((x) => x.key === "replay").trust, "projection");
  assert.equal(s.action.label, "查看本机缺口");
  assert.equal(s.stale, false);
});

test("规则已修改时步骤摘要主动作变为重新调试", () => {
  const s = buildStepSummary({
    channel: "jvm",
    step: { name: "content", verdict: "pass" },
    stale: true,
  });
  assert.deepEqual(s.action, { kind: "rerun", label: "重新调试本步" });
  assert.equal(s.stale, true);
});
