<script setup>
// 任务进度区：**明细弹窗与一键生成面板共用这一份**。
//
// 只回答三个问题，每个问题一个数据源（这是设计判据，别混着用）：
//   ① 走到哪了   → 任务行 `progress` / `total`
//   ② 花了多久   → `elapsed_ms`（总）/ `phase_ms`（本段）
//   ③ 现在在干什么 / 卡没卡 → **当前段的事件**（低频拉，不是订阅整条流）
//
// 为什么事件用「拉」不用 SSE：这里只要「此刻在做什么」，不需要逐行时间线。每 1.5 秒
// 拉一条 `/jobs/{id}/timeline?after=<游标>`，增量极小（一段只有两三条事件），而 SSE
// 那条流保持轻量（它推的是任务行 + 全量事件）。同一个接口，两种读法，不是新端点。
//
// **本件不判断任务成败**：`progress_kind` 决定画百分比还是「第 N/M 段」；成败由调用方
// 从 result 里读。范围锁死的好处是这类组件不会长成第二个状态机。
import { computed, onUnmounted, ref, watch } from "vue";

import { jobTimeline } from "../api/jobs";
import { useJobs } from "../composables/useJobs";
import { formatTimelineEvent } from "../utils/jobTimeline";
import { STEP_LABELS } from "../utils/steps";
import { stageStates } from "../utils/taskProgress";

const props = defineProps({
  //: 任务行（来自 `useJobs` 的 `jobById` 或详情接口）。缺字段就少画一块，不报错。
  job: { type: Object, default: null },
  //: 段名清单（一键生成用，来自详情接口的 `stages`）。没有它就走 count 分支。
  stages: { type: Array, default: () => [] },
  //: 紧凑模式（面板里用）：不重复显示总用时
  compact: { type: Boolean, default: false },
  //: 结果体里的段结论（`result.stages`）。跑完后**以它为准**给每段上色——
  //: 任务行的 `progress` 是「正在跑第几段」，跑完不会等于段数，拿它判最后一段会一直
  //: 显示「等待」（实测 2026-10-28）。
  finalStages: { type: Array, default: () => [] },
});

const { jobById } = useJobs();

const TERMINAL = ["done", "failed", "cancelled"];
//: 卡住判据：**多久没有新事件**。取 60 秒——实测首次调试要 41.5 秒（引擎冷启动），
//: 30 秒会在第一段误报。
const STALL_SEC = 60;

const row = computed(() => {
  const live = props.job && props.job.id ? jobById(props.job.id) : null;
  return live || props.job || null;
});
const status = computed(() => String((row.value && row.value.status) || ""));
const running = computed(() => !TERMINAL.includes(status.value));
//: 进度语义：`step` 才有段卡片；其它（`count` / ''）走百分比
const isStep = computed(() => String((row.value && row.value.progress_kind) || "") === "step"
  && props.stages.length > 0);
//: 「准备引擎」是四段之外的**前置一步**（冷启动四十秒）。这时 `progress` 还是 0，
//: 若照原样画，第 1 段会显示「进行中」——用户以为在搜索，其实还在点火。
const preparing = computed(() => String((row.value && row.value.phase) || "") === "preparing_engine");
const progress = computed(() => Number((row.value && row.value.progress) || 0));
const total = computed(() => Number((row.value && row.value.total) || 0) || props.stages.length);

//: 段卡片：当前段之前都算通过（后端一失败就 break，不会往后走），当前段看事件，
//: 之后的段是等待。
const steps = computed(() => stageStates(props.stages, {
  progress: progress.value,
  running: running.value,
  preparing: preparing.value,
  finalStages: props.finalStages,
}));

// ---- 事件：低频拉，只用于「现在在做什么」与「卡没卡」 ----
//: 游标（行号）。拉到的帧里 `cursor` 是下一次的起点。
const cursor = ref(0);
//: 当前段最近一条事件（`{text, tone, ts}`）
const activity = ref(null);
//: `activity` 属于哪一段。**必须记**：换段时新段的事件还没到，不清就会显示上一段
//: 的最后一条（实测：详情段进行中，活动行却写着「搜索 · 通过」）。
const activityStage = ref("");
//: 最后一条事件的时间（Unix 秒）：判卡住用
const lastTs = ref(0);
const nowSec = ref(Date.now() / 1000);
let timer = null;
let nowTimer = null;

