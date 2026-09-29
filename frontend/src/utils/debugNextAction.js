// 调试结果的下一步动作：把 verdict 与当前通道收敛成一个用户可执行动作。
// 这里只决定动作，不重算规则结论；动态层的真机出口仍由 debugOutlets 负责。
import { debugOutletFor } from "./debugOutlets.js";

const ACTIONS = {
  fail: { kind: "workbench", label: "查看诊断" },
  unknown: { kind: "workbench", label: "打开调试工作台" },
};

/**
 * @param {Object} step
 * @param {string} channel jvm / app
 * @param {string} layer L1-L5 / ""
 * @param {boolean} stale 规则变更后结论是否过期
 * @param {boolean} inWorkbench 当前是否已经在调试工作台
 * @returns {{kind: string, label: string} | null}
 */
export function nextDebugAction(step, {
  channel = "", layer = "", stale = false, inWorkbench = false,
} = {}) {
  if (!step) return null;
  if (stale) return { kind: "rerun", label: "重新调试本步" };
  const verdict = step.verdict || (step.ok === false ? "fail" : "");
  if (verdict === "pass") return step.has_notes
    ? { kind: "workbench", label: "查看疑点" }
    : null;
  if (verdict === "unknown") {
    return debugOutletFor({ channel, verdict, layer })
      || (inWorkbench ? { kind: "diagnosis", label: "查看诊断" } : ACTIONS.unknown);
  }
  return ACTIONS[verdict] || null;
}

export function firstNextDebugAction(steps, options = {}) {
  for (const step of steps || []) {
    const action = nextDebugAction(step, options);
    if (action) return { step, action };
  }
  return null;
}
