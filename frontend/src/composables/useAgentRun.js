// 一轮 AI 补全（`kind="agent_loop"`）的观测与收尾——**唯一一份**。
//
// 原来这一套写死在 DebugWorkbench 里：提交任务 → 吃观测流逐字段处理 → 结束时取结果体
// 写回输入框。AI 补全弹窗要同一个任务的状态与逐字段四态，只能靠这份共享件——
// 抄一份的后果不是报错，是「弹窗说补上了 2 个，输入框只落了 1 个」。
//
// 分工（照抄工作台已经踩出来的三条纪律）：
//   - 观测只走 `useJobs().watchJob`（引用计数共享一条 SSE），本件**不开定时器**；
//   - 逐字段事实的取词/归并在 `utils/agentRun`（纯函数，有单测），这里只管状态与副作用；
//   - 失败原因原样带到界面（AGENTS #4），不压成「失败」。
//
// **单例**（同 `useJobs` / `useDebugSession`）：调试工作台与 AI 补全弹窗是两个组件，
// 但看的是同一轮补全。状态声明在 `agentRunState.js`（模块作用域、叶子件），本件只放
// 行为——状态写在函数体里就会每次取用新建一份，后果不是报错，是「工作台提交的那一轮，
// 弹窗里看不到」（2026-10-20 真发生过一次）。
import { computed, watch } from "vue";

import { agentLoop } from "../api/rules";
import { cancelJob, getJobDetail, listJobs } from "../api/jobs";
import { useJobs } from "../composables/useJobs";
import {
  debugJobId,
  detailError,
  error,
  fields,
  jobId,
  jobRow,
  result,
  running,
  step,
  summary,
} from "./agentRunState.js";
import {
  agentBudgetNotes,
  foldAgentFields,
  resultAgentFields,
  ruleFieldOf,
} from "../utils/agentRun";

const { watchJob, frameOf } = useJobs();

//: 已处理过的事件行号：帧是增量合并的，同一行会再出现（去重判据是 seq，不是内容）
let seen = new Set();
let stopWatch = null;
//: 等终态的那个 Promise 的出口（`track` 里设，`reset` 时清）
let settle = null;

//: 这一轮的事件（只读）：弹窗与工作台从同一帧取。**模块级**：`adoptStartedFields`
//: 的 watcher 在模块作用域，读的就是它。
const events = computed(() => {
  const frame = jobId.value ? frameOf(jobId.value) : null;
  return (frame && frame.events) || [];
});

/** 逐字段四态：事件优先；事件还没到（或流接晚了）时用结果体兜底。 */
const rows = computed(() => {
  const fromEvents = foldAgentFields(events.value, fields.value);
  // 事件一条都没有而结果体已经到了（流接晚了 / 刷新后重开）：用结果体兜底
  if (result.value && !events.value.length) {
    return resultAgentFields(result.value, fields.value);
  }
  return fromEvents;
});

/** 任务行（状态徽章、进度）：流帧优先，其次详情；两处都没有就是空对象。 */
const job = computed(() => {
  const frame = jobId.value ? frameOf(jobId.value) : null;
  const live = (frame && frame.job) || {};
  return { ...(jobRow.value || {}), ...live };
});

/** 事件一进来就把 `ai_started` 的字段集收下：第一帧常到得比首屏渲染早，不在这里收
 *  就永远显示不了字段。 */
function adoptStartedFields() {
  for (const ev of events.value) {
    if (String((ev && ev.kind) || "") !== "ai_started") continue;
    const got = (ev.fields || []).map(String);
    if (got.length && !fields.value.length) fields.value = got;
    if (!step.value && ev.step) step.value = String(ev.step);
    if (ev.debug_job_id && !debugJobId.value) debugJobId.value = String(ev.debug_job_id);
    break;
  }
}

// 模块级、**只注册一次**（放 useAgentRun 里等于每次取用多挂一个 watcher）
watch(events, adoptStartedFields, { immediate: true });

