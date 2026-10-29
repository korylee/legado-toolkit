<!-- AI 补全的**内联过程条**：跑的时候看得见，且不挡住手上的活儿。

     原来一提交就弹遮罩弹窗——用户在等的时候既不能改规则、也看不到模型在做什么，
     只有一个「进行中」。这里把过程摊在规则区下方：逐字段四态 + 最近一次模型往返。
     要翻每一次调用的提示词与回答，仍走明细弹窗（那是深入视图，不是必经之路）。

     数据与弹窗**同一份**（`useAgentRun` 是模块级单例）：两处不会各说一套。 -->
<script setup>
import { computed, onUnmounted, ref, watch } from "vue";

import { jobLlmCalls } from "../api/rules";
import { useAgentRun } from "../composables/useAgentRun";
import { STATE_TEXT, STATE_TONE } from "../utils/agentRun";
import { STEP_LABELS } from "../utils/steps";

const props = defineProps({
  //: 当前这次调试的任务号。`useAgentRun` 是**模块级单例**——不拿它收口的话，
  //: 换一个源之后这条还会显示上一轮的字段结论（数据没错，但答的不是这个问题）。
  debugJobId: { type: String, default: "" },
});
const emit = defineEmits(["detail"]);

const run = useAgentRun();
//: 模型调用账的增量游标（行号）。**只拉增量**：提示词几十 KB，每轮重拉等于让等待背历史。
const cursor = ref(0);
const calls = ref([]);
let timer = null;

const rows = computed(() => run.rows.value || []);
const active = computed(() => !!run.jobId.value
  && (!props.debugJobId || run.debugJobId.value === props.debugJobId));
const stepName = computed(() => STEP_LABELS[run.step.value] || run.step.value || "");
//: 最近一次往返：它是「模型正在干什么」最直接的证据
const latest = computed(() => calls.value[calls.value.length - 1] || null);
//: 已落定的条数：进度感来自它，不是百分比（字段数少，百分比会一跳一跳）
const settled = computed(() => rows.value.filter(
  (r) => r.state === "accepted" || r.state === "pending_engine" || r.state === "failed").length);

async function poll() {
  const id = run.jobId.value;
  if (!id) return;
  try {
    const out = await jobLlmCalls(id, cursor.value);
    const items = (out && out.items) || [];
    if (items.length) calls.value = calls.value.concat(items);
    if (out && out.cursor != null) cursor.value = Number(out.cursor) || cursor.value;
  } catch (e) {
    // 账本是排障材料：读不到就不显示，不拦过程条也不假装没有
  }
}

function stop() {
  if (timer) { clearInterval(timer); timer = null; }
}
watch([() => run.jobId.value, () => run.running.value, () => props.debugJobId], ([id, running]) => {
  stop();
  if (!id || !active.value) { calls.value = []; cursor.value = 0; return; }
  void poll();
  // 跑的时候盯着；跑完再取一次就收（结果体与逐字段结论那时才全）
  if (running) timer = setInterval(poll, 2500);
  else void poll();
}, { immediate: true });
onUnmounted(stop);
</script>

<template>
  <div v-if="active" class="al">
    <div class="al-head">
      <span class="al-title">{{ stepName }} · AI 补全</span>
      <span class="al-count muted">{{ settled }}/{{ rows.length }} 个字段有结论</span>
      <el-button link size="small" @click="emit('detail')">看每次调用</el-button>
    </div>
    <div class="al-fields">
      <span v-for="row in rows" :key="row.field" class="al-field">
        <el-tag size="small" effect="plain" :type="STATE_TONE[row.state] || 'info'">
          {{ STATE_TEXT[row.state] || row.state }}
        </el-tag>
        <span class="al-label">{{ row.label }}</span>
        <span v-if="row.tries > 1" class="al-tries muted">第 {{ row.tries }} 轮</span>
      </span>
    </div>
    <!-- 模型在跟谁说话：提示词规模 + 这次输出开头。全文在明细弹窗里按 seq 取 -->
    <div v-if="latest" class="al-call muted">
      <span>第 {{ latest.seq }} 次调用</span>
      <span v-if="latest.prompt_chars">· 提示词 {{ latest.prompt_chars }} 字符</span>
      <span v-if="latest.response_head" class="al-response">· {{ latest.response_head }}</span>
    </div>
  </div>
</template>

<style scoped>
.al {
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  padding: 8px 10px;
  margin: 8px 0;
  background: var(--el-fill-color-blank);
}
.al-head { display: flex; align-items: center; gap: 10px; margin-bottom: 6px; }
.al-title { font-size: 12px; font-weight: 600; }
.al-count { font-size: 12px; }
.al-fields { display: flex; flex-wrap: wrap; gap: 10px; }
.al-field { display: inline-flex; align-items: center; gap: 4px; }
.al-label { font-size: 12px; }
.al-tries { font-size: 11px; }
.al-call { font-size: 12px; margin-top: 6px; }
.al-response {
  display: inline-block;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  vertical-align: bottom;
}
</style>