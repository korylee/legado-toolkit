import test from "node:test";
import assert from "node:assert/strict";
import { formatTimelineEvent, formatFailureLine, groupTimelineLines } from "./jobTimeline.js";

test("里程碑与块事件说人话，并展示启动耗时", () => {
  assert.equal(
    formatTimelineEvent({ kind: "batch_started", chunks: 3, sources: 75 }).text,
    "开始校验：75 条源，分 3 块");
  assert.equal(
    formatTimelineEvent({ kind: "chunk_started", index: 2, count: 25, mode: "gradle" }).text,
    "第 3 块 开始（25 条）");
  assert.equal(
    formatTimelineEvent({ kind: "chunk_done", index: 2, count: 25, cost_sec: 17.2, mode: "validate_daemon" }).text,
    "第 3 块 完成：25 条 · 17.2 秒 · 常驻引擎");
  assert.equal(
    formatTimelineEvent({ kind: "done", count: 75, cost_sec: 600 }).text,
    "校验完成：共 75 条 · 600 秒");
  assert.equal(
    formatTimelineEvent({ kind: "resumed", index: 0 }).text,
    "第 1 块 上次已完成，本次不重跑");
});

test("启动阶段事件展示耗时和失败原因", () => {
  assert.equal(
    formatTimelineEvent({ kind: "startup_stage", stage: "queue_wait", status: "done", cost_sec: 1.2 }).text,
    "任务排队完成（1.2 秒）");
  assert.equal(
    formatTimelineEvent({ kind: "startup_stage", stage: "daemon_request", status: "done", cost_sec: 17.8 }).text,
    "常驻引擎执行完成（17.8 秒）");
  assert.equal(
    formatTimelineEvent({ kind: "startup_stage", stage: "mystery_stage" }).text,
    "mystery_stage完成");
  const failed = formatTimelineEvent({ kind: "startup_stage", stage: "daemon_request", status: "failed", cost_sec: 2.4, reason: "daemon 忙" });
  assert.equal(failed.tone, "error");
  assert.equal(failed.text, "常驻引擎执行失败（2.4 秒）：daemon 忙");
  const waiting = formatTimelineEvent({ kind: "waiting_engine", elapsed_sec: 12.5 });
  assert.equal(waiting.text, "等待校验引擎空闲：已等 12.5 秒");
  assert.equal(waiting.tone, "warn");
});
test("prepare 四档各自有措辞，忙/失败带原因", () => {
  assert.equal(formatTimelineEvent({ kind: "prepare", outcome: "ready" }).text, "校验引擎已就绪（热复用）");
  assert.equal(
    formatTimelineEvent({ kind: "prepare", outcome: "started", cost_sec: 11.2 }).text,
    "校验引擎已就绪（首次启动 11.2 秒）");
  const busy = formatTimelineEvent({ kind: "prepare", outcome: "busy", reason: "进程还在但 ping 没应答" });
  assert.equal(busy.tone, "warn");
  assert.match(busy.text, /校验引擎忙/);
  const failed = formatTimelineEvent({ kind: "prepare", outcome: "failed", reason: "180s 内没起来" });
  assert.equal(failed.tone, "error");
  assert.match(failed.text, /启动失败/);
});

test("失败/取消是 error/warn，未知 kind 原样透出不伪装", () => {
  assert.equal(formatTimelineEvent({ kind: "chunk_failed", index: 6, reason: "Gradle 失败" }).tone, "error");
  assert.equal(formatTimelineEvent({ kind: "cancelled" }).tone, "warn");
  assert.equal(formatTimelineEvent({ kind: "mystery" }).text, "mystery");
});

test("失败源一行：名字优先、理由收尾", () => {
  assert.deepEqual(
    formatFailureLine({ name: "甲", url: "https://a.com", state: "no_result", reason: "搜索为空" }),
    { title: "甲", detail: "（no_result）搜索为空" });
  assert.deepEqual(
    formatFailureLine({ name: "", url: "https://b.com", state: "error", reason: "连不上" }).title,
    "https://b.com");
});

test("事件流按块归组：块首不缩进、块内后续缩进", () => {
  const grouped = groupTimelineLines([
    { seq: 1, kind: "batch_started", text: "开始校验", tone: "info" },
    { seq: 2, kind: "chunk_started", index: 0, text: "第 1 块 开始", tone: "info" },
    { seq: 3, kind: "chunk_done", index: 0, text: "第 1 块 完成", tone: "info" },
    { seq: 4, kind: "chunk_started", index: 1, text: "第 2 块 开始", tone: "info" },
    { seq: 5, kind: "done", text: "校验完成", tone: "info" },
  ]);
  assert.deepEqual(grouped.map((g) => g.depth), [0, 0, 1, 0, 0]);
  assert.deepEqual(grouped.map((g) => g.chunk), [false, true, true, true, false]);
  assert.equal(grouped[2].line.text, "第 1 块 完成");
});

test("同一块被顶层事件打断后仍算同一块", () => {
  const grouped = groupTimelineLines([
    { seq: 1, kind: "chunk_started", index: 3, text: "第 4 块 开始", tone: "info" },
    { seq: 2, kind: "waiting_engine", text: "等待校验引擎空闲", tone: "warn" },
    { seq: 3, kind: "chunk_done", index: 3, text: "第 4 块 完成", tone: "info" },
  ]);
  assert.deepEqual(grouped.map((g) => g.depth), [0, 0, 1]);
  assert.equal(grouped[1].chunk, false);
});

test("归组对空输入与脏输入不抛", () => {
  assert.deepEqual(groupTimelineLines([]), []);
  assert.deepEqual(groupTimelineLines(null), []);
  assert.deepEqual(groupTimelineLines([null, undefined]), []);
});
