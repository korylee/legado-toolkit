import { test } from "node:test";
import assert from "node:assert/strict";
import { buildEvidenceSummary, buildStepSummary, matchedSampleValues } from "./debugEvidence.js";

test("命中 DOM 里的值与属性都要取出来，作为「App 实际拿到了什么」", () => {
  const html = '<div class="item"><h3><a href="/b/1">诡秘之主</a></h3></div>'
    + '<div class="item"><h3><a href="/b/2">绍宋</a></h3></div>';
  const vals = matchedSampleValues(html);
  assert.ok(vals.includes("诡秘之主"));
  assert.ok(vals.includes("绍宋"));
  assert.ok(vals.includes("/b/1"), "取值动作取到的属性也是值");
});

test("没有命中 DOM 时给空数组（调用方退回事件流水，不编值）", () => {
  assert.deepEqual(matchedSampleValues(""), []);
  assert.deepEqual(matchedSampleValues(null), []);
});

test("重复值与超长属性值不收（它只是比对样本，不是正文）", () => {
  const long = "x".repeat(400);
  const vals = matchedSampleValues(`<p>甲</p><p>甲</p><img src="${long}">`);
  assert.deepEqual(vals, ["甲"]);
});

test("App 结果区分真实引擎和另抓页面", () => {
  const s = buildEvidenceSummary({
    channel: "app",
    step: { verdict: "pass" },
    page: { origin: "http", cached: true },
  });
  assert.deepEqual(s.sources.map((x) => x.label), ["App 实测", "缓存页面"]);
  assert.ok(s.boundaries.some((x) => x.includes("Cookie")));
});

test("非引擎结果与 unknown 必须显示边界", () => {
  const s = buildEvidenceSummary({
    channel: "jvm",
    step: { verdict: "unknown" },
    layer: { layer: "L3" },
  });
  assert.deepEqual(s.sources.map((x) => x.label), ["本机引擎"]);
  assert.equal(s.boundaries.length, 2);
});

test("步骤摘要区分本机引擎与辅助材料的可信等级", () => {
  const s = buildStepSummary({
    channel: "jvm",
    step: { name: "content", verdict: "unknown", reason: "正文未取到" },
    page: { origin: "http", cached: true },
    layer: { layer: "L3" },
    action: { kind: "diagnosis", label: "查看本机缺口" },
  });
  assert.equal(s.verdictLabel, "无法判定");
  assert.equal(s.reason, "正文未取到");
  assert.equal(s.sources.find((x) => x.key === "jvm").trust, "authoritative");
  assert.equal(s.sources.find((x) => x.key === "page").trust, "supporting");
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
