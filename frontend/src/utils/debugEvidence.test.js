import { test } from "node:test";
import assert from "node:assert/strict";
import { buildEvidenceSummary } from "./debugEvidence.js";

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
