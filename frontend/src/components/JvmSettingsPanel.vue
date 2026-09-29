<template>
  <div class="jvm-panel">
    <section class="env-compact" :class="{ 'is-ok': readiness?.ok, 'is-error': readiness && !readiness.ok }">
      <div>
        <b>JVM 校验环境</b>
        <div class="env-status muted">
          <template v-if="checking">正在检查环境…</template>
          <template v-else-if="readiness">{{ readiness.ok ? "环境可用，可以开始校验" : "环境不可用，请处理下方问题" }}</template>
          <template v-else>尚未检查</template>
        </div>
      </div>
      <el-tag v-if="readiness" :type="readiness.ok ? 'success' : 'danger'" size="small">
        {{ readiness.ok ? "可用" : "不可用" }}
      </el-tag>
      <el-tag v-else size="small" type="info">待检查</el-tag>
    </section>

    <el-form label-position="top" size="small" @submit.prevent>
      <el-form-item label="App 源码目录">
        <div class="path-row">
          <el-input v-model="conf.app_repo" placeholder="legado-with-MD3 仓库根目录"
                    clearable :disabled="saving" />
          <el-button :disabled="saving || picking" :loading="picking" @click="pickAppRepo">选择</el-button>
        </div>
      </el-form-item>
      <el-form-item label="Android SDK">
        <div class="path-row">
          <el-input v-model="conf.android_sdk_dir" placeholder="留空则自动发现"
                    clearable :disabled="saving" />
          <el-button :disabled="saving || pickingSdk" :loading="pickingSdk" @click="pickAndroidSdk">选择</el-button>
        </div>
      </el-form-item>
      <el-form-item label="网络代理">
        <el-input v-model="conf.proxy" placeholder="留空表示直连" clearable :disabled="saving" />
      </el-form-item>
    </el-form>

    <div class="env-actions">
      <el-button type="primary" size="small" :loading="saving || checking" @click="saveAndCheck">
        {{ saving || checking ? "检查中…" : "保存并检查" }}
      </el-button>
      <span v-if="saving" class="muted">保存中…</span>
    </div>

    <div v-if="readiness && !readiness.ok" class="env-problem">
      <div class="problem-text">
        {{ firstFailure.name ? firstFailure.name + "：" : "" }}{{ firstFailure.hint || "环境检查未通过" }}
      </div>
      <el-button v-if="isPathFailure" size="small" link type="primary"
                 @click="firstFailure.name.includes('SDK') ? pickAndroidSdk() : pickAppRepo()">
        选择目录
      </el-button>
    </div>

    <el-button v-if="readiness" class="diagnosis-link" size="small" link type="info"
               @click="diagnosisOpen = !diagnosisOpen">
      {{ diagnosisOpen ? "收起诊断详情" : "查看诊断详情" }}
    </el-button>
    <div v-if="diagnosisOpen && readiness" class="diagnosis-detail">
      <div class="diagnosis-grid">
        <div><span class="muted">运行方式</span><span>{{ launchModeText }}</span></div>
        <div><span class="muted">Java</span><span>{{ readiness.runtime?.java?.version || "—" }} · {{ readiness.runtime?.java?.home || "未解析" }}</span></div>
        <div><span class="muted">Gradle daemon JVM</span><span>{{ readiness.runtime?.daemon_java?.version || "—" }} · {{ readiness.runtime?.daemon_java?.home || "未解析" }}</span></div>
        <div><span class="muted">项目 toolchain</span><span>{{ readiness.runtime?.toolchain_java?.version || "—" }} · {{ readiness.runtime?.toolchain_java?.home || "未解析" }}</span></div>
        <div><span class="muted">Android SDK</span><span>{{ readiness.runtime?.compile_sdk || "—" }} · {{ readiness.runtime?.android_sdk || "未解析" }}（{{ readiness.runtime?.android_sdk_source || "来源未知" }}）</span></div>
        <div><span class="muted">Gradle 用户目录</span><span>{{ readiness.runtime?.gradle_user_home || "未解析" }}</span></div>
        <div><span class="muted">Wrapper 缓存</span><span>{{ readiness.runtime?.wrapper_distribution_cache || "未就绪" }}</span></div>
      </div>
      <el-table :data="readiness.checks" row-key="id" size="small" class="diagnosis-table">
        <el-table-column label="检查项" prop="name" width="160" />
        <el-table-column label="状态" width="70">
          <template #default="{ row }"><el-tag :type="row.ok ? 'success' : 'danger'" size="small">{{ row.ok ? "通过" : "失败" }}</el-tag></template>
        </el-table-column>
        <el-table-column label="说明"><template #default="{ row }"><div>{{ row.found || row.hint }}</div><div v-if="row.detail" class="muted">{{ row.detail }}</div></template></el-table-column>
      </el-table>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted } from "vue";
