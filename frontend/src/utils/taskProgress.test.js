// 四段状态判定（`utils/taskProgress`）。
//
// 只钉**错了也看不出来**的那条：跑完后最后一段不能停在「等待」。这是 `progress`
// 字段的两态语义造成的（0 基「正在跑第几段」，跑完不递进到段数），照它画就恒等待。
// 其余（颜色映射、文案、各态之间怎么推）是查表，测了只是复述实现。
import assert from "node:assert/strict";
import test from "node:test";

import { stageStates } from "./taskProgress.js";

const NAMES = ["search", "bookUrl", "toc", "content"];
const OK_ALL = NAMES.map((stage) => ({ stage, ok: true }));

test("跑完后以结果体为准：最后一段不能显示「等待」", () => {
  // 真实数据：任务 done，progress 停在下标 3（不是 4）
  const got = stageStates(NAMES, { running: false, progress: 3, finalStages: OK_ALL });
  assert.deepEqual(got.map((s) => s.state), ["ok", "ok", "ok", "ok"]);
});

test("准备引擎时四段全等：那时第 1 段还没开始，不能显示进行中", () => {
  const got = stageStates(NAMES, { running: true, progress: 0, preparing: true });
  assert.deepEqual(got.map((s) => s.state), ["idle", "idle", "idle", "idle"]);
});