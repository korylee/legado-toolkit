<script setup>
// 任务明细弹窗：**同一个弹窗承担两种时序**——在跑时是进度条，结束后是结果明细。
//
// 为什么合成一个而不是两个：这两件事看的是同一条任务的同一段生命周期，分开做必然
// 出现「进度弹窗说 100%、明细弹窗说还在跑」这类矛盾；而且两个组件要各写一份轮询。
// 打开时先拉一次详情，在跑时按固定间隔拉任务状态（终态即停）。
//
// 进度只报后端给的真实数（不做估算/ETA）；「正在校验某源」这类瞬时状态引擎没上报，不编。
import { ref, computed, watch, onUnmounted } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { api } from "../api/client";
import { healthLabel } from "../utils/health";
import {
  isWorseHealth,
  jobAgoText,
  jobCancelHint,
  jobIsInFlight,
  jobIsTerminal,
  jobKindLabel,
  jobPhaseLabel,
  jobProgressStatus,
  jobSpentText,
  jobStatusLabel,
  jobStatusType,
} from "../utils/jobs";
import { useJobs } from "../composables/useJobs";
import { healthNeedsAction } from "../utils/tags";
import JobTimeline from "./JobTimeline.vue";

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  jobId: { type: String, default: "" },
});
const emit = defineEmits(["update:modelValue", "changed"]);

const visible = computed({
  get: () => props.modelValue,
  set: (v) => emit("update:modelValue", v),
});

const { upsertJob } = useJobs();

const detail = ref(null);
const live = ref(null);
const detailLoading = ref(false);
const rawOpen = ref(false);
//: 「此刻」的锚点：耗时与「多久没更新」都要跟着它走，由每次轮询推进（见 pullStatus）
const nowMs = ref(Date.now());
let timer = null;

//: 详情给静态字段，轮询帧只覆盖**会变的**那几个。整体展开 live 会把详情里的富字段
//: 盖成轻量版（比如 summary 丢掉 changed_items）——那是两个端点的不同投影，不能直接叠。
const job = computed(() => {
  const d = detail.value || {};
  const l = live.value || {};
  return {
    ...d,
    status: l.status || d.status,
    phase: l.phase || d.phase,
    progress: l.progress ?? d.progress,
    total: l.total ?? d.total,
    kind: l.kind || d.kind,
    updated_at: l.updated_at || d.updated_at,
  };
});
const summary = computed(() => (detail.value || {}).summary || null);
const inFlight = computed(() => jobIsInFlight(job.value.status));
//: 变坏的那几条排在最前：结果态真正的入口是「哪些源坏了」，不是「有几条变化」——
//: 顺序本身就是权重。哪档算坏由后端下发，见 isWorseHealth。
const changedItems = computed(() => {
  const items = (summary.value && summary.value.changed_items) || [];
  return [...items].sort((a, b) =>
    Number(isWorseHealth(b.to, healthNeedsAction.value))
    - Number(isWorseHealth(a.to, healthNeedsAction.value)));
});
//: 耗时与「多久没更新」都是相对**此刻**的数，不是 detail 里的静态字段：每 2 秒的
//: 轮询把 nowMs 顶一次，computed 才会跟着涨（Date.now() 自己不是响应式的）。
const spentText = computed(() => jobSpentText(job.value.created_at, nowMs.value));
const agoText = computed(() => jobAgoText(job.value.updated_at, nowMs.value));
const pct = computed(() => {
  const total = Number(job.value.total || 0);
  const progress = Number(job.value.progress || 0);
  return total > 0 ? Math.min(100, Math.round((progress / total) * 100)) : 0;
});
const progressText = computed(() => {
  const total = Number(job.value.total || 0);
  const progress = Number(job.value.progress || 0);
  if (!total && !progress) return "";
  return total ? `${progress} / ${total}` : `${progress} 条`;
});
const title = computed(() => {
  const kind = jobKindLabel(job.value.kind);
  return inFlight.value ? kind + "进行中" : kind + "明细";
});

function stopPoll() {
  if (timer) window.clearInterval(timer);
  timer = null;
}

async function loadDetail() {
  const id = props.jobId;
  if (!id) return;
  nowMs.value = Date.now();
  detailLoading.value = true;
  try {
    const data = await api.get("/jobs/" + id + "/detail");
    if (id === props.jobId) detail.value = data;
  } catch (e) {
    ElMessage.error("加载任务结果失败：" + e.message);
  } finally {
    detailLoading.value = false;
  }
}

