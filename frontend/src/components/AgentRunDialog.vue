<!--
  AI 补全明细弹窗：**一轮 AI 补全自己的明细**，不复用任务明细弹窗（TaskDetailDialog）。

  为什么不复用：任务明细弹窗的骨架是「通过 / 未通过 + 变化清单 + 执行时间线」，而
  `agent_loop` 的结果体是 `{step, fields, budget}`——在那副骨架里它既没有摘要
  （判据要 `checked` 是数字），真正的 AI 事实（逐字段四态、模型调用账）反而被塞进
  两层折叠。这里照它**实际在回答的问题**排：这一轮在补哪几步的哪些字段、每个字段
  补上了没有、为什么没补上、模型问了几次、还能做什么。

  状态一律来自 `composables/useAgentRun`（观测只有一条：useJobs 的共享 SSE）。
  本组件不取数、不开定时器、不做第二次归并。
-->
<script setup>
import { computed, onUnmounted, ref } from "vue";
import { ElMessage } from "element-plus";

import { useAgentRun } from "../composables/useAgentRun";
import { jobLlmCallDetail, jobLlmCalls } from "../api/rules";
import { agentBudgetNotes, fieldLabelOf, retryFields } from "../utils/agentRun";
import { STEP_LABELS } from "../utils/steps";
import { jobStatusLabel, jobStatusType } from "../utils/jobs";

const props = defineProps({
  /** 点「再补 N 个」时怎么重启一轮：由调用方给（它知道 debug_job_id 与这一步） */
  rerun: { type: Function, default: null },
});
const emit = defineEmits(["close"]);

const run = useAgentRun();
const visible = ref(false);
const mode = ref("run"); // run = 这一轮（在跑/结果）；ledger = 模型调用账
const editOpen = ref(0);
const editDetail = ref(null);
const calls = ref([]);
const callsError = ref("");
const retrying = ref(false);

const stepLabel = computed(() => STEP_LABELS[run.step.value] || run.step.value || "");
const status = computed(() => String(run.job.value.status || ""));
const rows = computed(() => run.rows.value);
const pending = computed(() => retryFields(rows.value));
const notes = computed(() => agentBudgetNotes(run.result.value));

const STATE_TEXT = {
  accepted: "已写入",
  failed: "取不到",
  proposed: "验证中",
  pending: "还没轮到",
};
//: Element Plus 的 el-tag 只认 primary|success|info|warning|danger 五档；
//: 传空串是**未定义行为**（渲染成默认色还是原样写到 class 上，看版本）。
//: 「验证中」没有语义词，落 info（中性）——它既不是成功也不是失败。
const STATE_TONE = {
  accepted: "success",
  failed: "danger",
  proposed: "info",
  pending: "info",
};

/** 打开**这一轮**（调用方刚提交或刚拿回结果时用它）。 */
async function openRun() {
  visible.value = true;
  mode.value = "run";
  await loadCalls();
}

/** 打开一条已有的 AI 任务（任务中心点进来 / 刷新后重挂）。 */
async function openJob(jobId) {
  if (!jobId) return;
  visible.value = true;
  mode.value = "run";
  editOpen.value = 0;
  editDetail.value = null;
  await run.open(jobId);
  await loadCalls();
}

function close() {
  visible.value = false;
  emit("close");
}
defineExpose({ openRun, openJob, close });

const title = computed(() => {
  const where = stepLabel.value ? `${stepLabel.value} · ` : "";
  return run.running.value ? `${where}AI 补全进行中` : "AI 补全明细";
});

async function loadCalls() {
  callsError.value = "";
  const id = run.jobId.value;
  if (!id) {
    calls.value = [];
    return;
  }
  try {
    const out = await jobLlmCalls(id);
    calls.value = (out && out.items) || [];
  } catch (e) {
    // 账本是排障材料：读不到就说读不到，不拦明细、也不假装没有
    calls.value = [];
    callsError.value = "模型调用账读取失败：" + String((e && e.message) || e);
  }
}

async function openCall(seq) {
  if (editOpen.value === seq) {
    editOpen.value = 0;
    editDetail.value = null;
    return;
  }
  editOpen.value = seq;
  editDetail.value = null;
  try {
    editDetail.value = await jobLlmCallDetail(run.jobId.value, seq);
  } catch (e) {
    editDetail.value = { error: String((e && e.message) || e) };
  }
}

/** 再补没补上的那几个字段：范围从逐字段四态取（弹窗内唯一一份状态）。 */
async function retryPending() {
  const targets = pending.value;
  if (!targets.length || retrying.value) return;
  retrying.value = true;
  try {
    if (typeof props.rerun === "function") {
      await props.rerun(targets);
    } else {
      ElMessage.warning("这里没有调试上下文，没法重跑");
    }
    await loadCalls();
  } finally {
    retrying.value = false;
  }
}