async function poll() {
  const id = row.value && row.value.id;
  if (!id || !running.value || !isStep.value) return;
  try {
    const frame = await jobTimeline(id, cursor.value);
    const events = (frame && frame.events) || [];
    if (Number(frame && frame.cursor) >= cursor.value) cursor.value = Number(frame.cursor) || cursor.value;
    const stage = String(row.value.phase || "");
    if (activityStage.value !== stage) { activity.value = null; activityStage.value = stage; }
    for (const ev of events) {
      // 只看**当前段**的事件：跨段折叠是另一个问题，这里范围锁死
      if (!ev || ev.stage !== stage) continue;
      const line = formatTimelineEvent(ev);
      if (line && line.text) activity.value = { ...line, ts: Number(ev.ts) || 0 };
      if (ev.ts) lastTs.value = Math.max(lastTs.value, Number(ev.ts));
    }
  } catch (e) {
    // 拉不到就只显示段名——降级，不弹错（这是辅助信息，不是数据来源）
  }
}

function start() {
  stop();
  if (!running.value || !isStep.value) return;
  void poll();
  timer = setInterval(poll, 1500);
  nowTimer = setInterval(() => { nowSec.value = Date.now() / 1000; }, 1000);
}
function stop() {
  if (timer) { clearInterval(timer); timer = null; }
  if (nowTimer) { clearInterval(nowTimer); nowTimer = null; }
}

// 换任务就重置游标与活动（同一组件会被复用）
watch(() => (row.value && row.value.id), () => {
  cursor.value = 0;
  activity.value = null;
  activityStage.value = "";
  lastTs.value = 0;
  start();
}, { immediate: true });
// 从跑到停/停到跑，也要重开或收掉定时器
watch([running, isStep], () => start());
onUnmounted(stop);

//: 卡住：跑着、且**超过 STALL_SEC 没有新事件**。这是事实陈述，不是猜——
//: 判据来自事件时间戳，不是「我算出来该好了」。
const stalledSec = computed(() => {
  if (!running.value || !isStep.value || !lastTs.value) return 0;
  const gap = Math.round(nowSec.value - lastTs.value);
  return gap >= STALL_SEC ? gap : 0;
});

//: 本段已用（`phase_ms` 是任务行里的「当前阶段已耗时」）
const phaseCost = computed(() => {
  const ms = Number((row.value && row.value.phase_ms) || 0);
  if (!ms || !running.value) return "";
  return fmtSec(ms / 1000);
});
const totalCost = computed(() => {
  const ms = Number((row.value && row.value.elapsed_ms) || 0);
  return ms ? fmtSec(ms / 1000) : "";
});
function fmtSec(sec) {
  const s = Math.max(0, Math.round(sec));
  return s >= 60 ? `${Math.floor(s / 60)}m ${s % 60}s` : `${s}s`;
}

//: 百分比只对**等量计数**有意义（批量校验的数源条数）。段流水线各段时长不等，
//: 画百分比就是假的，所以 step 分支不画。
const pct = computed(() => {
  if (isStep.value || !total.value) return 0;
  return Math.min(100, Math.round((progress.value / total.value) * 100));
});
const countText = computed(() => {
  if (!total.value && !progress.value) return "";
  return `${progress.value} / ${total.value}`;
});
</script>

