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
  preparing_engine: "准备校验引擎",
  starting_daemon: "启动校验 daemon",
  configuring: "配置执行参数",
  compiling: "编译中",
  running_validate: "执行校验",
  isolating_stall: "隔离重跑慢源",
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

//: 任务状态与类型（**唯一一份**）：任务中心列表、明细弹窗、列表页在跑条都读这里。
//: 后端只出状态字面量与 kind 枚举，中文句子在这一层按码取词。
const STATUS_LABELS = {
  pending: "排队中",
  running: "运行中",
  cancel_requested: "取消中",
  done: "已完成",
  failed: "失败",
  cancelled: "已取消",
  unknown: "状态未知",
};

const KIND_LABELS = {
  check: "校验",
  jvm_run: "本机引擎校验",
  add: "添加书源",
  ping: "连通性检查",
};

export function jobStatusLabel(status) {
  return STATUS_LABELS[status] || status || "未知";
}

export function jobStatusType(status) {
  return status === "done" ? "success" : status === "failed" ? "danger" : "warning";
}

/** 终态：不会再变的状态（删除、重试只对这些开放）。
 *
 *  `unknown` 也算终态：它是 SSE 重连到上限仍没拿到终态时的兜底，而库里那条很可能
 *  早就跑完了。不把它算进来的话，这条任务在界面上**永远清不掉、也重试不了**。 */
export function jobIsTerminal(status) {
  return ["done", "failed", "cancelled", "unknown"].includes(status);
}

/** 在跑：还占着执行槽的状态。「进行中」的档位口径在后端（`_STATUS_SCOPES`），
 *  这里只回答单个任务的字面量问题——两处判的东西不同，不是同一件事。 */
export function jobIsInFlight(status) {
  return ["pending", "running", "cancel_requested"].includes(status);
}

/** 取消的后果说明（**唯一一份**：任务中心的行内取消与明细弹窗共用）。
 *
 *  jvm_run 是**块级中止**：当前块在跑 Gradle（`subprocess.run`，同步子进程，取消信号
 *  进不去），后端等它收尾后才停后续块（`jvm_exec` 里 `cancelled` 的检查点在块之间）。
 *  不写清这点，用户点完取消看到进度还在动，只会以为按钮坏了。 */
export function jobCancelHint(kind) {
  return kind === "jvm_run"
    ? "正在跑的这一块会跑完（Gradle 子进程不能中途打断），之后的块不再启动；已产出的结果保留。"
    : "取消后任务不会继续推进，已产生的结果会保留。";
}

export function jobKindLabel(kind) {
  return KIND_LABELS[kind] || kind || "任务";
}
