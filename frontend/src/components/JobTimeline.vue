<script setup>
// 执行时间线（jvm-batch-timeline）：像构建日志一样看批量校验。
//
// **本组件不再自己取数**：事件、失败清单、终态都来自 `useJobs` 的那条流
// （每个任务一条，引用计数共用；见 composables/useJobs.js）。原来的 2 秒轮询 +
// 行号游标是"同一个任务的第二份观测"，它和明细弹窗拉的 `/jobs/{id}` 必然漂。
// 进度只报后端给的真实数（不做估算/ETA）；「正在校验某源」的瞬时状态引擎没有上报，不编。
// 失败块的引擎日志从详情投影的 chunk_reports 取。
import { ref, computed, watch, onUnmounted } from "vue";
import { useJobs } from "../composables/useJobs";
import { useFollowScroll } from "../composables/useFollowScroll";
import { emptyRun } from "../utils/runStream";
import { formatTimelineEvent, formatFailureLine, groupTimelineLines } from "../utils/jobTimeline";
import { executionModeLabel } from "../utils/jobs";

const props = defineProps({
  jobId: { type: String, default: "" },
  status: { type: String, default: "" },
  // 任务级的执行方式（详情投影）。它决定这次会不会慢，所以摆在时间线头上，
  // 和逐块明细同一处——原来它收在「技术细节」折叠块的最底下，等于看不见
  executionMode: { type: String, default: "" },
  executionNote: { type: String, default: "" },
  // 详情投影里的块级报告：失败块展开时取 Gradle 日志尾部作附件
  chunkReports: { type: Array, default: () => [] },
});

const TERMINAL = ["done", "failed", "cancelled"];
const { frameOf, watchJob } = useJobs();

//: 本组件也占一个流的引用：只开明细弹窗不开时间线时，流归弹窗；两个都开也只有一条连接
let stopWatch = null;
watch(() => props.jobId, (id) => {
  if (stopWatch) stopWatch();
  stopWatch = id ? watchJob(id) : null;
}, { immediate: true });
onUnmounted(() => { if (stopWatch) stopWatch(); });

const state = computed(() => frameOf(props.jobId) || emptyRun());
const status = computed(() => (state.value.job || {}).status || props.status);
//: 结果态默认收起来：那时用户要读的是变化清单，时间线是排查时才摊开的材料
const collapsed = ref(false);
const expanded = ref("");
const listEl = ref(null);

const lines = computed(() => (state.value.events || []).map((ev) => ({
  seq: ev.seq, ts: ev.ts, kind: ev.kind, index: ev.index,
  ...formatTimelineEvent(ev),
})));
//: 跟随最新（判据与调试的时间线共用一份，见 composables/useFollowScroll）
const { following, onScroll, followLatest } = useFollowScroll(listEl, lines);
const failures = computed(() => ({
  total: state.value.failuresTotal || 0,
  truncated: state.value.failuresTruncated,
  items: state.value.failures || [],
}));

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

//: 执行方式的中文词（「Gradle 回退」是最要紧的那一档——它意味着这次会慢）
const modeLabel = computed(() => executionModeLabel(props.executionMode));
//: 渲染前先归组：块结构是模板与样式共用的判据（见 utils/jobTimeline 的 groupTimelineLines）
const groupedLines = computed(() => groupTimelineLines(lines.value));
const failureTotal = computed(() => failures.value.total || 0);
const notShown = computed(() =>
  Math.max(failureTotal.value - (failures.value.items || []).length, 0));

//: 开局与终态都按状态收起：终态时主视线归结论（变化清单），展开时间线是排查动作
watch(status, (value) => { collapsed.value = TERMINAL.includes(value); },
      { immediate: true });
</script>

