// AI 补全事实 → 界面词与逐字段四态（`utils/agentRun`，唯一一份）。
//
// 只钉**错了也看不出来**的三条：① 事件里没有的字段不许被推成 accepted（「没失败」不
// 等于「补上了」，界面上是假成功）；② 流接晚了只剩结果体时，四态要与事件那条路一致；
// ③ `ai_*` 的过程行必须有词——落 default 就是往界面上打 `ai_try` 这样的裸码。
// 访问器（字段名映射、预算注记）与「再补范围」不测：前者是查表，后者是一个 filter，
// 判据与四态共用一条，测了也只是复述实现（AGENTS #26）。
import assert from "node:assert/strict";
import test from "node:test";

import {
  agentEventLine,
  foldAgentFields,
  resultAgentFields,
} from "./agentRun.js";

const FIELDS = ["name", "author", "cover_url"];

test("逐字段四态：没被事件提到的字段算 pending，不算 accepted", () => {
  const rows = foldAgentFields([
    { kind: "ai_started", step: "bookUrl", fields: [] },
    { kind: "ai_try", field: "name", rule: "h1@text", ok: false, error: "没选中" },
    { kind: "ai_try", field: "name", rule: "h2@text", ok: true, count: 1 },
    { kind: "ai_accepted", field: "name", rule: "h2@text", count: 1 },
    { kind: "ai_failed", field: "author", reason: "模型没有给出该字段的候选" },
  ], FIELDS);

  assert.deepEqual(rows.map((r) => [r.field, r.state]), [
    ["name", "accepted"],
    ["author", "failed"],
    // 一个事件都没提到：照实 pending，绝不默认 accepted
    ["cover_url", "pending"],
  ]);
  assert.equal(rows[0].tries, 2);
  assert.equal(rows[0].count, 1);
  assert.equal(rows[1].error, "模型没有给出该字段的候选");
});

test("结果体兜底：验过的算 accepted，只有原因的算 failed", () => {
  const rows = resultAgentFields({
    fields: [
      // 事件漏了，但结果体里这条有 rule + try_result.ok
      { field: "name", rule: "h1@text", try_result: { ok: true, count: 2 } },
      // 提议全不中：只留原因，不出规则文本
      { field: "author", reason: "模型提议了 2 条，轻量验证都没选中", try_result: null },
      // 有 rule 但自查没通过：不许算 accepted
      { field: "cover_url", rule: "img@src", try_result: { ok: false, error: "0 条" } },
    ],
  }, FIELDS);

  assert.equal(rows[0].state, "accepted");
  assert.equal(rows[0].rule, "h1@text");
  assert.equal(rows[0].count, 2);
  assert.equal(rows[1].state, "failed");
  assert.match(rows[1].error, /轻量验证都没选中/);
  assert.equal(rows[2].state, "failed");
});

test("快验测不了：规则落了但标待引擎验收，不冒充已写入", () => {
  // 材料是脚本输出、规则是选择器：快验答不了（core.rule_try.unfaithful_reason）。
  // 落成 accepted 会让界面说「已写入」，而它一次都没被引擎验过（D1）。
  const events = foldAgentFields([
    { kind: "ai_accepted", field: "name", rule: "h1@text", pending_engine: true,
      reason_code: "material_is_script" },
  ], ["name"]);
  assert.equal(events[0].state, "pending_engine");
  assert.equal(events[0].rule, "h1@text");

  const body = resultAgentFields({ fields: [
    { field: "content", rule: "@js:result", pending_engine: true,
      try_result: { unfaithful: true, reason_code: "result_absent" } },
  ] }, ["content"]);
  assert.equal(body[0].state, "pending_engine");
  assert.equal(body[0].rule, "@js:result");
});

test("过程行取词：ai_* 都有词，失败原因原样带出，没有裸码", () => {
  const started = agentEventLine({ kind: "ai_started", step: "bookUrl", fields: ["name"] });
  assert.match(started.text, /AI 补全开始（详情）：书名/);

  const family = agentEventLine({ kind: "ai_family", fields: ["name", "author"] });
  assert.match(family.text, /正在提议：书名、作者/);

  const familyFailed = agentEventLine({ kind: "ai_family_failed", reason: "模型没配" });
  assert.equal(familyFailed.tone, "error");
  assert.match(familyFailed.text, /模型没配/);

  const tried = agentEventLine({ kind: "ai_try", field: "name", ok: false, error: "0 条" });
  assert.equal(tried.tone, "warn");
  assert.match(tried.text, /书名：这条规则取不到（0 条）/);

  const failed = agentEventLine({ kind: "ai_failed", field: "intro", reason: "没有候选" });
  assert.equal(failed.tone, "error");
  assert.match(failed.text, /简介：没有可用的提议（没有候选）/);

  const done = agentEventLine({ kind: "ai_done", written: 2, model_calls: 1, rule_tries: 3 });
  assert.match(done.text, /写入 2 条 · 模型 1 次 · 验证 3 次/);

  // 认不出的码原样透出，不伪装成已知项（同 jobTimeline 的纪律）
  assert.deepEqual(agentEventLine({ kind: "ai_mystery" }), { text: "ai_mystery", tone: "info" });
});
