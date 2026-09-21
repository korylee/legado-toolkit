// 任务（jobs）结果里的失败原因——**唯一的一份**。
//
// 为什么要有它：任务失败时后端写的是 `{"error","trace"}`，其中「进程重启，任务没写
// 终态（崩溃或被强杀）」这类原因（见 `Store.fail_orphan_jobs`）是用户唯一能看到的解释。
// 界面只显示 status 字面量（"failed"）等于把原因丢了——同一件事两处各写一遍，还会
// 漂成两种说法。调用方：`SourcesView`（校验任务）与 `SourceEditDialog`（生成任务）。
export function jobFailReason(resultJson) {
  try { return JSON.parse(resultJson || "{}").error || ""; } catch (e) { return ""; }
}
