// 任务（jobs）结果里的失败原因——**唯一的一份**。
//
// 为什么要有它：任务失败时后端写的是 `{"error","trace"}`，其中「进程重启，任务没写
// 终态（崩溃或被强杀）」这类原因（见 `Store.fail_orphan_jobs`）是用户唯一能看到的解释。
// 界面只显示 status 字面量（"failed"）等于把原因丢了——同一件事两处各写一遍，还会
// 漂成两种说法。调用方：`SourcesView`（校验任务）与 `SourceEditDialog`（生成任务）。
export function jobFailReason(resultJson) {
  try { return JSON.parse(resultJson || "{}").error || ""; } catch (e) { return ""; }
}

const PHASE_LABELS = {
  queued: "排队中",
  waiting_readiness: "等待环境检查",
  starting_worker: "启动执行器",
  starting_gradle: "启动 Gradle",
  starting_daemon: "启动校验 daemon",
  configuring: "配置执行参数",
  compiling: "编译中",
  running_validate: "执行校验",
  reading_results: "读取结果",
  saving_results: "写入结果",
  cancel_requested: "正在取消",
  finished: "已结束",
};

export function jobPhaseLabel(phase) {
  return PHASE_LABELS[phase] || phase || "准备中";
}

//: daemon 批前准备的四档结果：后端给的是 outcome 枚举，界面上说人话
export function daemonPrepareLabel(outcome) {
  return {
    ready: "热复用",
    started: "已启动",
    busy: "忙，本批未准备",
    failed: "启动失败",
  }[outcome] || outcome || "";
}