import { ElMessage } from "element-plus";
import { getSettings, patchSettings } from "../api/settings";
import { jvmReadiness, pickJvmAppRepo, pickJvmAndroidSdk } from "../api/jvm.js";

const conf = reactive({ app_repo: "", android_sdk_dir: "", proxy: "" });
const readiness = ref(null);
const checking = ref(false);
const picking = ref(false);
const pickingSdk = ref(false);
const saving = ref(false);
const diagnosisOpen = ref(false);
const launchModeText = computed(() => {
  const mode = readiness.value?.runtime?.launch_mode;
  return mode === "daemon_or_gradle" ? "优先 daemon，失败时走 Gradle" : (mode || "—");
});
const firstFailure = computed(() => ((readiness.value || {}).checks || []).find((c) => !c.ok) || {});
const isPathFailure = computed(() => /源码|SDK/i.test(firstFailure.value.name || ""));

onMounted(async () => {
  try {
    const s = await getSettings();
    conf.app_repo = (s.values.jvm || {}).app_repo || "";
    conf.android_sdk_dir = (s.values.jvm || {}).android_sdk_dir || "";
    conf.proxy = (s.values.network || {}).proxy || "";
  } catch (e) { /* 设置接口挂起时仍允许用户填写配置 */ }
  await runReadiness();
});

async function pickAppRepo() {
  picking.value = true;
  try { const r = await pickJvmAppRepo(); if (!r.cancelled && r.path) conf.app_repo = r.path; }
  catch (e) { ElMessage.error("选择目录失败：" + e); }
  finally { picking.value = false; }
}
async function pickAndroidSdk() {
  pickingSdk.value = true;
  try { const r = await pickJvmAndroidSdk(); if (!r.cancelled && r.path) conf.android_sdk_dir = r.path; }
  catch (e) { ElMessage.error("选择 Android SDK 目录失败：" + e); }
  finally { pickingSdk.value = false; }
}
async function runReadiness() {
  checking.value = true;
  try { readiness.value = await jvmReadiness(); }
  catch (e) { readiness.value = { ok: false, checks: [{ name: "环境检查", hint: "检查失败：" + e }] }; }
  finally { checking.value = false; }
}
async function save() {
  saving.value = true;
  try {
    const s = await patchSettings({ jvm: { app_repo: conf.app_repo, android_sdk_dir: conf.android_sdk_dir }, network: { proxy: conf.proxy } });
    conf.app_repo = (s.values.jvm || {}).app_repo || "";
    conf.android_sdk_dir = (s.values.jvm || {}).android_sdk_dir || "";
    conf.proxy = (s.values.network || {}).proxy || "";
    await runReadiness();
    ElMessage.success("已保存并完成环境检查");
  } catch (e) { ElMessage.error("保存失败：" + e); }
  finally { saving.value = false; }
}
async function saveAndCheck() { await save(); }
</script>

<style scoped>
.jvm-panel { padding-bottom: 8px; }
.env-compact { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 12px; margin-bottom: 14px; border: 1px solid var(--el-border-color-lighter); border-left: 3px solid var(--el-color-info); border-radius: 6px; background: var(--el-fill-color-lighter); }
.env-compact.is-ok { border-left-color: var(--el-color-success); }
.env-compact.is-error { border-left-color: var(--el-color-danger); }
.env-status { margin-top: 3px; font-size: 12px; }
.path-row { display: flex; gap: 8px; width: 100%; }
.path-row .el-input { min-width: 0; }
.env-actions { display: flex; align-items: center; gap: 8px; margin: 4px 0 12px; }
.env-problem { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 8px 10px; margin: 6px 0; color: var(--el-color-danger); border: 1px solid var(--el-color-danger-light-7); border-radius: 4px; background: var(--el-color-danger-light-9); font-size: 13px; }
.diagnosis-link { padding: 0; }
.diagnosis-detail { margin-top: 8px; padding: 8px; border-top: 1px solid var(--el-border-color-lighter); }
.diagnosis-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px 16px; font-size: 12px; }
.diagnosis-grid > div { display: flex; flex-direction: column; gap: 2px; min-width: 0; word-break: break-word; }
.diagnosis-table { margin-top: 10px; }
@media (max-width: 600px) { .diagnosis-grid { grid-template-columns: 1fr; } .env-problem { align-items: flex-start; flex-direction: column; } }
</style>
