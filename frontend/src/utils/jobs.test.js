// 任务状态的界面判据（status 词表、终态、在跑、取消后果）。
//
// 这些判据原来散在 `JobsDrawer` 里，改版后多了明细弹窗与列表页在跑条两个读者——
// 三处各判一次必然漂。这里钉住两条容易改错的：
//   ① `unknown` 必须算终态，否则那条任务在界面上永远清不掉；
//   ② 取消的后果说明要按任务类型分开——跑批是块级中止，说成「立刻停」等于让人以为按钮坏了。
import test from "node:test";
import assert from "node:assert/strict";
import {
  executionModeLabel,
  isWorseHealth,
  jobAgoText,
  jobCancelHint,
  jobIsInFlight,
  jobIsTerminal,
  jobKindLabel,
  jobProgressStatus,
  jobSpentText,
  jobStatusLabel,
  jobStatusType,
} from "./jobs.js";

test("终态包含 unknown，否则那条任务在界面上永远清不掉", () => {
  for (const status of ["done", "failed", "cancelled", "unknown"]) {
    assert.equal(jobIsTerminal(status), true, status + " 应算终态");
  }
  for (const status of ["pending", "running", "cancel_requested"]) {
    assert.equal(jobIsTerminal(status), false, status + " 不该算终态");
  }
});

test("在跑与终态互补，没有状态落在两者之外", () => {
  const all = ["pending", "running", "cancel_requested", "done", "failed",
               "cancelled", "unknown"];
  for (const status of all) {
    assert.notEqual(jobIsInFlight(status), jobIsTerminal(status),
                    status + " 只能属于一边");
  }
});

test("跑批的取消说明是块级中止，不能写成「立刻停止」", () => {
  const hint = jobCancelHint("jvm_run");
  assert.match(hint, /跑完/);          // 当前块会跑完
  assert.match(hint, /不再启动/);       // 之后的块不再启动
  assert.doesNotMatch(hint, /不会继续推进/);
});

test("其他类型的取消说明不借用跑批那套说法", () => {
  const hint = jobCancelHint("add");
  assert.match(hint, /不会继续推进/);
  assert.doesNotMatch(hint, /Gradle/);
});

test("状态与类型都有中文词，未知值原样透出不伪装", () => {
  assert.equal(jobStatusLabel("running"), "运行中");
  assert.equal(jobKindLabel("jvm_run"), "本机引擎校验");
  assert.equal(jobStatusLabel("weird"), "weird");
  assert.equal(jobKindLabel("weird"), "weird");
  assert.equal(jobStatusType("done"), "success");
  assert.equal(jobStatusType("failed"), "danger");
});

test("进度条状态：取消不许画成成功，unknown 不猜", () => {
  assert.equal(jobProgressStatus("done"), "success");
  assert.equal(jobProgressStatus("failed"), "exception");
  assert.equal(jobProgressStatus("cancelled"), "warning");
  for (const status of ["pending", "running", "cancel_requested", "unknown"]) {
    assert.equal(jobProgressStatus(status), undefined, status + " 应取中性档");
  }
});

test("耗时只报已发生的数：起点缺失或时钟对不上都不编", () => {
  const t0 = "2026-10-05 10:00:00";
  const at = (sec) => new Date(2026, 9, 5, 10, 0, sec).getTime();
  assert.equal(jobSpentText(t0, at(12)), "12 秒");
  assert.equal(jobSpentText(t0, at(192)), "3 分 12 秒");
  assert.equal(jobSpentText(t0, at(3720)), "1 小时 2 分");
  assert.equal(jobSpentText("", at(12)), "");
  assert.equal(jobSpentText("不是时间", at(12)), "");
  assert.equal(jobSpentText(t0, at(-30)), "0 秒");
});

test("多久以前更新过：进度停住时这个数会一直涨", () => {
  const t0 = "2026-10-05 10:00:00";
  const at = (sec) => new Date(2026, 9, 5, 10, 0, sec).getTime();
  assert.equal(jobAgoText(t0, at(3)), "刚刚");
  assert.equal(jobAgoText(t0, at(45)), "45 秒前");
  assert.equal(jobAgoText(t0, at(200)), "3 分 20 秒前");
  assert.equal(jobAgoText("", at(45)), "");
});

test("变坏只看后端下发的档位名单，名单没到位时不标红", () => {
  const needs = ["auth", "gfw", "dead"];
  assert.equal(isWorseHealth("dead", needs), true);
  assert.equal(isWorseHealth("auth", needs), true);
  assert.equal(isWorseHealth("ok", needs), false);
  assert.equal(isWorseHealth("pending", needs), false);   // 没结论不算坏
  assert.equal(isWorseHealth("dead", []), false);          // meta 还没回来
  assert.equal(isWorseHealth("dead", undefined), false);
});

test("执行方式有中文词，未知值原样透出不伪装", () => {
  assert.equal(executionModeLabel("validate_daemon"), "常驻引擎");
  assert.equal(executionModeLabel("gradle_fallback"), "Gradle 回退");
  assert.equal(executionModeLabel("unknown"), "未确定");
  assert.equal(executionModeLabel("mystery"), "mystery");
  assert.equal(executionModeLabel(""), "");
});
