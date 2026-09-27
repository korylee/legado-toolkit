<!-- 调试抽屉（第三期后只剩薄壳）：本体在 DebugWorkbench.vue——弹框的
     「查看证据」已改为跳工作台路由，这个壳暂无调用方，留着给一个提交周期
     做对照后删（TODO ux-debug-shell 的收尾）。 -->
<script setup>
import { computed } from "vue";
import DebugWorkbench from "./DebugWorkbench.vue";

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  initialStep: { type: String, default: "" },
  rules: { type: Object, default: () => ({}) },
  sourceType: { type: Number, default: 0 },
  enabledCookieJar: { type: Boolean, default: false },
  source: { type: Object, default: null },
  entryError: { type: String, default: "" },
});
const emit = defineEmits(["update:modelValue", "goto", "applyRule", "rerunFrom"]);
const visible = computed({
  get: () => props.modelValue,
  set: (v) => emit("update:modelValue", v),
});
</script>

<template>
  <el-drawer v-model="visible" size="72%" destroy-on-close>
    <template #header>
      <span>调试</span>
    </template>
    <DebugWorkbench :initial-step="initialStep" :rules="rules"
                    :source-type="sourceType" :enabled-cookie-jar="enabledCookieJar"
                    :source="source" :entry-error="entryError"
                    @goto="(v) => emit('goto', v)"
                    @apply-rule="(v) => emit('applyRule', v)"
                    @rerun-from="(v) => emit('rerunFrom', v)" />
  </el-drawer>
</template>
