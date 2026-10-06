// 五格**渲染层**的断言：判据在后端（`core/agent_plan.py`），这里只验「码 → 中文句子」
// 「动作种类 → 按钮词」这两张映射表，以及取不到计划时不得自作判据。
// 码表与后端逐码一致由 `tests/test_agent_plan.py` 的契约测试钉住。
import { test } from "node:test";
import assert from "node:assert/strict";
import { buildDecision, decisionLines, fixView, probeView } from "./debugDecision.js";

const L3 = { layer: "L3", info: { name: "L3 加密负载", action: "用 webJs 读页面全局里的对象" },
             evidence: [], unsure: "" };
const L1 = { layer: "L1", info: { name: "L1 静态直出", action: "直接写选择器" },
             evidence: [], unsure: "" };

const STEP = { name: "content", verdict: "unknown", detail: "", values: [] };

//: 口袋漫画那一步：判据（后端）只给码，句子由这里出
const PLAN_RULE_MISSING = {
  layer: "L3",
  gap: { code: "rule_missing" },
  deferred_gaps: [{ code: "no_wanted_nodes" }, { code: "runtime_missing" }],
  fix: { kind: "edit_rule", target: "rule" },
  probe: null,
  ai: { eligible: false, reason_code: "rule_missing", material_kind: "" },
};

test("规则为空：现状是「没配规则」，主动作去补规则，不给取证", () => {
  const d = buildDecision({
    plan: PLAN_RULE_MISSING,
    step: STEP,
    want: { kind: "media", label: "正文图片/音频" },
    layer: L3,
    stats: { links: 7, images: 2, images_with_src: 0 },
  });
  assert.equal(d.gap.code, "rule_missing");
  assert.match(d.gap.reason, /没配「正文图片\/音频」规则/);
  assert.equal(d.fix.kind, "edit_rule");
  assert.equal(d.fix.label, "去补规则");
  assert.equal(d.probe, null);
  assert.equal(d.ai.eligible, false);
  assert.equal(d.ai.hint, "", "材料不对时不渲染理由，现状行已经说过了");
});

test("其余缺口一并带出来，句子按码取", () => {
  const d = buildDecision({
    plan: PLAN_RULE_MISSING,
    step: STEP,
    want: { kind: "media", label: "正文图片/音频" },
    layer: L3,
    stats: { links: 7, images: 2, images_with_src: 0 },
  });
  assert.deepEqual(d.deferred_gaps.map((g) => g.code),
                   ["no_wanted_nodes", "runtime_missing"]);
  assert.match(d.deferred_gaps[0].reason, /带 src 的只有 0 个/);
  assert.doesNotMatch(d.deferred_gaps[0].reason, /undefined/);
});

test("动态层缺材料：解决按层给按钮词，取证写清是取材料", () => {
  const d = buildDecision({
    plan: {
      layer: "L2",
      gap: { code: "runtime_missing" },
      deferred_gaps: [],
      fix: { kind: "edit_rule", target: "layer", layer: "L2" },
      probe: { kind: "app" },
      ai: { eligible: false, reason_code: "material_mismatch", material_kind: "runtime_dom" },
    },
    step: { name: "search", verdict: "unknown" },
    want: { kind: "list", label: "书目列表" },
    layer: { layer: "L2", info: { name: "L2 JS 壳", action: "换能跑 JS 的通道 + webJs" },
             evidence: [], unsure: "" },
  });
  assert.equal(d.fix.label, "声明 webView + webJs", "按钮词出自 layers.js 那张表");
  assert.equal(d.probe.kind, "app");
  assert.match(d.probe.label, /取运行时材料/);
  assert.equal(d.gap.todo, "换能跑 JS 的通道 + webJs", "折叠里的说明用同一张表的 action");
});

