// 运行观测帧的合并规则（utils/runStream.js）——守「重连不重复」「游标只前进」。
import assert from "node:assert/strict";
import test from "node:test";

import { asRunStatus, emptyRun, mergeFrame } from "./runStream.js";

test("增量帧按行号累加", () => {
  let state = emptyRun();
  state = mergeFrame(state, {
    id: "j1", status: "running", phase: "running_validate", progress: 25, total: 50,
    cursor: 2, done: false,
    events: [{ seq: 1, kind: "batch_started" }, { seq: 2, kind: "chunk_started" }],
    failures: { total: 0, cursor: 0, items: [] },
  });
  state = mergeFrame(state, {
    id: "j1", status: "running", progress: 50, cursor: 3, done: true,
    events: [{ seq: 3, kind: "done" }],
    failures: { total: 1, cursor: 1, truncated: false,
                items: [{ seq: 1, url: "https://b.com", state: "error" }] },
  });
  assert.deepEqual(state.events.map((e) => e.kind), ["batch_started", "chunk_started", "done"]);
  assert.deepEqual(state.failures.map((f) => f.url), ["https://b.com"]);
  assert.equal(state.failuresTotal, 1);
  assert.equal(state.job.progress, 50);
  assert.ok(state.done);
});

test("重连重发同一段：按 seq 去重，不重复渲染", () => {
  const frame = {
    id: "j1", status: "running", cursor: 2, done: false,
    events: [{ seq: 1, kind: "batch_started" }, { seq: 2, kind: "chunk_started" }],
    failures: { total: 1, cursor: 1, items: [{ seq: 1, url: "https://b.com" }] },
  };
  let state = mergeFrame(emptyRun(), frame);
  state = mergeFrame(state, frame);   // 服务端从原游标重发
  assert.equal(state.events.length, 2);
  assert.equal(state.failures.length, 1);
});

test("游标只前进：迟到的小游标帧不改写已读位置", () => {
  let state = mergeFrame(emptyRun(), {
    id: "j1", status: "running", cursor: 9, done: false,
    events: [{ seq: 9, kind: "chunk_done" }],
    failures: { total: 0, cursor: 0, items: [] },
  });
  state = mergeFrame(state, { id: "j1", status: "running", cursor: 3, done: false,
                              events: [], failures: { total: 0, cursor: 0, items: [] } });
  assert.equal(state.eventSeq, 9);
  assert.equal(state.events.length, 1);
});

test("任务行只取白名单键，不把通道字段混进来", () => {
  const state = mergeFrame(emptyRun(), {
    id: "j1", kind: "jvm_run", status: "running", phase: "queued",
    cursor: 5, done: false, events: [], failures: { total: 0, cursor: 0, items: [] },
    lane_holder: "debug", lane_waiting: 2, elapsed_ms: 1200, phase_ms: 300,
  });
  assert.equal(state.job.lane_holder, "debug");
  assert.equal(state.job.cursor, undefined);
  assert.deepEqual(asRunStatus(state.job), {
    phase: "queued", elapsed_ms: 1200, phase_ms: 300,
    lane_holder: "debug", lane_waiting: 2,
  });
});