async function pullStatus() {
  const id = props.jobId;
  if (!id) return;
  // 每轮都推进「此刻」：时间在走，即使这一帧的状态没变（进度停住时那个数会一直涨）
  nowMs.value = Date.now();
  try {
    const row = await api.get("/jobs/" + id);
    if (id !== props.jobId || !row || row.error) return;
    live.value = row;
    if (jobIsTerminal(row.status)) {
      stopPoll();
      await loadDetail();   // 终态：结果这一次才写得全
    }
  } catch (e) {
    // 单次拉取失败不提示（本地接口偶发失败很常见），下一轮自己会补上；
    // 时间线那边有连续失败的提示，这里不重复
  }
}

watch(() => [props.modelValue, props.jobId], ([open, id]) => {
  stopPoll();
  detail.value = null;
  live.value = null;
  rawOpen.value = false;
  if (!open || !id) return;
  void pullStatus();
  void loadDetail();
  timer = window.setInterval(pullStatus, 2000);
}, { immediate: true });

onUnmounted(stopPoll);

async function cancelJob() {
  try {
    await ElMessageBox.confirm(jobCancelHint(job.value.kind), "取消任务", {
      type: "warning", confirmButtonText: "取消任务", cancelButtonText: "返回",
    });
    await api.post("/jobs/" + props.jobId + "/cancel", {});
    ElMessage.success("已请求取消任务");
    await pullStatus();
    emit("changed");
  } catch (e) {
    if (e !== "cancel" && e !== "close") ElMessage.error("取消任务失败：" + e.message);
  }
}

async function retryJob() {
  const again = job.value.status === "done";
  try {
    await ElMessageBox.confirm(
      again ? "将使用相同参数再运行一次，原任务会保留。" : "将使用相同参数重新提交，原任务会保留。",
      again ? "再次运行" : "重试任务",
      { type: "warning", confirmButtonText: again ? "再次运行" : "重试", cancelButtonText: "返回" },
    );
    const created = await api.post("/jobs/" + props.jobId + "/retry", {});
    ElMessage.success("已创建新任务");
    visible.value = false;
    emit("changed", created.job_id || "");
  } catch (e) {
    if (e !== "cancel" && e !== "close") ElMessage.error("提交重试失败：" + e.message);
  }
}

async function deleteJob() {
  try {
    await ElMessageBox.confirm("删除后将移除任务记录和结果，不能恢复。", "删除任务", {
      type: "warning", confirmButtonText: "删除", cancelButtonText: "返回",
    });
    await api.del("/jobs/" + props.jobId);
    ElMessage.success("任务已删除");
    visible.value = false;
    emit("changed", "");
  } catch (e) {
    if (e !== "cancel" && e !== "close") ElMessage.error("删除任务失败：" + e.message);
  }
}
</script>

