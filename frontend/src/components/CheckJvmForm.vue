<template>
  <div class="jvm-check-form">
    <div class="row">
      <el-tag v-if="checking" size="small" type="info">正在自检…</el-tag>
      <el-tag v-else-if="readiness && readiness.ok" size="small" type="success">环境可用</el-tag>
      <el-tag v-else-if="readiness" size="small" type="danger">环境不可用</el-tag>
      <el-button size="small" link type="primary" :loading="checking" @click="runReadiness">重新检查</el-button>
    </div>
    <el-alert v-if="readiness && !readiness.ok" type="error" :closable="false" show-icon
              style="margin: 8px 0" :title="blockReason" />
    <el-form v-if="conf" label-width="96px" size="small" style="margin: 8px 0">
      <el-form-item label="测试关键词"><el-input v-model="run.keyword" style="width: 160px" placeholder="我" /></el-form-item>
      <el-form-item label="超时 / 并发">
        <el-input-number v-model="run.timeout" :min="limits.jvm_timeout?.[0]" :max="limits.jvm_timeout?.[1]" :step="5" />
        <span class="muted" style="margin: 0 6px">秒 /</span>
        <el-input-number v-model="run.concurrency" :min="limits.jvm_concurrency?.[0]" :max="limits.jvm_concurrency?.[1]" />
      </el-form-item>
      <el-form-item label="校验深度"><el-select v-model="run.depth" style="width: 200px"><el-option v-for="d in depthOpts" :key="d.value" :value="d.value" :label="d.label" /></el-select></el-form-item>
    </el-form>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from "vue";
import { getSettings } from "../api/settings";
import { jvmReadiness } from "../api/jvm.js";
import { depthOptions } from "../utils/jvmDepth";

const emit = defineEmits(["ready"]);
const conf = ref(null);
const limits = ref({});
const depthOpts = computed(() => depthOptions(limits.value));
// 关键词等默认值只在后端 settings_store（AGENTS #8）：这里初始为空，
// 空值交上去由后端 coerce 落回默认——前端不再抄一份「我」
const run = ref({ keyword: "", timeout: 25, concurrency: 8, depth: "search" });
const readiness = ref(null);
const checking = ref(false);

const blockReason = computed(() => {
  const bad = ((readiness.value || {}).checks || []).filter((c) => !c.ok);
  const first = bad[0] || {};
  return (first.name ? first.name + "：" : "") + (first.hint || "环境自检未通过");
});

async function load() {
  emit("ready", false);
  try {
    const s = await getSettings();
    conf.value = s.values.jvm || {};
    limits.value = s.limits || {};
    run.value = {
      keyword: conf.value.keyword || s.defaults?.jvm?.keyword || "",
      timeout: conf.value.timeout ?? s.defaults?.jvm?.timeout,
      concurrency: conf.value.concurrency ?? s.defaults?.jvm?.concurrency,
      depth: conf.value.depth || s.defaults?.jvm?.depth || "search",
    };
  } catch (e) { /* 设置接口挂了就只跑自检，参数留空 */ }
  await runReadiness();
}

async function runReadiness() {
  checking.value = true;
  try { readiness.value = await jvmReadiness(); }
  catch (e) { readiness.value = { ok: false, checks: [{ name: "环境就绪接口", hint: "检查失败：" + e }] }; }
  finally { checking.value = false; emit("ready", !!readiness.value?.ok); }
}

onMounted(load);
defineExpose({ reload: load, params: () => ({ ...run.value }) });
</script>

<style scoped>
.row { display: flex; align-items: center; gap: 6px; }
</style>
