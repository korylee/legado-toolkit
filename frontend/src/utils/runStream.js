// 运行观测帧的合并规则（**纯件**，与 `backend/api/job_timeline.run_frame` 同一条契约）。
//
// 为什么要单列：帧是**增量**的（事件与失败清单都按行号游标只给新行），而 SSE 会自己
// 重连、重连后服务端**从原来的游标重发**——不按行号去重，同一行就会渲染两遍，
// 而时间线上看起来像真跑了两遍。去重判据是行号（seq），不是内容相等。

/** 一个任务的观测状态：帧合并后的全部界面事实。 */
export function emptyRun() {
  return {
    job: null,          // 轻量任务行（status/phase/progress/total/summary/error…）
    events: [],         // 时间线（按 seq 升序、已去重）
    failures: [],       // 失败清单（同上）
    failuresTotal: 0,   // 精确总数（清单可能被 cap 截断）
    failuresTruncated: false,
    eventSeq: 0,
    failureSeq: 0,
    done: false,
  };
}

//: 帧里属于「任务行」的键。**白名单而不是整帧拷贝**：帧上还有 events/failures/cursor
//: 这些通道状态，混进任务行之后，"这个任务是什么状态"就得在两处判断。
const JOB_KEYS = [
  "id", "kind", "status", "phase", "progress", "total", "retry_of", "expires_at",
  "created_at", "updated_at", "error", "summary",
  // 引擎争用的两条本机事实（调试侧原本靠 /rules/debug-status 拿）
  "lane_holder", "lane_waiting", "elapsed_ms", "phase_ms",
];

export function jobFromFrame(frame) {
  const out = {};
  if (!frame || typeof frame !== "object") return out;
  JOB_KEYS.forEach((key) => {
    if (key in frame) out[key] = frame[key];
  });
  return out;
}

/** 把一帧并进状态；返回**新对象**（调用方负责落进响应式容器）。 */
export function mergeFrame(state, frame) {
  const prev = state || emptyRun();
  const next = { ...prev };
  next.job = { ...(prev.job || {}), ...jobFromFrame(frame) };
  const events = Array.isArray(frame && frame.events) ? frame.events : [];
  const freshEvents = events.filter((ev) => Number(ev && ev.seq) > prev.eventSeq);
  if (freshEvents.length) next.events = prev.events.concat(freshEvents);
  const failures = frame && frame.failures && Array.isArray(frame.failures.items)
    ? frame.failures.items : [];
  const freshFailures = failures.filter((it) => Number(it && it.seq) > prev.failureSeq);
  if (freshFailures.length) next.failures = prev.failures.concat(freshFailures);
  const cursors = [
    prev.eventSeq, Number(frame && frame.cursor) || 0,
  ];
  next.eventSeq = Math.max(...cursors);
  next.failureSeq = Math.max(
    prev.failureSeq, Number(frame && frame.failures && frame.failures.cursor) || 0);
  next.failuresTotal = Number(
    (frame && frame.failures && frame.failures.total) ?? prev.failuresTotal) || 0;
  next.failuresTruncated = !!(frame && frame.failures && frame.failures.truncated);
  next.done = !!(frame && frame.done);
  return next;
}

/** 任务行 → 运行条要的观测快照（形状同 `/rules/debug-status`，见 `utils/debugRun.js`）。 */
export function asRunStatus(job) {
  const j = job || {};
  return {
    phase: j.phase || "",
    elapsed_ms: j.elapsed_ms || 0,
    phase_ms: j.phase_ms || 0,
    lane_holder: j.lane_holder || "",
    lane_waiting: j.lane_waiting || 0,
  };
}
