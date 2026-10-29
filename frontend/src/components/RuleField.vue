<!-- 规则行：输入框 + 就地快验 / AI 补全。
     编辑页每一行规则都套这个壳——**能力**在这里，字段清单仍归调用方
     （编辑页要编的是整份规则，比 AI 补全字段表宽：`searchUrl` / `imageStyle` 那些
     不在 AI 词表里，但它们同样是用户要改的规则）。

     为什么不做成「按字段表平铺」：`RULE_FIELD_DEFS` 是 **AI 补全的字段表**，
     照它平铺会**丢掉若干可编辑字段**——那是静默的功能缩减，不是重构。

     快验与 AI 都要求工作区记着一次调试（材料挂在那个任务行上）：
     没有就禁用并说清缺什么，不静默失败（AGENTS #4）。 -->
<script setup>
import { computed, inject, ref } from "vue";

import { SOURCE_WORKSPACE_KEY } from "../composables/useSourceWorkspace";
import { useAgentRun } from "../composables/useAgentRun";
import { RULE_FIELD_DEFS } from "../utils/steps";
import { autofillKeyOf } from "../utils/agentRun";
import { verifyFieldRule } from "../utils/ruleVerify";

const props = defineProps({
  //: 规则原文（v-model）
  modelValue: { type: String, default: "" },
  //: 规则字段路径，如 `ruleSearch.bookList`——快验与 AI 都按它认字段
  field: { type: String, required: true },
  //: 步骤名（`search` / `bookUrl` / `toc` / `content`）
  step: { type: String, required: true },
  label: { type: String, default: "" },
  placeholder: { type: String, default: "" },
  type: { type: String, default: "input" },   // input | textarea
  rows: { type: Number, default: 3 },
});
const emit = defineEmits(["update:modelValue"]);

const workspace = inject(SOURCE_WORKSPACE_KEY);
if (!workspace) throw new Error("RuleField must be mounted under MainLayout");

//: 词表里有没有这一行，决定**能不能快验**（`verify: "none"` 的行取的不是单值，
//: 拿单值口径去验会得出方向相反的结论）。没有这一行的规则照样可编，只是不给按钮。
const def = computed(() => RULE_FIELD_DEFS.find((d) => d.field === props.field) || null);
//: AI 认字段用的是后端那张**落点表的反查**（`AUTOFILL_RULE_FIELDS`，与
//: `core.autofill.FILL_SPECS` 逐词比对钉着）。用 defs 的 `key` 会放进后端不认的字段
//: （如正文的下一页链接），那边只会回一句补不了——两件事的前提不同，不当成同一件事。
const aiKey = computed(() => autofillKeyOf(props.step, props.field));
//: 材料：快验与 AI 都吃它（就是那次调试的任务号）
const jobId = computed(() => String((workspace.state.debug || {}).jobId || ""));
const busy = ref("");
const fact = ref(null);      // 快验结果 {count, values, error}
const aiNote = ref("");      // AI 的一句话结果

function onInput(value) {
  emit("update:modelValue", value);
  // 规则一改，上一次的结论就不作数了（结论描述的是旧规则）
  fact.value = null;
  aiNote.value = "";
}

async function onVerify() {
  if (busy.value) return;
  busy.value = "verify";
  fact.value = null;
  try {
    fact.value = await verifyFieldRule({
      jobId: jobId.value, step: props.step, field: props.field, rule: props.modelValue,
    });
  } finally {
    busy.value = "";
  }
}

/** AI 只补这一个字段：写完直接落进输入框，并说清它试了没试成。 */
async function onAi() {
  const key = aiKey.value;
  if (!key || busy.value) return;
  busy.value = "ai";
  aiNote.value = "";
  fact.value = null;
  try {
    // AI 补全是**任务**：`submit` 起任务，`done` 等终态并给出结果体
    const run = useAgentRun();
    const { done } = await run.submit({
      debugJobId: jobId.value, step: props.step, only: [key],
    });
    const body = await done;
    const rows = (body && body.fields) || [];
    const row = rows.find((r) => r.field === key) || rows[0] || null;
    if (!row || !row.rule) {
      aiNote.value = "没给出可用的规则";
      return;
    }
    emit("update:modelValue", String(row.rule));
    // 「引擎没验过」要如实说：AI 写的规则在快验通过前只是候选（AGENTS #3）
    const checked = row.try_result || {};
    if (row.pending_engine || checked.unfaithful) aiNote.value = "AI 给出候选，未能快验，请重调本步";
    else if (checked.ok === false) aiNote.value = "AI 给出候选，快验没通过";
    else aiNote.value = "AI 已填入，快验通过";
  } catch (e) {
    aiNote.value = String((e && e.message) || e);
  } finally {
    busy.value = "";
  }
}

//: 字段表标了 `verify: "none"` 的行**不给验证按钮**：那些规则取的不是单值
//: （如正文的下一页链接是一组链接），拿单值口径去验会得出方向相反的结论
const noVerify = computed(() => !!def.value && def.value.verify === "none");
const showVerify = computed(() => !!def.value && !noVerify.value);
const verifyDisabled = computed(() => !jobId.value || !String(props.modelValue || "").trim());
const verifyWhy = computed(() => (!jobId.value
  ? "先跑一次调试：快验要用那次调试的页面材料"
  : "这一行还没有规则"));
</script>

<template>
  <el-form-item :label="label">
    <el-input
      :model-value="modelValue"
      :type="type === 'textarea' ? 'textarea' : 'text'"
      :rows="rows"
      :autosize="type === 'textarea' ? { minRows: rows, maxRows: 8 } : false"
      :placeholder="placeholder"
      @update:model-value="onInput"
    />
    <div class="rf-bar">
      <el-tooltip v-if="showVerify" :disabled="!verifyDisabled" :content="verifyWhy" placement="top">
        <el-button v-if="showVerify" size="small" link type="primary"
                   :loading="busy === 'verify'" :disabled="verifyDisabled"
                   @click="onVerify">验证</el-button>
      </el-tooltip>
      <el-tooltip v-if="aiKey" :disabled="!!jobId" content="先跑一次调试：AI 补全要用那次调试的页面材料" placement="top">
        <el-button size="small" link
                   :loading="busy === 'ai'" :disabled="!jobId"
                   @click="onAi">AI 补全</el-button>
      </el-tooltip>
      <!-- 快验结论：值本身 / 条数 / 原因。原因原样显示——「测不了」与「取不到」
           是两个方向相反的结论，压成一句「失败」等于把原因丢掉（AGENTS #4） -->
      <span v-if="fact" class="rf-fact" :class="{ 'is-bad': !!fact.error }">
        <template v-if="fact.error">{{ fact.error }}</template>
        <template v-else-if="fact.count === 1">{{ fact.values[0] }}</template>
        <template v-else>{{ fact.count }} 条<span v-if="fact.values.length" class="rf-sample">（{{ fact.values[0] }}）</span></template>
      </span>
      <span v-if="aiNote" class="rf-fact muted">{{ aiNote }}</span>
      <slot />
    </div>
  </el-form-item>
</template>

<style scoped>
.rf-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  width: 100%;
  margin-top: 2px;
}
.rf-fact {
  font-size: 12px;
  color: var(--el-color-success-dark-2);
  word-break: break-all;
}
.rf-fact.is-bad { color: var(--el-color-danger); }
.rf-sample { color: var(--el-text-color-secondary); }
</style>