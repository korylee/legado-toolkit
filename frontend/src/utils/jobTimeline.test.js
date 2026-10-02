import test from "node:test";
import assert from "node:assert/strict";
import { formatTimelineEvent, formatFailureLine } from "./jobTimeline.js";

test("里程碑与块事件说人话，不带引擎术语", () => {
  assert.equal(
    formatTimelineEvent({ kind: "batch_started", chunks: 3, sources: 75 }).text,
    "开始校验：75 条源，分 3 块");
  assert.equal(
    formatTimelineEvent({ kind: "chunk_started", index: 2, count: 25 }).text,
    "第 3 块 开始（25 条）");
  assert.equal(
    formatTimelineEvent({ kind: "chunk_done", index: 2, count: 25, cost_sec: 17.2 }).text,
    "第 3 块 完成：25 条 · 17.2 秒");
  assert.equal(
    formatTimelineEvent({ kind: "done", count: 75, cost_sec: 600 }).text,
    "校验完成：共 75 条 · 600 秒");
  assert.equal(
    formatTimelineEvent({ kind: "resumed", index: 0 }).text,
    "第 1 块 上次已完成，本次不重跑");
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