/** 切「这一轮 / 模型调用账」时账本按需取（打开弹窗时那一次可能还没有调用）。 */
async function switchMode(next) {
  mode.value = next;
  if (next === "ledger") await loadCalls();
}

onUnmounted(() => {});
</script>

<template>
  <el-dialog
    v-model="visible"
    :title="title"
    width="min(760px, 94vw)"
    top="8vh"
    append-to-body
    class="agent-run-dialog"
    modal-class="agent-run-overlay"
    :close-on-click-modal="false"
    @closed="close"
  >
    <div class="ar-body">
      <!-- 头：这一轮在补什么 + 状态。字段集来自 ai_started（后端给的真事实） -->
      <div class="ar-head">
        <el-tag v-if="status" size="small" :type="jobStatusType(status)">
          {{ jobStatusLabel(status) }}
        </el-tag>
        <span v-else-if="run.running.value" class="ar-pulse" />
        <span class="muted">
          {{ stepLabel ? stepLabel + " · " : "" }}{{ rows.length }} 个字段
        </span>
        <span class="grow" />
        <span v-if="run.jobId.value" class="muted ar-jid" :title="run.jobId.value">
          {{ run.jobId.value }}
        </span>
        <el-button
          v-if="run.running.value"
          size="small"
          type="warning"
          plain
          @click="run.cancel()"
        >取消补全</el-button>
      </div>

      <!-- 停止原因 / 失败原因：原样，不压成一句「失败」（AGENTS #4） -->
      <div v-if="run.error.value" class="ar-warn">{{ run.error.value }}</div>
      <div v-if="run.detailError.value" class="ar-warn">{{ run.detailError.value }}</div>
      <p v-if="run.summary.value" class="muted ar-summary">{{ run.summary.value }}</p>

      <!-- 主：逐字段四态。一列到底——用户盯的就是「补上了几个、哪个没补上」 -->
      <div v-if="rows.length" class="ar-rows">
        <div v-for="row in rows" :key="row.field" class="ar-row">
          <span class="ar-name">{{ row.label }}</span>
          <el-tag size="small" :type="STATE_TONE[row.state]" effect="plain">
            {{ STATE_TEXT[row.state] }}
          </el-tag>
          <span v-if="row.state === 'accepted'" class="ar-rule" :title="row.rule">
            {{ row.rule }}
          </span>
          <span v-else-if="row.state === 'failed'" class="ar-reason" :title="row.error">
            {{ row.error || "原因未记录" }}
          </span>
          <span v-else class="muted ar-reason">
            {{ row.state === "proposed" ? "已提议，还在验证" : "这一轮还没轮到它" }}
          </span>
          <span class="grow" />
          <span v-if="row.state === 'accepted' && row.count" class="muted ar-count">
            {{ row.count }} 条
          </span>
        </div>
      </div>
      <div v-else-if="!run.running.value" class="muted ar-empty">
        这一轮没有记录到字段。
      </div>

      <!-- 模型调用账：摘要一行、全文按 seq 点开才取（排查「它为什么这么说」） -->
      <div class="ar-tabs">
        <el-radio-group :model-value="mode" size="small" @update:model-value="switchMode">
          <el-radio-button value="run">这一轮</el-radio-button>
          <el-radio-button value="ledger">模型调用账</el-radio-button>
        </el-radio-group>
      </div>
      <template v-if="mode === 'ledger'">
        <p v-if="callsError" class="ar-warn">{{ callsError }}</p>
        <p v-else-if="!calls.length" class="muted ar-empty">这一轮没有调用模型。</p>
        <div v-for="call in calls" :key="call.seq" class="ar-call">
          <button type="button" class="ar-call-toggle" @click="openCall(call.seq)">
            #{{ call.seq }} {{ call.kind }} · {{ (call.fields || []).map(fieldLabelOf).join("、") || "—" }}
            <span class="ar-chevron" :class="{ 'is-open': editOpen === call.seq }">▸</span>
          </button>
          <span class="muted">
            {{ call.model || "" }}
            <template v-if="call.usage && call.usage.prompt_tokens">
              · 输入 {{ call.usage.prompt_tokens }} / 输出
              {{ call.usage.completion_tokens || 0 }} tokens
            </template>
            · 提示词 {{ call.prompt_chars }} 字符
          </span>
          <p v-if="call.error" class="ar-reason">{{ call.error }}</p>
          <p v-else-if="call.response_head" class="muted ar-reason">
            模型输出：{{ call.response_head }}
          </p>
          <div v-if="editOpen === call.seq" class="ar-call-detail">
            <p v-if="!editDetail" class="muted">正在取全文…</p>
            <template v-else-if="editDetail.error">
              <p class="ar-reason">{{ editDetail.error }}</p>
            </template>
            <template v-else>
              <div class="muted ar-section">发出去的提示词</div>
              <pre class="ar-raw">{{ editDetail.prompt }}</pre>
              <div class="muted ar-section">模型原样输出</div>
              <pre class="ar-raw">{{ editDetail.response }}</pre>
            </template>
          </div>
        </div>
      </template>
      <div v-if="notes.length && mode === 'run'" class="ar-notes">
        <div v-for="(n, i) in notes" :key="i" class="muted">{{ n }}</div>
      </div>
    </div>

    <template #footer>
      <!-- 没有可补的字段 = 没有这个动作：不渲染，也不置灰 -->
      <el-button
        v-if="pending.length && !run.running.value"
        type="primary"
        plain
        :loading="retrying"
        @click="retryPending"
      >再补这 {{ pending.length }} 个字段</el-button>
      <el-button @click="close">关闭</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
