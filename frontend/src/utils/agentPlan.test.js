// 「下一步做什么」消费侧的两件事：按缺口码取词、把事实拼成请求体。

import assert from "node:assert/strict";
import test from "node:test";
import { gapLine, planSnapshot } from "./agentPlan.js";

test("gapLine speaks one sentence per gap code, unknown codes show raw", () => {
  // 渲染这一档要把**要加的那串**写出来——写成「加 webView」用户不知道写在哪儿
  const line = gapLine({ gap: { code: "render_missing" } });
  assert.equal(line.code, "render_missing");
  assert.match(line.text, /\{"webView":true\}/);
  // 没有缺口 → 不说话
  assert.equal(gapLine({ gap: null }), null);
  assert.equal(gapLine({}), null);
  // 认不出的码原样显示：藏起来就没人知道对端多了一个码
  assert.equal(gapLine({ gap: { code: "x_new" } }).text, "x_new");
});

test("planSnapshot only carries facts it actually has", () => {
  const source = { ruleContent: {} };  // 正文规则为空
  const step = { name: "content", verdict: "fail", page_id: "p1",
                 url: "https://a.com/c/1", detail: "内容为空" };
  // 读不到渲染事实的页面：**不带** rendered 这个键（带了会凭空多一档结论）
  const body = planSnapshot({ source, step, page: { html: "x" }, layer: "L2", channel: "jvm" });
  assert.equal(body.target.step, "content");
  assert.equal(body.target.rule_empty, true, "规则空是全局前置，必须带出去");
  assert.equal(body.page.rendered, undefined);
  // 有事实才带
  const withFacts = planSnapshot({
    source, step, layer: "L2", channel: "jvm",
    page: { rendered: false, render_reason: "", page_layer: { has_wanted: false } },
  });
  assert.equal(withFacts.page.rendered, false);
  assert.equal(withFacts.page.has_wanted, false);
  assert.equal(withFacts.page.render_reason, undefined, "空原因不带（空串不是事实）");
  // 换到非正文步：不算 rule_empty（其它步的规则是组规则，不是这一个字段）
  const search = planSnapshot({
    source, step: { name: "search", verdict: "ok" }, page: null, layer: "L1", channel: "jvm",
  });
  assert.equal(search.target.rule_empty, undefined);
  assert.equal(search.page.present, false);
  // 没有步骤就没有判据可问
  assert.equal(planSnapshot({}), null);
});