<template>
  <el-dialog v-model="visible" :title="title" width="min(880px, 92vw)" top="6vh"
             append-to-body class="task-detail-dialog" modal-class="task-detail-overlay"
             :close-on-click-modal="false">
    <div v-loading="detailLoading" class="td-body">
      <div class="td-head">
        <el-tag size="small" :type="jobStatusType(job.status)">
          {{ jobStatusLabel(job.status) }}
        </el-tag>
        <!-- 在跑时给阶段配一个呼吸点：文字说「编译中」，可一屏灰看不出它还活着 -->
        <span v-if="inFlight" class="td-pulse" />
        <span class="muted">{{ jobPhaseLabel(job.phase) }}</span>
        <span v-if="job.retry_of" class="muted">· 由 {{ job.retry_of }} 重试而来</span>
        <span class="grow" />
        <!-- 任务号是标识、不是技术细节：报障时要把它交出去，放在头上一行里 -->
        <span v-if="jobId" class="muted td-jid" :title="jobId">{{ jobId }}</span>
        <el-button v-if="inFlight" size="small" type="warning" plain @click="cancelJob">
          取消任务
        </el-button>
      </div>

      <!-- 在跑：这一段就是「进度弹窗」；结束后同一位置换成结论 -->
      <div class="td-progress">
        <!-- 数字在前、百分比让位：12 / 40 才是信息，百分比只是它的换算 -->
        <div class="td-progress-top">
          <span v-if="progressText" class="td-progress-num">{{ progressText }}</span>
          <span v-if="spentText" class="muted td-progress-spent">已跑 {{ spentText }}</span>
        </div>
        <el-progress :percentage="pct" :stroke-width="10" :show-text="false"
                     :status="jobProgressStatus(job.status)" />
        <!-- 绝对时间对分钟级跑批没用（还得自己减时钟）；这个数停住不动就是「卡住了」 -->
        <p v-if="inFlight && agoText" class="muted td-progress-text">{{ agoText }}更新</p>
      </div>

      <template v-if="summary">
        <div class="td-summary">
          <span class="chip">通过 <b>{{ summary.ok }}</b></span>
          <span class="chip">未通过 <b :class="{ bad: summary.fail > 0 }">{{ summary.fail }}</b></span>
          <span v-if="summary.changed_total" class="chip">
            相对上次变化 <b>{{ summary.changed_total }}</b> 条
          </span>
          <span v-else class="muted">相对上次无变化</span>
        </div>
        <div v-for="(w, i) in summary.warnings" :key="'warn' + i" class="td-warn">{{ w }}</div>
        <div v-if="changedItems.length" class="td-changes">
          <div class="muted td-section-title">
            状态变化（{{ summary.changed_items.length }} / {{ summary.changed_total }} 条）
          </div>
          <div v-for="(c, i) in changedItems" :key="'item' + i" class="td-change"
               :class="{ 'is-worse': isWorseHealth(c.to, healthNeedsAction.value) }">
            <span class="td-name" :title="c.name">{{ c.name || "（无名）" }}</span>
            <span class="muted td-url" :title="c.url">{{ c.url }}</span>
            <span class="muted">{{ healthLabel(c.from) }} → </span>
            <span :class="'to-' + c.to">{{ healthLabel(c.to) }}</span>
          </div>
        </div>
      </template>

      <div v-if="job.error" class="td-warn">{{ job.error }}</div>
      <!-- 回退原因不是「技术细节」：它说明这次为什么没走常驻引擎、为什么慢，
           有值就该露出来，不该等人去展开一个折叠块 -->
      <div v-if="detail && detail.daemon_fallback_reason" class="td-warn">
        {{ detail.daemon_fallback_reason }}
      </div>

      <!-- 只有 JVM 校验有执行时间线：其余 kind 后端没有事件源（见 job_timeline.py）。
           执行方式交给时间线头部：它决定这次会不会慢，与逐块明细是同一件事 -->
      <JobTimeline v-if="job.kind === 'jvm_run'" :job-id="jobId" :status="job.status"
                   :execution-mode="(detail && detail.execution_mode) || ''"
                   :execution-note="(detail && detail.execution_note) || ''"
                   :chunk-reports="(detail && detail.chunk_reports) || []" />

      <div v-if="!summary && !job.error && !inFlight" class="muted td-empty">
        这次任务没有可展示的结果。
      </div>

      <!-- 原始结果：生成/导入类任务要核对返回值时才有用，默认收起 -->
      <div v-if="detail && detail.result != null" class="td-fold">
        <button type="button" class="td-fold-toggle" :aria-expanded="rawOpen"
                @click="rawOpen = !rawOpen">
          原始结果<span class="td-fold-chevron" :class="{ 'is-open': rawOpen }">▸</span>
        </button>
        <pre v-if="rawOpen" class="td-raw">{{ JSON.stringify(detail.result, null, 2) }}</pre>
      </div>
    </div>

    <template #footer>
      <el-button v-if="jobIsTerminal(job.status)" type="danger" plain @click="deleteJob">
        删除
      </el-button>
      <el-button v-if="jobIsTerminal(job.status)" @click="retryJob">
        {{ job.status === "done" ? "再次运行" : "重试" }}
      </el-button>
      <el-button type="primary" @click="visible = false">关闭</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
/* 弹窗骨架的下一层（上一层在文件末尾的无 scoped 块）：内容区自己也是 flex
   容器，才能把剩余高度交给时间线。`flex: 1 0 auto` 的 shrink=0 是刻意的——
   结果态内容超长时不许缩，让它撑出外层的滚动条；有富余时才撑满（进行中态）。 */
.td-body {
  flex: 1 0 auto; min-height: 80px;
  display: flex; flex-direction: column;
}
/* 除时间线外都不参与伸缩：flex 的默认 shrink=1，漏了这条会把结果态的结论
   胶囊与变化清单一起压扁——不报错，只是看着不对。 */
