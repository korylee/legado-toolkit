// 任务明细的**分派**：同一个任务号，按 kind 决定开哪个弹窗。
//
// 为什么要有这个薄件：AI 补全（`agent_loop`）有自己的明细弹窗（`AgentRunDialog`），
// 校验与调试走 `TaskDetailDialog`。两个调用方（任务中心、列表页的结果条）各写一遍
// `kind === "agent_loop" ? … : …`，迟早有一处漏判——漏判的后果不是报错，是那次补全
// 被塞进校验骨架，显示出「这次任务没有可展示的结果」。
//
// 没有用 `utils/jobs.js` 的 `jobCategoryOf` 做判据：那里是**任务中心的页签**分类
// （校验/调试/AI），这里是**渲染器**的选择——将来新增 kind 落进「其他」页签时，
// 它该用哪个弹窗是另一件事，不该被页签分类绑死。
const AGENT_JOB_KIND = "agent_loop";

/** 这个任务该用 AI 补全弹窗吗？ */
function usesAgentDialog(kind) {
  return String(kind || "") === AGENT_JOB_KIND;
}

/** 打开一条任务的明细。
 *
 * `detailRef` = TaskDetailDialog 的 ref（暴露 `open`）；
 * `agentRef` = AgentRunDialog 的 ref（暴露 `openJob`）。
 * 两个 ref 都可能还没挂载（`v-if` / 抽屉懒挂）：拿不到就什么都不做，不抛。
 */
export function openJobDetail(kind, jobId, { detailRef, agentRef } = {}) {
  if (!jobId) return;
  if (usesAgentDialog(kind)) {
    const dialog = typeof agentRef === "function" ? agentRef() : agentRef;
    if (dialog && dialog.value && typeof dialog.value.openJob === "function") {
      dialog.value.openJob(jobId);
    }
    return;
  }
  const dialog = typeof detailRef === "function" ? detailRef() : detailRef;
  if (dialog && dialog.value && typeof dialog.value.open === "function") {
    dialog.value.open(jobId);
  }
}
