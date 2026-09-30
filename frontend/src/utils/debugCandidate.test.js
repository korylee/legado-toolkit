import assert from "node:assert/strict";
import test from "node:test";

import {
  candidateExamples,
  candidateIntentLabel,
  candidateKindLabel,
  candidateView,
} from "./debugCandidate.js";

test("四类候选意图使用统一展示词", () => {
  assert.equal(candidateIntentLabel("list"), "列表项");
  assert.equal(candidateIntentLabel("link"), "链接");
  assert.equal(candidateIntentLabel("text"), "正文文本");
  assert.equal(candidateIntentLabel("media"), "媒体地址");
});

test("候选展示模型保留规则、示例和重复数", () => {
  assert.deepEqual(candidateView({
    rule: ".chapter a@href",
    kind: "siblings",
    samples: ["/a", "", "/b", "/c"],
    count: 4,
    uniq: 3,
    why: "同层集合",
  }, { intent: "link" }), {
    intent: "链接",
    kind: "同层集合",
    rule: ".chapter a@href",
    examples: ["/a", "/b", "/c"],
    count: 4,
    unique: 3,
    duplicate: 1,
    status: "pending",
    reason: "同层集合",
  });
});

test("AI 或点选候选缺少样本时不伪造示例", () => {
  assert.deepEqual(candidateExamples({ samples: ["", null] }), []);
  assert.equal(candidateKindLabel("unknown"), "候选规则");
});