<template>
  <div v-if="row" class="tp">
    <!-- step：四段卡片 + 一行当前活动 -->
    <template v-if="isStep">
      <div class="tp-steps">
        <div v-for="s in steps" :key="s.key" class="tp-step" :class="'is-' + s.state"
             :title="s.label + ' · ' + s.text">
          <div class="tp-step-head">
            <span class="tp-step-mark">
              <template v-if="s.state === 'ok'">✓</template>
              <template v-else-if="s.state === 'fail'">✕</template>
              <template v-else>{{ s.idx }}</template>
            </span>
            <span class="tp-step-label">{{ s.label }}</span>
          </div>
          <div class="tp-step-text">{{ s.text }}</div>
        </div>
      </div>
      <div class="tp-live">
        <template v-if="preparing">
          <span class="tp-live-text">准备校验引擎</span>
          <span v-if="totalCost" class="tp-sep">·</span>
          <span v-if="totalCost">已跑 {{ totalCost }}</span>
        </template>
        <template v-else-if="stalledSec">
          <span class="tp-stall">
            已经 {{ fmtSec(stalledSec) }} 没有新进展（本段已 {{ phaseCost || "?" }}）
          </span>
        </template>
        <template v-else-if="activity">
          <span class="tp-live-text" :class="'is-' + activity.tone">{{ activity.text }}</span>
          <span v-if="phaseCost" class="tp-sep">·</span>
          <span v-if="phaseCost">本段 {{ phaseCost }}</span>
        </template>
        <template v-else>
          <span>第 {{ Math.min(progress + 1, total) }}/{{ total }} 段</span>
          <span v-if="phaseCost" class="tp-sep">·</span>
          <span v-if="phaseCost">本段 {{ phaseCost }}</span>
        </template>
        <template v-if="!compact && totalCost">
          <span class="tp-sep">·</span><span>已跑 {{ totalCost }}</span>
        </template>
      </div>
    </template>

    <!-- count：等量计数，百分比有意义（批量校验） -->
    <template v-else>
      <div class="tp-count">
        <span v-if="countText" class="tp-count-num">{{ countText }}</span>
        <span v-if="totalCost" class="tp-count-spent">已跑 {{ totalCost }}</span>
      </div>
      <el-progress :percentage="pct" :stroke-width="10" :show-text="false" />
    </template>
  </div>
</template>

<style scoped>
.tp-steps { display: flex; gap: 8px; margin-bottom: 8px; }
.tp-step {
  flex: 1 1 0;
  min-width: 0;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  padding: 8px 10px;
  background: var(--el-fill-color-blank);
  transition: border-color .2s, background .2s;
}
.tp-step-head { display: flex; align-items: center; gap: 6px; margin-bottom: 3px; min-width: 0; }
.tp-step-mark {
  width: 16px; height: 16px; border-radius: 50%;
  display: inline-flex; align-items: center; justify-content: center;
  font-size: 10px; font-weight: 600; line-height: 1;
  background: var(--el-fill-color); color: var(--el-text-color-placeholder); flex: none;
}
.tp-step-label {
  font-size: 12px; font-weight: 500; color: var(--el-text-color-placeholder);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.tp-step-text { font-size: 11px; color: var(--el-text-color-placeholder); padding-left: 22px; }
.tp-step.is-running { border-color: var(--el-color-primary-light-5); background: var(--el-color-primary-light-9); }
.tp-step.is-running .tp-step-mark {
  background: var(--el-color-primary); color: #fff;
  animation: tp-pulse 1.4s ease-in-out infinite;
}
.tp-step.is-running .tp-step-label { color: var(--el-color-primary); }
.tp-step.is-running .tp-step-text { color: var(--el-color-primary-light-3); }
.tp-step.is-ok { border-color: var(--el-color-success-light-5); background: var(--el-color-success-light-9); }
.tp-step.is-ok .tp-step-mark { background: var(--el-color-success); color: #fff; }
.tp-step.is-ok .tp-step-label, .tp-step.is-ok .tp-step-text { color: var(--el-color-success-dark-2); }
.tp-step.is-fail { border-color: var(--el-color-danger-light-5); background: var(--el-color-danger-light-9); }
.tp-step.is-fail .tp-step-mark { background: var(--el-color-danger); color: #fff; }
.tp-step.is-fail .tp-step-label, .tp-step.is-fail .tp-step-text { color: var(--el-color-danger); }

.tp-live {
  display: flex; align-items: center; gap: 6px; flex-wrap: wrap;
  font-size: 12px; color: var(--el-text-color-secondary);
  margin-bottom: 12px; font-variant-numeric: tabular-nums;
}
.tp-live-text.is-error { color: var(--el-color-danger); }
.tp-live-text.is-warn { color: var(--el-color-warning-dark-2); }
.tp-sep { color: var(--el-border-color); }
.tp-stall { color: var(--el-color-warning-dark-2); }

.tp-count { display: flex; align-items: baseline; gap: 10px; margin-bottom: 6px; }
.tp-count-num { font-size: 15px; font-weight: 600; color: var(--el-text-color-primary); font-variant-numeric: tabular-nums; }
.tp-count-spent { font-size: 12px; color: var(--el-text-color-secondary); }

@keyframes tp-pulse {
  0%, 100% { box-shadow: 0 0 0 0 var(--el-color-primary-light-5); }
  50% { box-shadow: 0 0 0 4px transparent; }
}
</style>
