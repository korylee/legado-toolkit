<script setup>
// 设置抽屉：只负责外壳与页签，各页签内容在各自面板组件里。
import { ref, computed } from "vue";
import LLMSettingsPanel from "./LLMSettingsPanel.vue";
import JvmSettingsPanel from "./JvmSettingsPanel.vue";

const props = defineProps({ modelValue: { type: Boolean, default: false } });
const emit = defineEmits(["update:modelValue"]);
const visible = computed({ get: () => props.modelValue, set: (v) => emit("update:modelValue", v) });
const activeTab = ref("jvm");
</script>

<template>
  <el-drawer v-model="visible" title="设置" direction="rtl" size="640px" destroy-on-close>
    <el-tabs v-model="activeTab">
      <el-tab-pane label="校验环境" name="jvm" lazy>
        <JvmSettingsPanel />
      </el-tab-pane>
      <el-tab-pane label="模型" name="llm" lazy>
        <LLMSettingsPanel />
      </el-tab-pane>
    </el-tabs>
  </el-drawer>
</template>