.td-head, .td-progress, .td-summary, .td-warn, .td-changes, .td-empty, .td-fold {
  flex: 0 0 auto;
}
.td-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.td-head .grow { flex: 1 1 auto; }
/* 任务号：等宽、可整体选中（报障时要复制它）；窄屏靠省略号让位 */
.td-jid {
  font-family: Consolas, Monaco, monospace; font-size: 12px;
  max-width: 160px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  user-select: all;
}
/* 一屏灰的进度里唯一的动静：它在动，就说明引擎还活着 */
.td-pulse {
  width: 6px; height: 6px; border-radius: 50%;
  background: var(--el-color-primary);
  animation: td-pulse 1.4s ease-in-out infinite;
}
@keyframes td-pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: .25; }
}
.td-progress {
  margin-top: var(--app-space-3); padding: 10px 12px;
  background: var(--el-fill-color-lighter);
  border: 1px solid var(--el-border-color-extra-light);
  border-radius: var(--app-radius-md);
}
.td-progress-top {
  display: flex; align-items: baseline; gap: 8px;
  margin-bottom: 8px;
}
.td-progress-num {
  font-size: 20px; font-weight: 600; line-height: 1.2;
  /* 等宽数字：每 2 秒重算一次，不等宽整行会左右抖 */
  font-variant-numeric: tabular-nums;
}
.td-progress-spent { margin-left: auto; white-space: nowrap; }
.td-progress-text { margin: 6px 0 0; font-size: 12px; }
/* 结论统计是全局 `.chip` 胶囊（任务抽屉、执行方式、统计条筛选都用同一个） */
.td-summary {
  display: flex; gap: var(--app-space-2); flex-wrap: wrap;
  margin-top: var(--app-space-3);
}
/* 错误与告警从裸红字升级成警示容器：读得出来「这是一条消息」而不是普通文本 */
.td-warn {
  margin-top: 10px; padding: 8px 12px; line-height: 1.6; font-size: 13px;
  color: var(--el-color-danger); word-break: break-all;
  background: var(--el-color-danger-light-9, #fef0f0);
  border: 1px solid var(--el-color-danger-light-7, #fde2e2);
  border-radius: var(--app-radius-md);
}
.td-section-title { margin: 14px 0 6px; font-size: 12px; }
.td-changes { max-height: 40vh; overflow-y: auto; margin-top: 4px; }
.td-change {
  display: flex; gap: 7px; align-items: baseline;
  line-height: 1.8; font-size: 12px;
  /* 透明边占位：恶化行加红边时整行不会右移 */
  padding-left: 8px; border-left: 2px solid transparent;
}
.td-change + .td-change { border-top: 1px solid var(--el-border-color-extra-light); }
/* 变坏的先看：结果态真正的入口是「哪些源坏了」，不是「有几条变化」 */
.td-change.is-worse {
  border-left-color: var(--el-color-danger);
  background: var(--el-color-danger-light-9, #fef0f0);
}
.td-name {
  flex: 0 1 auto; max-width: 150px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.td-url {
  flex: 1 1 auto; min-width: 0;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
/* 配色与列表页的健康色同口径：同一个状态在两处该长一样 */
.td-change .to-ok { color: var(--el-color-success); }
.td-change .to-dead { color: var(--el-color-danger); }
.td-change .to-auth, .td-change .to-cert { color: var(--el-color-warning); }
.td-change .to-gfw, .td-change .to-pending { color: var(--el-color-info); }
.td-empty { padding: 20px 0; text-align: center; font-size: 13px; }
.td-fold { margin-top: 14px; font-size: 12px; }
/* 折叠入口是按钮：键盘可达、有焦点环；原来是 span，只有鼠标能点 */
.td-fold-toggle {
  display: inline-flex; align-items: center; gap: 4px;
  padding: 2px 6px; margin-left: -6px;
  border: none; border-radius: 4px; background: none;
  color: var(--el-color-primary); font: inherit; cursor: pointer;
}
.td-fold-toggle:hover { background: var(--el-fill-color-light); }
.td-fold-chevron { transition: transform .15s; }
.td-fold-chevron.is-open { transform: rotate(90deg); }
.td-raw {
  max-height: 300px; overflow: auto; margin-top: 8px; padding: 10px;
  background: var(--el-fill-color-light); border-radius: 4px;
  white-space: pre-wrap; font-size: 12px;
}
</style>

<style>
/* el-dialog 会 teleport 到 body：内部结构够不到 scoped（AGENTS #15），按组件名前缀单开一块。
   限高 + 内滚是本弹窗的骨架：长结果（时间线 + 失败清单）曾把弹窗撑出屏幕，
   footer 永远看不见、只能滚被遮住的页面去看（2026-10-05 实测）——现在头部/底部
   常驻，只有内容区滚。modal-class 挂在 overlay 根上，用它限定作用范围。
   **高度只有这一条链决定**：body → `.td-body` → `.job-timeline` → 时间线列表，
   每一层都要 flex + min-height: 0；中间任何一层自己按视口定高，就会出现第二根滚动条。 */
.task-detail-overlay .el-overlay-dialog { overflow: hidden; }
.task-detail-overlay .task-detail-dialog {
  max-height: 88vh;
  margin-bottom: 0;
  display: flex; flex-direction: column;
}
.task-detail-overlay .task-detail-dialog .el-dialog__header { flex: 0 0 auto; }
.task-detail-overlay .task-detail-dialog .el-dialog__body {
  flex: 1 1 auto; min-height: 0; overflow-y: auto;
  display: flex; flex-direction: column;
  /* 跑批中每 2 秒来一行：没有它，内容刚越过高度的那一刻会冒出滚动条，
     整块文字跟着横跳一下 */
  scrollbar-gutter: stable;
}
.task-detail-overlay .task-detail-dialog .el-dialog__footer { flex: 0 0 auto; }
@media (max-width: 760px) {
  .task-detail-dialog { width: 94vw !important; }
  .task-detail-overlay .task-detail-dialog { max-height: 92vh; }
}
</style>
