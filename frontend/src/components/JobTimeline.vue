<script setup>
// 执行时间线（jvm-batch-timeline）：像构建日志一样看批量校验。
// 运行中每 2 秒按游标拉一次骨架事件增量 + 失败源；终态拉一次全量就停。
// 进度只报后端给的真实数（不做估算/ETA）；「正在校验某源」的瞬时状态
// 引擎没有上报，不编。失败块的引擎日志从详情投影的 chunk_reports 取。
import { ref, computed, watch, onUnmounted, nextTick } from "vue";
import { api } from "../api/client";
import { formatTimelineEvent, formatFailureLine } from "../utils/jobTimeline";

const props = defineProps({
  jobId: { type: String, default: "" },
  status: { type: String, default: "" },
  // 详情投影里的块级报告：失败块展开时取 Gradle 日志尾部作附件
  chunkReports: { type: Array, default: () => [] },
});
const emit = defineEmits(["state"]);

const POLL_MS = 2000;
const TERMINAL = ["done", "failed", "cancelled"];

const lines = ref([]);
const failures = ref({ total: 0, truncated: false, items: [] });
const expanded = ref("");
const pollError = ref("");
const following = ref(true);
const listEl = ref(null);
let cursor = 0;
let isDone = false;
let timer = null;
let lastKey = "";
let pollInFlight = false;
let pollPending = false;
let pollGeneration = 0;

const hhmmss = (ts) => {
  if (!ts) return "--:--:--";
  const d = new Date(ts * 1000);
  const p = (x) => String(x).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
};

const logOf = (index) => {
  const report = props.chunkReports.find((c) => c && c.index === index);
  if (!report || !report.gradle) return "";
  return report.gradle.stderr || report.gradle.stdout || "";
};

const failureTotal = computed(() => failures.value.total || 0);
const notShown = computed(() =>
  Math.max(failureTotal.value - (failures.value.items || []).length, 0));

function scrollFollow() {
  if (!following.value) return;
  nextTick(() => {
    const el = listEl.value;
    if (el) el.scrollTop = el.scrollHeight;
  });
}

function stopPolling() {
  if (timer) clearInterval(timer);
  timer = null;
}

async function pull() {
  if (!props.jobId) return;
  if (pollInFlight) {
    pollPending = true;
    return;
  }
  const generation = pollGeneration;
  const jobId = props.jobId;
  pollInFlight = true;
  try {
    const data = await api.get(`/jobs/${jobId}/timeline?after=${cursor}`);
    if (generation !== pollGeneration || jobId !== props.jobId) return;
    pollError.value = "";
    emit("state", {
      status: data.status,
      phase: data.phase,
      progress: data.progress,
      total: data.total,
    });
    if (Array.isArray(data.events) && data.events.length) {
      lines.value = lines.value.concat(
        data.events.map((ev) => ({
          seq: ev.seq, ts: ev.ts, kind: ev.kind, index: ev.index,
          ...formatTimelineEvent(ev),
        })));
    }
    cursor = data.cursor ?? cursor;
    if (data.failures) failures.value = data.failures;
    if (data.done) {
      isDone = true;
      stopPolling();
    }
    scrollFollow();
  } catch (e) {
    if (generation !== pollGeneration || jobId !== props.jobId) return;
    // 单次轮询失败不终止跟随：下一轮重试；连续失败才提示（后端可能重启了）
    pollError.value = pollError.value === "" ? "1" : pollError.value + "1";
    if (pollError.value.length >= 3) pollError.value = "err";
  } finally {
    pollInFlight = false;
    if (pollPending && !isDone) {
      pollPending = false;
      void pull();
    }
  }
}

function start() {
  pollGeneration += 1;
  const key = `${props.jobId}|${props.status}`;
  if (key === lastKey) return;
  // 同一任务由轮询自然收尾（done 已停表）就不重置重来——终态的那几行
  // 已经在增量里了
  if (lastKey.startsWith(`${props.jobId}|`) && isDone) {
    lastKey = key;
    return;
  }
  lastKey = key;
  stopPolling();
  pollPending = false;
  lines.value = [];
  failures.value = { total: 0, truncated: false, items: [] };
  expanded.value = "";
  cursor = 0;
  isDone = false;
  pollError.value = "";
  following.value = true;
  if (!props.jobId || TERMINAL.includes(props.status)) {
    // 终态：一次全量即可（后端从 result_json 给，不再碰磁盘）
    pull();
    return;
  }
  pull();
  timer = setInterval(() => {
    if (!isDone) pull();
  }, POLL_MS);
}

