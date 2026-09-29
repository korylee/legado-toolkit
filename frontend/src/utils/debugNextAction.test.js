import assert from "node:assert/strict";
import test from "node:test";

import { firstNextDebugAction, nextDebugAction } from "./debugNextAction.js";

test("unknown 动态层给出连 App 调试出口", () => {
  const result = nextDebugAction(
    { name: "content", verdict: "unknown" },
    { channel: "jvm", layer: "L3" },
  );
  assert.deepEqual(result, {
    kind: "app",
    label: "连 App 调试（真机复查）",
    reason: "这一步的数据要渲染、解密或执行脚本后才有，本机引擎取不到",
  });
});

test("unknown 没有层级证据时仍给工作台入口", () => {
  assert.deepEqual(
    nextDebugAction({ verdict: "unknown" }, { channel: "jvm" }),
    { kind: "workbench", label: "打开调试工作台" },
  );
});

test("fail、过期和带附注的通过各有动作", () => {
  assert.deepEqual(nextDebugAction({ verdict: "fail" }),
    { kind: "workbench", label: "查看诊断" });
  assert.deepEqual(nextDebugAction({ verdict: "pass" }, { stale: true }),
    { kind: "rerun", label: "重新调试本步" });
  assert.deepEqual(nextDebugAction({ verdict: "pass", has_notes: true }),
    { kind: "workbench", label: "查看疑点" });
});

test("汇总动作带回第一个需要处理的步骤", () => {
  const result = firstNextDebugAction([
    { name: "search", verdict: "pass" },
    { name: "content", verdict: "unknown" },
  ]);
  assert.equal(result.step.name, "content");
  assert.equal(result.action.label, "打开调试工作台");
});
