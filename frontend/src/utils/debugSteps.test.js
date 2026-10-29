import assert from "node:assert/strict";
import test from "node:test";
import { mergedSteps, stepUrlOf } from "./debugSteps.js";

//: 会话结果：这次打开页面期间跑完的那一份（字段最全）
const RESULT = { steps: [
  { name: "search", url: "https://site/search?q=x", verdict: "pass", values: ["◇书籍总数:12"] },
  { name: "toc", url: "https://site/new-toc", verdict: "pass" },
] };

//: 工作区材料：壳从 last-debug 查回来的元数据（刷新后只剩这一份）
const MATERIAL = { steps: [
  { name: "toc", url: "https://site/old-toc", verdict: "fail" },
  { name: "content", url: "https://site/ch/1", verdict: "unknown" },
], pages: [{ page_id: "chapter" }] };

test("刷新后只剩工作区材料时，这一步的地址仍取得到（原本会报「没有链接」）", () => {
  // 刷新 = 会话结果为空，材料还在——这正是「明明有材料却说没有」的场景
  assert.equal(stepUrlOf("toc", { result: null, material: MATERIAL }),
    "https://site/old-toc");
  assert.equal(stepUrlOf("content", { material: MATERIAL }), "https://site/ch/1");
});

test("同名步骤以会话结果为准，不被旧材料顶掉", () => {
  assert.equal(stepUrlOf("toc", { result: RESULT, material: MATERIAL }),
    "https://site/new-toc");
  // 合并只补缺，不重复同名项
  const merged = mergedSteps({ result: RESULT, material: MATERIAL });
  assert.deepEqual(merged.map((s) => s.name), ["search", "toc", "content"]);
});

test("两份都没有这一步时返回空串（调用方据此说清缺什么）", () => {
  assert.equal(stepUrlOf("content", { result: RESULT }), "");
  assert.equal(stepUrlOf("", { result: RESULT, material: MATERIAL }), "");
  assert.equal(stepUrlOf("toc", {}), "");
});