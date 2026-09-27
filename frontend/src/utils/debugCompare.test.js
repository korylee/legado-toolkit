// debugCompare 的断言（ux-debug-loop）：重跑不清场、对比键是（段名, URL）、
// 「新失败」单独成档、错误体不许覆盖对比基线。倒着写会让「重跑清空结果」
// 或「换个 URL 就报值变了」复活。

import assert from "node:assert/strict";
import test from "node:test";

import {
  RUN_HISTORY_MAX,
  compareRuns,
  nextCompareState,
  slimRunResult,
  statusLabel,
} from "./debugCompare.js";

function step(name, { url = "https://a.com/x", verdict = "pass", detail = "ok" } = {}) {
  return { name, url, verdict, detail };
}

test("URL 与结论都没变 → same", () => {
  const prev = slimRunResult({ steps: [step("search")] });
  const cur = { steps: [step("search")] };
  assert.deepEqual(compareRuns(prev, cur).map((d) => d.status), ["same"]);
});

test("verdict 或 detail 变了 → changed；pass 掉成 fail → 新失败", () => {
  const prev = slimRunResult({
    steps: [step("content"), step("toc", { url: "https://a.com/t" }),
            step("search", { verdict: "unknown", detail: "没验" })],
  });
  const cur = {
    steps: [
      step("content", { verdict: "fail", detail: "取不到正文" }),
      step("toc", { url: "https://a.com/t", verdict: "pass", detail: "目录总数:11" }),
      step("search", { verdict: "fail", detail: "坏了" }),
    ],
  };
  const got = Object.fromEntries(compareRuns(prev, cur).map((d) => [d.name, d.status]));
  assert.equal(got.content, "regressed", "pass → fail 才是「新失败」");
  assert.equal(got.toc, "changed");
  assert.equal(got.search, "changed", "unknown → fail 不算退步：没验过谈不上退");
  assert.equal(statusLabel("regressed"), "新失败");
});

test("对比键是（段名, URL）：URL 换了标 moved，不与值变了混", () => {
  const prev = slimRunResult({ steps: [step("content")] });
  const cur = { steps: [step("content", { url: "https://a.com/y", verdict: "fail", detail: "坏" })] };
  assert.deepEqual(compareRuns(prev, cur).map((d) => d.status), ["moved"]);
});

test("上一份没有的段 → new", () => {
  const prev = slimRunResult({ steps: [step("search")] });
  const cur = { steps: [step("search"), step("content")] };
  assert.deepEqual(compareRuns(prev, cur).map((d) => d.status), ["same", "new"]);
});

test("cur 没有 steps（纯错误体）：基线不动，最后的真实结果还在", () => {
  const first = { steps: [step("search")] };
  let state = nextCompareState(null, null, first);
  assert.equal(state.prev, null, "第一份没有「上次」");
  state = nextCompareState(state, first, { steps: [step("search", { detail: "第2次" })] });
  assert.equal(state.prev.steps[0].detail, "ok");
  const afterError = nextCompareState(state, { error: "网络断了" }, { error: "又断了" });
  assert.deepEqual(afterError, state, "错误体不覆盖基线");
});

test("old 有 steps 时它成为对比基线并进历史，历史封顶", () => {
  let state = { prev: null, history: [] };
  for (let i = 0; i < RUN_HISTORY_MAX + 2; i++) {
    state = nextCompareState(state,
                             { steps: [step("search", { detail: "第" + i + "次" })] },
                             { steps: [step("search")] });
  }
  assert.equal(state.history.length, RUN_HISTORY_MAX);
  assert.equal(state.prev.steps[0].detail, "第" + (RUN_HISTORY_MAX + 1) + "次");
});

test("slimRunResult 只留摘要字段", () => {
  const slim = slimRunResult({
    source: "jvm",
    steps: [{ name: "toc", url: "u", verdict: "pass", detail: "d",
              values: ["一大段"], matched_html: "<div>" }],
  });
  assert.deepEqual(Object.keys(slim.steps[0]).sort(),
                   ["detail", "name", "reason", "url", "verdict"]);
});
