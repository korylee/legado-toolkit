// 在途调试**渲染层**的断言：后端只给相位码与事实（`backend/jobs/runner.active_run_snapshot`），
// 中文句子在这里出。要钉住的是两件事：
//   ① 码 → 词的两张表（排队 / 拉起引擎 / 引擎执行中），以及「谁占着引擎」的说法；
//   ② **交接判据用 phase_ms，不用总已等时长**——前面排了多久的队不影响「拉起引擎」
//      该报多久，用错了会让「拉起中」一出现就立刻跳过去（等于这一档白报）。
import { test } from "node:test";
import assert from "node:assert/strict";
import { debugRunState, PHASE_HANDOFF_MS, RUNNING_LABEL } from "./debugRun.js";

test("等引擎：说自己已等多久，不编进度", () => {
  const s = debugRunState({ phase: "queued", elapsed_ms: 4200, phase_ms: 4200 });
  assert.equal(s.phase, "queued");
  assert.equal(s.elapsedSec, 4);
  assert.equal(s.waiting, true);
  assert.match(s.text, /等待本机引擎/);
  assert.match(s.text, /已等 4 秒/);
  assert.doesNotMatch(s.text, /%|预计|大约/, "不做估算、不报百分比");
});

test("批量校验占着引擎：把它说出来——那才是「没动静」的答案", () => {
  const s = debugRunState({ phase: "queued", elapsed_ms: 65000, lane_holder: "batch" });
  assert.match(s.text, /批量校验/);
  assert.match(s.text, /已等 65 秒/);
});

test("自己占着（另一次调试）与未知持有者都不冒充别的任务", () => {
  assert.match(debugRunState({ phase: "queued", lane_holder: "debug" }).text, /另一次调试/);
  const unknown = debugRunState({ phase: "queued", lane_holder: "whatever" });
  assert.match(unknown.text, /等待本机引擎/);
  assert.doesNotMatch(unknown.text, /whatever/);
});

test("后面还排着队时报出数量", () => {
  const s = debugRunState({ phase: "queued", elapsed_ms: 1000, lane_holder: "batch",
                            lane_waiting: 2 });
  assert.match(s.text, /前面还排着 2 个/);
});

test("刚拿到许可：报「正在拉起引擎」", () => {
  const s = debugRunState({ phase: "starting", elapsed_ms: 800, phase_ms: 200 });
  assert.equal(s.phase, "starting");
  assert.equal(s.waiting, true);
  assert.match(s.text, /正在拉起引擎/);
});

test("交接判据用 phase_ms：排了很久的队也不影响「拉起中」这一档", () => {
  // 排队 300 秒之后刚进入 starting：phase_ms 很小，仍应说「正在拉起引擎」
  const justStarted = debugRunState({ phase: "starting", elapsed_ms: 300_000, phase_ms: 100 });
  assert.match(justStarted.text, /正在拉起引擎/,
    "用总时长判会在这里跳成「引擎执行中」——那正是这一档白报的写法");
  // 拉起确实拖久了（> 门槛）：如实换成「引擎执行中」，不把「拉起」一直挂着当解释
  const stalled = debugRunState({ phase: "starting", elapsed_ms: 300_000,
                                  phase_ms: PHASE_HANDOFF_MS + 1 });
  assert.equal(stalled.phase, "running");
  assert.match(stalled.text, new RegExp(RUNNING_LABEL));
  assert.equal(stalled.waiting, false);
});

test("不在途：照实说已等多久，不谎称失败、也不假装还在等", () => {
  const s = debugRunState({ phase: "" }, 7000);
  assert.equal(s.phase, "");
  assert.equal(s.waiting, false);
  assert.equal(s.text, "已等待 7 秒");
  assert.ok(!/失败|错误/.test(s.text));
});

test("后端快照还没回来：退回本地秒表，不显示「已等 0 秒」", () => {
  const s = debugRunState(null, 3500);
  assert.equal(s.elapsedSec, 3);
  assert.equal(s.text, "已等待 3 秒");
});
