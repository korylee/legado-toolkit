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

//: 任务的执行方式（结果里的 `execution_mode`，取值定义在 backend/jobs/jvm_exec.py）。
//: 「Gradle 回退」是最要紧的一档——它意味着这次没走常驻引擎、比预期慢，
//: 而这正是用户在进度迟迟不动时想知道的。
const EXECUTION_MODE_LABELS = {
  validate_daemon: "常驻引擎",
  gradle_fallback: "Gradle 回退",
  unknown: "未确定",
};

export function executionModeLabel(mode) {
  return EXECUTION_MODE_LABELS[mode] || mode || "";
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

/** 进度条的 `status`：Element Plus 只有 success / exception / warning 三档，
 *  而任务状态有五档——少映射一档是**静默画错**，不报错也不失败：
 *  `cancelled` 落进 `success` 就是一根绿色满条，与头部「已取消」标签自相矛盾。
 *  `unknown` 取中性：它只是「SSE 没拿到终态」，库里那条可能早就跑完了，
 *  画成功和画失败都是编。 */
export function jobProgressStatus(status) {
  if (status === "failed") return "exception";
  if (status === "cancelled") return "warning";
  if (status === "done") return "success";
  return undefined;   // 排队 / 在跑 / unknown：中性
}

/** 「这批校验后变坏了」：目标档在**后端下发的**需要动手档里（需登录 / 需翻墙 /
 *  已失效）。档位名单走 `GET /api/sources/tags/meta`（真源 core/models 的
 *  HEALTH_NEEDS_ACTION），前端不另存一份——多一个档就是少报一批，且不报错。
 *  名单还没到位时返回 false：宁可不标红，也不乱标。 */
export function isWorseHealth(to, needsAction) {
  return (Array.isArray(needsAction) ? needsAction : []).includes(to);
}

/** 后端时间戳是**本地时间字符串**（无时区）。自己按字段解析，不用 Date 解析字符串：
 *  各浏览器对 `YYYY-MM-DD HH:mm:ss` 的解释不一致。解析不了返回 0——
 *  调用方据此不出文案，宁可不说，也不要猜出一个「刚刚」。 */
const STAMP_RE = /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})/;

function stampMs(stamp) {
  const m = STAMP_RE.exec(String(stamp || ""));
  if (!m) return 0;
  const at = new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6]).getTime();
  return Number.isNaN(at) ? 0 : at;
}

function durationText(sec) {
  if (sec < 60) return sec + " 秒";
  const min = Math.floor(sec / 60);
  if (min < 60) return min + " 分 " + (sec % 60) + " 秒";
  return Math.floor(min / 60) + " 小时 " + (min % 60) + " 分";
}

/** 「已经跑了多久」。跑批是分钟级，这是用户最想知道的数，且是**已发生的耗时**，
 *  不是 ETA（编 ETA 在组件注释里是明令禁止的）。时钟对不上（负数）时取 0。 */
export function jobSpentText(createdAt, now = Date.now()) {
  const start = stampMs(createdAt);
  if (!start) return "";
  return durationText(Math.max(0, Math.floor((now - start) / 1000)));
}

/** 「多久以前更新过」：进度停住时这个数会一直涨，它就是「还在推进吗」的判据。 */
export function jobAgoText(stamp, now = Date.now()) {
  const at = stampMs(stamp);
  if (!at) return "";
  const sec = Math.max(0, Math.floor((now - at) / 1000));
  return sec < 5 ? "刚刚" : durationText(sec) + "前";
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
