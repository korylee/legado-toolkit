// 任务观测的共享状态（单例，同 useDebugSession 的模式）。
//
// 两件事收在这里，别人不各自维护：
//
// 1. **在跑任务**（`active`）：徽标/在跑条/列表要的是"有几条在跑"，原来这个数绕了
//    一圈 emit 回到父组件；现在谁拿到事件（提交成功 / 帧 / 列表拉回）谁 upsert。
// 2. **每个任务一条流**（`runs` + `watchJob`）：帧是增量的，而同一个任务可能同时被
//    明细弹窗、时间线面板、列表页在跑条、生成弹窗盯着——各开一条 SSE 就会撞浏览器
//    的同源连接上限，各存一份状态就会出现"弹窗说在跑、时间线说完了"。所以按
//    **引用计数**共用一条流与一份合并后的状态；没人看时关流。
import { ref, computed } from "vue";

import { openJobStream } from "../api/jobs";
import { emptyRun, mergeFrame } from "../utils/runStream";

//: 非终态白名单。终态与未知状态（流兜底的 "unknown"：重连到上限没拿到终态，
//: 任务实际可能早跑完了）都**不算在跑**——与抽屉列表的状态标签同口径。
const ACTIVE = new Set(["pending", "running", "cancel_requested"]);

//: job_id -> 最近一帧任务状态（含 progress / phase / total / kind）。
//:
//: **存整帧，不是只存 status**：徽标数的是「有几条在跑」，而进度条、阶段、在跑条
//: 要的是同一批事件里的另外几个数（提交成功 / 帧 / 列表拉回）。只存 status
//: 的话，那几个数就得各自再维护一份「当前任务」的 ref——历史上正是这么散成三份的。
const active = ref(new Map());

//: job_id -> 观测状态（帧合并后的：任务行 + 时间线 + 失败清单 + 终态标记）。
const runs = ref(new Map());

//: job_id -> { refs, stop, done, callbacks }。**不是响应式的**：它是连接的生命周期账，
//: 界面不读它（读了就意味着有人要按连接状态渲染）。
const streams = new Map();

const runningCount = computed(() => active.value.size);

//: 在跑任务（新的在前）。列表页的在跑条与任务中心都读这一份。
const activeJobs = computed(() => Array.from(active.value.values()).sort(
  (a, b) => String(b.created_at || "").localeCompare(String(a.created_at || ""))));

/** 幂等：同一任务重复 upsert 只覆盖它带来的字段。
 *
 * **不看 `error`**：终态失败/中止的行本来就带原因（后端 `_error_text` 会把
 * `result["reason"]` 一并给出），拿"有 error"当"这不是一条任务数据"会让失败的任务
 * **永远留在「在跑」里**——徽标数不清、在跑条不消失。那条"任务不存在"的帧没有
 * `id`，靠 `!job.id` 挡住它才是对的分辨办法。
 */
function upsertJob(job) {
  if (!job || !job.id) return;
  if (ACTIVE.has(job.status)) {
    const prev = active.value.get(job.id) || {};
    active.value.set(job.id, { ...prev, ...job });
  } else {
    active.value.delete(job.id);
  }
}

/** 按 id 取在跑任务的那一帧；不在跑（或没见过）给 null。 */
function jobById(id) {
  return active.value.get(id) || null;
}

function removeJob(id) {
  active.value.delete(id);
}

/** 按 id 取观测状态；没见过给 null（不是空状态——"没有"和"还没有事件"要分得开）。 */
function frameOf(id) {
  return runs.value.get(String(id || "")) || null;
}

function finishStream(key, extraJob) {
  const entry = streams.get(key);
  if (!entry || entry.done) return;      // 终态只通知一次
  entry.done = true;
  if (extraJob) {
    // 兜底结局（重连到上限 / 任务不存在）：把它并进任务行再通知，调用方只看一份状态
    const state = runs.value.get(key) || emptyRun();
    runs.value.set(key, { ...state, job: { ...(state.job || {}), ...extraJob } });
  }
  const state = runs.value.get(key) || emptyRun();
  Array.from(entry.callbacks).forEach((cb) => {
    try {
      cb(state);
    } catch (e) {
      // 回调是消费方的事，不能反过来把流的状态机带死
      console.warn("[jobs] 终态回调失败：", e);
    }
  });
}

function noteFrame(frame) {
  if (!frame || typeof frame !== "object" || frame.error || !frame.id) return;
  upsertJob(frame);
  const next = mergeFrame(runs.value.get(frame.id) || emptyRun(), frame);
  runs.value.set(frame.id, next);
  if (next.done) finishStream(frame.id, null);
}

/**
 * 盯住一个任务的观测流；返回取消函数（引用计数到 0 才真的关流）。
 *
 * `onDone(state)` 在**终态**或兜底结局时各叫一次：那时 `state.job` 已经是终态行、
 * `state.events`/`state.failures` 是完整的增量合并结果——要用原始结果体的调用方
 * 走 `getJobDetail`（见 `api/jobs.js`），不在这条流里传大对象。
 */
function watchJob(id, onDone) {
  const key = String(id || "");
  if (!key) return () => {};
  let entry = streams.get(key);
  if (!entry) {
    entry = { refs: 0, stop: null, done: false, callbacks: new Set() };
    streams.set(key, entry);
    entry.stop = openJobStream(key, {
      onFrame: noteFrame,
      onEnd: (extraJob) => finishStream(key, extraJob),
    });
  }
  entry.refs += 1;
  if (typeof onDone === "function") entry.callbacks.add(onDone);
  let stopped = false;
  return () => {
    if (stopped) return;
    stopped = true;
    entry.refs -= 1;
    if (typeof onDone === "function") entry.callbacks.delete(onDone);
    if (entry.refs <= 0) {
      if (entry.stop) entry.stop();
      streams.delete(key);
    }
  };
}

export function useJobs() {
  return { active, activeJobs, runningCount, upsertJob, jobById, removeJob,
           runs, frameOf, watchJob };
}
