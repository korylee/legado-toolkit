// AI 补全的状态**在模块作用域**——这一条抓不到编译错误。
//
// 失败形态（2026-10-20 真发生过一次）：状态声明写在 `useAgentRun()` 函数体里，调试
// 工作台与 AI 补全弹窗各调一次就各得一份 ref。构建绿、既有单测绿，跑起来是「工作台
// 提交的那一轮，弹窗里永远看不到」。
//
// 这条测试的力气用在哪：它**直接 import 状态件**并断言导出的是可写的 ref。谁把状态
// 搬回 `useAgentRun` 函数体，这个 import 就先失败——也就是说，`agentRunState.js` 的
// 存在本身成了结构约束，而不是靠约定。
//
// 它**不观察**「两次 useAgentRun() 拿到同一组 ref」：那要加载 useAgentRun 本体，而本
// 仓既有模块之间的 import 不带扩展名（Vite 会补，Node 不补），那条路只能跑一次打包
// ——为一条断言付 700ms 与一个 vite 内部路径依赖，不值。共享由结构保证（状态不在
// 函数里，就没有「各建一份」这回事）。
import assert from "node:assert/strict";
import test from "node:test";

import { isRef } from "vue";

import * as state from "./agentRunState.js";

test("状态件导出的是共享 ref，不是一个新建状态的工厂", () => {
  const keys = [
    "jobId", "running", "jobRow", "fields", "error",
    "summary", "result", "detailError", "step", "debugJobId",
  ];
  for (const key of keys) {
    assert.ok(isRef(state[key]), `${key} 必须是 ref`);
  }
  // 取用者写入的是**同一个** ref：直接改它，再读回来（没有工厂可绕过）
  state.jobId.value = "job-1";
  assert.equal(state.jobId.value, "job-1");
  state.fields.value = ["name"];
  assert.deepEqual(state.fields.value, ["name"]);
  // 收尾复位：模块级状态会在测试进程里留存，别把值漏给后面的用例
  state.jobId.value = "";
  state.fields.value = [];
});