<template>
  <div class="job-timeline" :class="{ 'is-collapsed': collapsed }">
    <div class="job-timeline-head">
      <!-- 折叠入口是**按钮**：原来那个 span 键盘到不了、也没有焦点环 -->
      <button type="button" class="muted job-timeline-toggle" :aria-expanded="!collapsed"
              @click="collapsed = !collapsed">
        执行时间线<span class="job-timeline-chevron" :class="{ 'is-open': !collapsed }">▸</span>
        <span v-if="collapsed && lines.length">{{ lines.length }} 行</span>
      </button>
      <!-- 执行方式是时间线的摘要：它决定这次会不会慢，与逐块明细同一处；
           胶囊视觉用全局 .chip（与统计条筛选、结果统计是同一件东西）；
           补充说明挂 title 上，不占主视线 -->
      <span v-if="modeLabel" class="chip" :title="executionNote">{{ modeLabel }}</span>
      <span v-if="!collapsed && !following" class="job-timeline-follow"
            @click="followLatest">回到最新</span>
    </div>
    <div v-show="!collapsed" ref="listEl" class="job-timeline-list" @scroll="onScroll">
      <div v-for="item in groupedLines" :key="item.key" class="job-timeline-line"
           :class="['is-' + item.line.tone, { 'is-nested': item.depth > 0 }]">
        <span class="job-timeline-ts">{{ hhmmss(item.line.ts) }}</span>
        <span class="job-timeline-text">{{ item.line.text }}</span>
        <span
          v-if="item.line.kind === 'chunk_failed' && logOf(item.line.index)"
          class="job-timeline-log-toggle"
          @click="expanded = expanded === String(item.line.seq) ? '' : String(item.line.seq)"
        >{{ expanded === String(item.line.seq) ? "收起日志" : "看日志" }}</span>
        <pre v-if="expanded === String(item.line.seq) && logOf(item.line.index)"
             class="job-timeline-log">{{ logOf(item.line.index) }}</pre>
      </div>

      <!-- 失败源是**结论**、不是时间序列：和事件行同格式混在一起时，读的人分不清
           哪条是「发生过什么」、哪条是「最后坏了几条」 -->
      <div v-if="failures.items.length" class="job-timeline-failures">
        <div class="muted job-timeline-failures-title">失败源（{{ failureTotal }}）</div>
        <div v-for="(item, i) in failures.items" :key="'failure' + i"
             class="job-timeline-line is-error">
          <span class="job-timeline-ts">✕</span>
          <span class="job-timeline-text">
            {{ formatFailureLine(item).title }}：{{ formatFailureLine(item).detail }}
          </span>
        </div>
        <div v-if="notShown > 0" class="muted job-timeline-failures-more">
          还有 {{ notShown }} 条失败源未显示
        </div>
      </div>

      <div v-if="!lines.length && !failures.items.length" class="muted job-timeline-note">还没有执行记录</div>
    </div>
  </div>
</template>

<style>
/* el-drawer 会 teleport：本组件的壳元素由 Scoped 管不了，按 AGENTS #15
   用组件名前缀写在无 Scoped 块（只此组件用，不上全局 styles.css） */
/* 高度由弹窗骨架给（TaskDetailDialog 末尾的 flex 链往这里传），`flex-basis: 0`
   让列表吃剩余空间，而不是自己按视口定高——52vh 与弹窗的 88vh 各算各的，
   窗口一矮就会同时冒出弹窗和列表两根滚动条。
   **保底必须是视口相关的**：弹窗只有 max-height，内容不到 88vh 时它按内容收缩，
   链上就没有「剩余空间」可分，grow 全是空转——这种情形下保底就是时间线实际
   拿到的高度（固定 96px 时实测只有三行，2026-10-05 截图）。32vh 在任何窗口下
   都够读，又不会把弹窗顶过 88vh。 */
