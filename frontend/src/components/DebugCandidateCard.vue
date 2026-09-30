<!-- 候选卡片的**唯一布局**：算法候选、自动挑选、AI 候选、点选候选共用一份。
     判定不在这里——意图、规则、示例、计数都来自 `utils/debugCandidate` 的
     `candidateView()`；本组件只负责把它们排开：桌面「左内容 + 右操作」两列、
     窄屏单列。原先四处各写一遍 flex 行，长规则与示例必然互相撑宽。 -->
<script setup>
import { computed } from "vue";

const props = defineProps({
  //: `candidateView()` 的产物（intent / kind / rule / examples / count / unique …）
  card: { type: Object, required: true },
  //: `[{key, label, type, disabled, loading}]`——点击把 key 回传父组件执行
  actions: { type: Array, default: () => [] },
  //: 点选时标出正在预览的那条
  highlighted: { type: Boolean, default: false },
});
const emit = defineEmits(["action"]);

const examples = computed(
  () => (props.card.examples || []).join("  |  ") || "没有取到示例",
);
</script>

<template>
  <div class="candidate-card" :class="{ 'is-active': highlighted }">
    <div class="candidate-card-tags">
      <el-tag size="small" type="info">意图：{{ card.intent }}</el-tag>
      <el-tag size="small" type="info">{{ card.kind }}</el-tag>
      <slot name="tags" />
    </div>
    <code class="candidate-card-rule">{{ card.rule }}</code>
    <div class="candidate-card-meta">
      <span class="muted">示例：{{ examples }}</span>
      <slot name="meta" />
    </div>
    <div v-if="actions.length" class="candidate-card-actions">
      <el-button v-for="a in actions" :key="a.key" size="small" plain
                 :type="a.type || 'primary'"
                 :disabled="!!a.disabled" :loading="!!a.loading"
                 @click="emit('action', a.key)">{{ a.label }}</el-button>
    </div>
  </div>
</template>

<style scoped>
/* 两列栅格：左列内容、右列操作。内容列用 minmax(0, 1fr) 才能让长规则换行，
   否则栅格项的最小内容宽度会把卡片撑出容器 */
.candidate-card {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  grid-template-areas:
    "tags   actions"
    "rule   actions"
    "meta   actions";
  gap: var(--app-space-1) var(--app-space-3);
  padding: var(--app-space-2) 0;
  min-width: 0;
}
.candidate-card + .candidate-card { border-top: 1px solid var(--app-border-light); }
.candidate-card-tags {
  grid-area: tags;
  display: flex; align-items: center; flex-wrap: wrap; gap: 6px;
  min-width: 0;
}
.candidate-card-rule {
  grid-area: rule;
  font-family: Consolas, Monaco, monospace;
  font-size: 12px;
  min-width: 0; max-width: 100%;
  overflow-wrap: anywhere; word-break: break-word; white-space: normal;
}
.candidate-card-meta {
  grid-area: meta;
  display: flex; flex-wrap: wrap; gap: 2px 10px;
  min-width: 0;
  font-size: 12px;
}
.candidate-card-actions {
  grid-area: actions;
  display: flex; flex-wrap: wrap; align-items: flex-start; justify-content: flex-end;
  gap: 6px;
}
.candidate-card.is-active { border-left: 3px solid var(--app-primary); padding-left: var(--app-space-2); }

@media (max-width: 720px) {
  .candidate-card {
    grid-template-columns: minmax(0, 1fr);
    grid-template-areas: "tags" "rule" "meta" "actions";
  }
  .candidate-card-actions { justify-content: flex-start; }
  /* 窄屏：动作**一行内均分**，不各占一整行（通栏按钮会把卡片拉成一串色块，
     而且两个动作哪个是主看不清）；放不下才换行 */
  .candidate-card-actions .el-button { flex: 1 1 0; min-width: 84px; }
}
</style>