test("L5 的按层修法是登录，不是 webJs", () => {
  const d = buildDecision({
    plan: { layer: "L5", gap: { code: "runtime_missing" }, deferred_gaps: [],
            fix: { kind: "edit_rule", target: "login" }, probe: null, ai: {} },
    step: { name: "content", verdict: "unknown" },
    layer: { layer: "L5", info: { name: "L5 需登录 / 被墙", action: "先解决登录态" },
             evidence: [], unsure: "" },
  });
  assert.equal(d.fix.label, "配置登录 / 会话");
  assert.match(d.gap.reason, /登录态/);
});

test("候选摆在主动作位：动作种类与规则都照后端给的走", () => {
  const fix = fixView({ kind: "apply_candidate", rule: ".book-list .item@tag.a@href" });
  assert.equal(fix.label, "用候选规则");
  assert.equal(fix.rule, ".book-list .item@tag.a@href");
});

test("规则本地跑不了：取证是唯一动作，按钮词仍带「取材料」", () => {
  const d = buildDecision({
    plan: { layer: "L1", gap: { code: "rule_unsupported" }, deferred_gaps: [],
            fix: null, probe: { kind: "app" },
            ai: { eligible: false, reason_code: "material_mismatch", material_kind: "" } },
    step: { name: "search", verdict: "unknown" },
    layer: L1,
    replay: { rule_error: "@js: 脚本" },
  });
  assert.equal(d.fix, null);
  assert.equal(d.gap.level, "info");
  assert.match(d.gap.reason, /@js: 脚本/);
  assert.equal(d.probe.label, "连 App 取运行时材料");
});

test("取不到计划：渲染现状与证据，并说明接口不可用——不自己再判一遍", () => {
  const d = buildDecision({
    plan: null,
    step: { name: "search", verdict: "fail", reason: "搜索没出结果" },
    layer: L1,
    channel: "jvm",
    page: { origin: "engine" },
  });
  assert.equal(d.gap, null);
  assert.equal(d.fix, null);
  assert.equal(d.probe, null);
  assert.equal(d.plan_ready, false);
  assert.equal(d.ai.reason_code, "no_plan");
  assert.match(d.ai.hint, /接口不可用/);
  assert.equal(d.state.verdictLabel, "失败");
  assert.equal(d.have.find((s) => s.key === "jvm").trust, "authoritative");
});

test("喂给 AI 的现状与首屏同一份（含其余缺口）", () => {
  const d = buildDecision({
    plan: PLAN_RULE_MISSING,
    step: STEP,
    want: { kind: "media", label: "正文图片/音频" },
    layer: L3,
    stats: { links: 7, images: 2, images_with_src: 0 },
  });
  const lines = decisionLines(d);
  assert.equal(lines.length, 3);
  assert.match(lines[0], /没配「正文图片\/音频」规则/);
});
test("取证按钮只有 app / jvm 两种，别的种类不渲染", () => {
  assert.equal(probeView({ kind: "app" }).kind, "app");
  assert.equal(probeView({ kind: "jvm" }).kind, "jvm");
  assert.match(probeView({ kind: "jvm" }).label, /取材料/);
  assert.equal(probeView({ kind: "nope" }), null);
  assert.equal(probeView(null), null);
});

test("默认决策卡带出当前步骤核心值，并截断过长值", () => {
  const d = buildDecision({
    step: { name: "content", verdict: "pass", values: ["正文首段" + "x".repeat(200), "第二条"] },
    layer: L1,
  });
  assert.equal(d.core_values.length, 2);
  assert.equal(d.core_values[0].length, 181);
  assert.equal(d.core_values[0].endsWith("…"), true);
  assert.equal(d.core_value_hint, "");
});

test("没有权威核心值时明确提示未返回", () => {
  const d = buildDecision({ step: { name: "content", verdict: "unknown", values: [] }, layer: L1 });
  assert.deepEqual(d.core_values, []);
  assert.equal(d.core_value_hint, "本次通道未返回核心值");
});