.job-timeline {
  flex: 1 1 0; min-height: 32vh; margin-top: 12px;
  display: flex; flex-direction: column;
}
/* 收起时不再吃剩余空间：结果态把高度让给结论（变化清单），自己只留一行标题 */
.job-timeline.is-collapsed { flex: 0 0 auto; min-height: 0; }
.job-timeline-head { display: flex; align-items: baseline; gap: 12px; flex: 0 0 auto; }
/* 折叠入口是按钮：键盘可达、有焦点环；原来那个 span 只有鼠标能点 */
.job-timeline-toggle {
  display: inline-flex; align-items: center; gap: 4px;
  padding: 2px 6px; margin-left: -6px;
  border: none; border-radius: 4px; background: none;
  font: inherit; cursor: pointer;
}
.job-timeline-toggle:hover { background: var(--el-fill-color-light); }
.job-timeline-chevron { transition: transform .15s; color: var(--el-text-color-secondary); }
.job-timeline-chevron.is-open { transform: rotate(90deg); }
.job-timeline-follow, .job-timeline-log-toggle {
  font-size: 12px; color: var(--el-color-primary); cursor: pointer; user-select: none;
}
.job-timeline-list {
  /* 高度来自骨架（吃 `.job-timeline` 的剩余空间），不再按视口定高：52vh 与
     弹窗的 88vh 各算各的，窗口一矮就同时出现两根滚动条。「要能完整展示信息」
     这条约束还在，只是现在由 flex 保证，而不是靠调 vh 的数值。 */
  flex: 1 1 0; min-height: 0;
  margin-top: 6px; overflow: auto; scrollbar-gutter: stable;
  padding: 6px 8px; border: 1px solid var(--el-border-color-extra-light);
  border-radius: 6px; background: var(--el-fill-color-lighter);
  font-family: var(--el-font-family-monospace, monospace); font-size: 12px;
}
/* 三列：时间戳 / 正文 / 动作。原来是行内文本流——有没有第三列、正文长短，
   都会把正文起点推歪，几十行看下来是锯齿。 */
.job-timeline-line {
  display: grid; grid-template-columns: 62px 1fr auto;
  column-gap: 8px; align-items: baseline;
  line-height: 1.7; white-space: pre-wrap;
  /* break-all 会把 URL 与路径从中间劈开；anywhere 只在该断的地方断 */
  overflow-wrap: anywhere;
}
/* 块内后续事件缩进 + 左侧细线：块的边界一眼可辨（块结构见 groupTimelineLines） */
.job-timeline-line.is-nested {
  padding-left: 14px;
  border-left: 2px solid var(--el-border-color-extra-light);
}
.job-timeline-ts { color: var(--el-text-color-secondary); }
.job-timeline-log-toggle { margin-left: 8px; }
/* 异常行给整行淡底 + 左侧色条：跑批时用户的动作就是扫一眼有没有红的，
   只染文字色不够——一屏几十行里找不出来。写在 is-nested 之后，异常优先级更高。 */
.job-timeline-line.is-warn {
  padding-left: 8px; border-left: 2px solid var(--el-color-warning);
  background: var(--el-color-warning-light-9, #fdf6ec);
}
.job-timeline-line.is-error {
  padding-left: 8px; border-left: 2px solid var(--el-color-danger);
  background: var(--el-color-danger-light-9, #fef0f0);
}
.job-timeline-line.is-warn .job-timeline-text { color: var(--el-color-warning); }
.job-timeline-line.is-error .job-timeline-text { color: var(--el-color-danger); }
.job-timeline-log {
  /* 日志块横跨三列：挤在正文列里会被时间戳列吃掉宽度 */
  grid-column: 1 / -1;
  margin: 4px 0 2px; padding: 6px; max-height: 160px; overflow: auto;
  background: var(--el-fill-color-light); border-radius: 4px;
  white-space: pre-wrap; font-size: 11px;
}
.job-timeline-failures { margin-top: 10px; }
.job-timeline-failures-title { margin-bottom: 4px; }
.job-timeline-failures-more { margin-top: 4px; }
.job-timeline-note { font-size: 12px; padding: 4px 0; }
</style>