/* 内容区自己排：逐字段一列，模型调用账在下面按需展开 */
.ar-body { display: flex; flex-direction: column; gap: var(--app-space-2); }
.ar-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.ar-head .grow { flex: 1 1 auto; }
.ar-jid {
  font-family: Consolas, Monaco, monospace; font-size: 12px;
  max-width: 150px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  user-select: all;
}
/* 在跑时的唯一动静：它在动就说明这一轮还活着 */
.ar-pulse {
  width: 6px; height: 6px; border-radius: 50%;
  background: var(--el-color-primary);
  animation: ar-pulse 1.4s ease-in-out infinite;
}
@keyframes ar-pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: .25; }
}
.ar-warn {
  padding: 8px 12px; line-height: 1.6; font-size: 13px; word-break: break-all;
  color: var(--el-color-danger);
  background: var(--el-color-danger-light-9, #fef0f0);
  border: 1px solid var(--el-color-danger-light-7, #fde2e2);
  border-radius: var(--app-radius-md);
}
.ar-summary { margin: 0; font-size: 12px; }
.ar-rows { display: flex; flex-direction: column; }
.ar-row {
  display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap;
  padding: 6px 0; font-size: 13px;
}
.ar-row + .ar-row { border-top: 1px solid var(--el-border-color-extra-light); }
.ar-row .grow { flex: 1 1 auto; }
.ar-name { flex: 0 0 auto; min-width: 84px; font-weight: 500; }
.ar-rule {
  min-width: 0; max-width: 46%;
  font-family: Consolas, Monaco, monospace; font-size: 12px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.ar-reason {
  min-width: 0; max-width: 62%; overflow: hidden; text-overflow: ellipsis;
  white-space: nowrap; font-size: 12px;
}
.ar-count { font-size: 12px; white-space: nowrap; }
.ar-empty { padding: 16px 0; text-align: center; font-size: 13px; }
.ar-tabs { margin-top: var(--app-space-2); }
.ar-call { margin: 8px 0; font-size: 12px; }
.ar-call-toggle {
  display: inline-flex; align-items: center; gap: 4px;
  padding: 2px 6px; margin-left: -6px;
  border: none; border-radius: 4px; background: none;
  color: var(--el-color-primary); font: inherit; cursor: pointer;
}
.ar-call-toggle:hover { background: var(--el-fill-color-light); }
.ar-chevron { transition: transform .15s; }
.ar-chevron.is-open { transform: rotate(90deg); }
.ar-call-detail { margin-top: 4px; }
.ar-section { margin: 8px 0 4px; }
.ar-raw {
  max-height: 260px; overflow: auto; margin: 0; padding: 10px;
  background: var(--el-fill-color-light); border-radius: 4px;
  white-space: pre-wrap; font-size: 12px;
}
.ar-notes { margin-top: 6px; font-size: 12px; }
</style>

<style>
/* el-dialog 会 teleport 到 body：内部结构够不到 scoped（AGENTS #15），按前缀单开一块。
   限高 + 内滚：模型调用账的全文可能很长，头部/footer 常驻。 */
.agent-run-overlay .el-overlay-dialog { overflow: hidden; }
.agent-run-overlay .agent-run-dialog {
  max-height: 84vh;
  display: flex; flex-direction: column;
}
.agent-run-overlay .agent-run-dialog .el-dialog__header { flex: 0 0 auto; }
.agent-run-overlay .agent-run-dialog .el-dialog__body {
  flex: 1 1 auto; min-height: 0; overflow-y: auto;
  scrollbar-gutter: stable;
}
.agent-run-overlay .agent-run-dialog .el-dialog__footer { flex: 0 0 auto; }
@media (max-width: 760px) {
  .agent-run-dialog { width: 94vw !important; }
  .agent-run-overlay .agent-run-dialog { max-height: 90vh; }
}
</style>