watch(() => [props.jobId, props.status], start, { immediate: true });
onUnmounted(stopPolling);

function onScroll() {
  const el = listEl.value;
  if (!el) return;
  following.value = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
}
</script>

<template>
  <div class="job-timeline">
    <div class="job-timeline-head">
      <span class="muted detail-section-title">执行时间线</span>
      <span v-if="!following" class="job-timeline-follow" @click="following = true; scrollFollow()">回到最新</span>
    </div>
    <div ref="listEl" class="job-timeline-list" @scroll="onScroll">
      <div v-for="line in lines" :key="line.seq" class="job-timeline-line" :class="'is-' + line.tone">
        <span class="job-timeline-ts">{{ hhmmss(line.ts) }}</span>
        <span class="job-timeline-text">{{ line.text }}</span>
        <span
          v-if="line.kind === 'chunk_failed' && logOf(line.index)"
          class="job-timeline-log-toggle"
          @click="expanded = expanded === String(line.seq) ? '' : String(line.seq)"
        >{{ expanded === String(line.seq) ? "收起日志" : "看日志" }}</span>
        <pre v-if="expanded === String(line.seq) && logOf(line.index)" class="job-timeline-log">{{ logOf(line.index) }}</pre>
      </div>
      <div v-for="(item, i) in failures.items" :key="'failure' + i" class="job-timeline-line is-error">
        <span class="job-timeline-ts">失败源</span>
        <span class="job-timeline-text">✕ {{ formatFailureLine(item).title }}：{{ formatFailureLine(item).detail }}</span>
      </div>
      <div v-if="notShown > 0" class="job-timeline-line is-warn">
        <span class="job-timeline-ts">{{ hhmmss() }}</span>
        <span class="job-timeline-text">还有 {{ notShown }} 条失败源未显示</span>
      </div>
      <div v-if="pollError === 'err'" class="muted job-timeline-note">时间线暂时取不到（任务仍在后台执行），会继续重试</div>
      <div v-if="!lines.length && !failures.items.length" class="muted job-timeline-note">还没有执行记录</div>
    </div>
  </div>
</template>

<style>
/* el-drawer 会 teleport：本组件的壳元素由 Scoped 管不了，按 AGENTS #15
   用组件名前缀写在无 Scoped 块（只此组件用，不上全局 styles.css） */
.job-timeline { margin-top: 12px; }
.job-timeline-head { display: flex; align-items: baseline; gap: 12px; }
.job-timeline-follow, .job-timeline-log-toggle {
  font-size: 12px; color: var(--el-color-primary); cursor: pointer; user-select: none;
}
.job-timeline-list {
  /* 52vh 而不是固定 320px：时间线是跑批弹窗里的**主内容**，压在 320px 里
     一屏只能看十几行，用户读到的信息被滚动条藏掉大半（2026-10-05 用户反馈
     「布局不能完整展示信息」）。vh 随窗口走，小窗口也不至于把弹窗撑爆 */
  margin-top: 6px; max-height: 52vh; overflow: auto;
  padding: 6px 8px; border: 1px solid var(--el-border-color-extra-light);
  border-radius: 6px; background: var(--el-fill-color-lighter);
  font-family: var(--el-font-family-monospace, monospace); font-size: 12px;
}
.job-timeline-line { line-height: 1.7; white-space: pre-wrap; word-break: break-all; }
.job-timeline-ts { color: var(--el-text-color-secondary); margin-right: 8px; }
.job-timeline-line.is-warn .job-timeline-text { color: var(--el-color-warning); }
.job-timeline-line.is-error .job-timeline-text { color: var(--el-color-danger); }
.job-timeline-log-toggle { margin-left: 8px; }
.job-timeline-log {
  margin: 4px 0 2px; padding: 6px; max-height: 160px; overflow: auto;
  background: var(--el-fill-color-light); border-radius: 4px;
  white-space: pre-wrap; font-size: 11px;
}
.job-timeline-note { font-size: 12px; padding: 4px 0; }
</style>