export function useAgentRun() {

  function reset() {
    if (stopWatch) stopWatch();
    stopWatch = null;
    seen = new Set();
    settle = null;
    jobId.value = "";
    running.value = false;
    fields.value = [];
    error.value = "";
    summary.value = "";
    result.value = null;
    detailError.value = "";
    step.value = "";
    debugJobId.value = "";
    jobRow.value = {};
  }

  /** 打开一条已有的 AI 任务（任务中心点进来 / 重挂）：先取详情，再挂观测。
   *
   * 任务行与结果体都来自详情；字段集取 `ai_started` 事件里那份（事件账本重放，
   * `run_frame(after=0)` 从头给）——拿不到就空表，不编字段。
   */
  async function open(id, { debugJobId: parent = "" } = {}) {
    reset();
    const key = String(id || "");
    if (!key) return null;
    jobId.value = key;
    if (parent) debugJobId.value = String(parent);
    try {
      const detail = await getJobDetail(key);
      if (detail) {
        jobRow.value = detail;
        result.value = detail.result || null;
        if (detail.status && !["done", "failed", "cancelled"].includes(detail.status)) {
          running.value = true;
        }
      }
    } catch (e) {
      detailError.value = "读取 AI 补全明细失败：" + String((e && e.message) || e);
    }
    // 事件账本：重开弹窗也要把这一轮的逐字段事实重放出来
    await replayEvents(key);
    const notes = agentBudgetNotes(result.value);
    if (notes.length) error.value = notes.join("；");
    return result.value;
  }

  /** 读一遍事件账本（`/jobs/{id}/timeline` 帧的 events 由 useJobs 合并，这里直接
   *  用那条流：`watchJob` 一挂上，历史帧就会到达）。 */
  function replayEvents(id) {
    return new Promise((resolve) => {
      let settled = false;
      let stop = null;
      const tick = (state) => {
        for (const ev of (state && state.events) || []) onEvent(ev);
        adoptStartedFields();
        if (settled) return;
        const status = String((state && state.job && state.job.status) || "");
        if (!status || !["done", "failed", "cancelled"].includes(status)) return;
        // 终态帧已到：历史事件都在这一帧里，可以收
        settled = true;
        if (stop) stop();
        resolve();
      };
      stop = watchJob(id, tick);
      // 帧可能已经缓存过：同步读一遍，终态就直接收
      tick({ job: job.value, events: events.value });
    });
  }

  function onEvent(ev) {
    const seq = ev && ev.seq;
    if (!seq || seen.has(seq)) return;
    seen.add(seq);
    if (String((ev && ev.kind) || "") !== "ai_done") return;
    summary.value = `写入 ${ev.written} 条 · 模型 ${ev.model_calls} 次 · 验证 ${ev.rule_tries} 次`;
    if (ev.stop) error.value = String(ev.stop);
  }

  /** 盯住这次任务直到终态；返回结果体（失败/取消给 `null`，原因在 `error`）。
   *
   * 每来一帧就把新事件喂给 `onEvent`；终态时拉一次详情拿完整结果体——流里不传大对象。
   */
  function track(id) {
    jobId.value = String(id);
    running.value = true;
    return new Promise((resolve) => {
      let done = false;
      settle = (value) => {
        if (done) return;
        done = true;
        settle = null;
        running.value = false;
        resolve(value);
      };
      stopWatch = watchJob(id, async (state) => {
        const status = String((state && state.job && state.job.status) || "");
        if (status !== "done") {
          error.value = String(
            (state && state.job && state.job.error)
              || (status === "cancelled" ? "AI 补全已取消" : "AI 补全没有结果"),
          );
          settle(null);
          return;
        }
        try {
          const detail = await getJobDetail(id);
          result.value = (detail && detail.result) || null;
          if (!result.value) detailError.value = "AI 补全没有返回结果";
          // 结果体的预算注记补进停止原因（事件里的 ai_done.stop 可能还没到）
          const notes = agentBudgetNotes(result.value);
          if (notes.length) error.value = notes.join("；");
          settle(result.value);
        } catch (e) {
          detailError.value = "AI 补全结果读取失败：" + String((e && e.message) || e);
          settle(null);
        }
      });
      // 帧可能在挂上之前就到了：补处理一遍（同一套 seq 去重）
      for (const ev of events.value) onEvent(ev);
    });
  }

  /** 提交一轮补全。`only` = 只补这些字段（后端 FILL_SPECS 蛇形键；空 = 整组）。
   *
   * **拿到任务号就返回**，不等整轮跑完：调用方要拿它去开弹窗；这一轮的进展由
   * `track` 继续推（`running` / `rows` 随事件走）。等整轮结束再返回，弹窗就要等到
   * 分钟后才出现——那时它已经没什么好看的了。
   */
  async function submit({ debugJobId: parent, step: stepName, only, allFields }) {
    reset();
    step.value = String(stepName || "");
    debugJobId.value = String(parent || "");
    fields.value = (Array.isArray(only) && only.length ? only : (allFields || [])).map(String);
    running.value = true;
    let submitted = null;
    try {
      submitted = await agentLoop({
        job_id: String(parent || ""),
        step: stepName,
        use_ai: true,
        ...(Array.isArray(only) && only.length ? { fields: only.map(String) } : {}),
      });
    } catch (e) {
      error.value = String((e && e.message) || e);
      running.value = false;
      return { jobId: "", done: Promise.resolve(null) };
    }
    const id = (submitted && submitted.job_id) || "";
    if (!id) {
      if (!error.value) error.value = "没有拿到任务号，AI 补全没跑起来";
      running.value = false;
      return { jobId: "", done: Promise.resolve(null) };
    }
    // `done` 是给「跑完要写回草稿」的那些调用方的：它等到终态并给出结果体
    return { jobId: id, done: track(id) };
  }

  /** 刷新页面后把那次**还在跑**的补全重挂上（任务行还在就挂得上）。
   *
   * `debugJobId` 给了就只要那一次调试起的（重挂挂到别人的补全会把规则写进错的源）。
   * **挂上就返回**（同 `submit`）：重挂的是分钟级的一轮，等它跑完会让调用方的
   * 「页面初始化」一直悬着。
   */
  async function resume({ debugJobId: parent = "", step: stepName = "", onDone = null } = {}) {
    if (running.value || jobId.value) return null;
    try {
      const page = await listJobs({ kind: "agent_loop", status: "active" });
      const row = ((page && page.items) || [])[0] || null;
      if (!row || !row.id) return null;
      if (stepName) step.value = stepName;
      // open 会重放历史帧并给出任务行；还在跑的那一轮再挂终态监视。
      // 终态时把结果体交给 `onDone`（工作台据此落框）——这里不能等它。
      await open(row.id, { debugJobId: parent });
      if (running.value) {
        const watching = track(row.id);
        if (typeof onDone === "function") {
          void watching.then((value) => {
            if (value) onDone(value);
          });
        }
      }
      return result.value;
    } catch (e) {
      // 重挂不上不影响手工重跑
      return null;
    }
  }

  /** 取消：花钱的动作要真的取消（不是只断前端的等）。 */
  async function cancel() {
    if (!jobId.value) return;
    try {
      await cancelJob(jobId.value);
    } catch (e) {
      error.value = "取消失败：" + String((e && e.message) || e);
    }
  }

  /** 规则字段路径（`ruleBookInfo.name`）→ 后端字段键（`name`），写回草稿时用。 */
  function ruleOf(field) {
    return ruleFieldOf(step.value, field);
  }

  /** 这一轮是哪个调试任务起的（`ai_started` / 提交参数给的那份）。 */
  function debugJobIdOf() {
    return debugJobId.value;
  }

  return {
    jobId, job, running, fields, error, summary, result, detailError, step, events, rows,
    submit, open, resume, cancel, reset, track, ruleOf, debugJobIdOf,
  };
}

