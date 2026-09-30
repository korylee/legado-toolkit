<!-- 调试工作台页面：#/debug/:url。运行态与结果全在 useDebugSession（单例）。 -->
<script setup>
import { ref, computed, onMounted, onUnmounted } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ElMessage, ElMessageBox } from "element-plus";
import DebugWorkbench from "../components/DebugWorkbench.vue";
import SourceFields from "../components/SourceFields.vue";
import { useDebugSession, DEBUG_TARGETS, DEBUG_CACHE_MODES, STEP_TARGETS } from "../composables/useDebugSession";
import { getDetail, saveSource } from "../api/sources";
import { ensureTagMeta, splitSystemUser, sourceTypes } from "../utils/tags";

const route = useRoute();
const router = useRouter();
const {
  source, sourceDirty, markSourceSaved, setSource, updateSourceField, result, running, elapsed, budget,
  channel, target, query, cacheMode, host,
  env, envLoading, envTitle,
  preflightState,
  startRun, cancelRun, confirmPush, loadEnvironment,
} = useDebugSession();

const loading = ref(false);
const settingsOpen = ref(false);
const rawOpen = ref(false);
const settingsJson = ref("");
const settingsTags = ref("");
const userTags = ref([]);
const sysLocked = ref(false);

const name = computed(() => (source.value || {}).bookSourceName || "（无名）");
const url = computed(() => (source.value || {}).bookSourceUrl || "");
const hasExploreConfig = computed(() => !!String((source.value || {}).exploreUrl || "").trim());
const currentTarget = computed(() => DEBUG_TARGETS.find((t) => t.value === target.value) || DEBUG_TARGETS[0]);
//: 入口层在有结果之后收成一行：结果一出来，视线该落在步骤卡片上，
//: 通道/缓存/IP 这些**本次运行参数**折进「调试选项」（AGENTS #68：动作与结果同屏）
const advancedOpen = ref(false);
const entryCompact = computed(() => !!result.value && !advancedOpen.value);
const channelLabel = computed(() => (channel.value === "app" ? "连 App" : "本机引擎"));
const cacheLabel = computed(() => (DEBUG_CACHE_MODES.find((m) => m.value === cacheMode.value) || {}).label || "");
const initialStep = computed(() => String(route.query.step || ""));
const debugHint = computed(() => target.value === "explore" && !hasExploreConfig.value
  ? "这个源没配 exploreUrl，请填一个发现页 URL" : currentTarget.value.hint);

onMounted(async () => {
  loadEnvironment();
  const urlParam = decodeURIComponent(route.params.url || "");
  const handoff = source.value && String(source.value.bookSourceUrl || "") === urlParam;
  if (handoff) {
    try { await ensureTagMeta(); } catch (e) { /* 使用现有会话源继续 */ }
    userTags.value = splitSystemUser(source.value.bookSourceGroup || "").user;
  }
  // URL 参数存 query 原文（不再回拼 key——拼装在后端 core/debug_keys），
  // 刷新后输入框从这里恢复
  if (route.query.url) query.value = String(route.query.url);
  if (handoff || !urlParam) return;
  loading.value = true;
  try {
    const d = await getDetail(urlParam);
    try { await ensureTagMeta(); } catch (e) { /* 拆不出系统标签时按原样存 */ }
    setSource(d.source);
    const parsed = splitSystemUser(d.source.bookSourceGroup || "");
    userTags.value = parsed.user;
    sysLocked.value = !!d.system_tags_locked;
  } catch (e) {
    ElMessage.error("加载源失败：" + e.message);
  } finally {
    loading.value = false;
  }
});

async function debugRun() {
  // 后端对同样的输入会 400 说同一件事；这里先挡是为了不发一次注定失败的请求
  if (target.value === "explore" && !query.value.trim()
      && !String((source.value || {}).exploreUrl || "").trim()) {
    return ElMessage.warning("这个源没配 exploreUrl，请先填发现页 URL");
  }
  const r = await startRun({ source: source.value, target: target.value,
                             query: query.value, confirmPush });
  router.replace({ query: { step: target.value, url: query.value || undefined } });
  return r;
}

function openSettings() {
  settingsJson.value = JSON.stringify(source.value || {}, null, 2);
  settingsTags.value = userTags.value.join(", ");
  rawOpen.value = false;
  settingsOpen.value = true;
}

function applySourceUpdate(next) {
  const previous = source.value || {};
  for (const [field, value] of Object.entries(next || {})) {
    if (previous[field] !== value) updateSourceField(field, value);
  }
}

function onApplyRawSettings(parsed) {
  setSource(parsed, { saved: false });
  userTags.value = splitSystemUser(parsed.bookSourceGroup || "").user;
  settingsJson.value = JSON.stringify(parsed, null, 2);
  ElMessage.success("已应用 JSON 到当前会话");
}

function applyRawSettings() {
  try {
    const parsed = JSON.parse(settingsJson.value);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("必须是 JSON 对象");
    setSource(parsed);
    ElMessage.success("已应用 JSON 到当前会话");
  } catch (e) {
    ElMessage.error("JSON 无法应用：" + e.message);
  }
}

function applySettingsTags() {
  userTags.value = settingsTags.value.split(",").map((x) => x.trim()).filter(Boolean);
}

function rerunFromStep(stepName) {
  const step = ((result.value || {}).steps || []).find((s) => s.name === stepName) || {};
  if (stepName === "search") return debugRun();   // 搜索段重跑 = 整链入口
  if (!String(step.url || "").trim()) {
    return ElMessage.warning("上一轮结果里没有这一步的链接。先跑一次完整调试，再重试这一步");
  }
  router.replace({ query: { step: stepName, url: step.url || undefined } });
  return startRun({ source: source.value, target: STEP_TARGETS[stepName],
                    query: step.url, confirmPush });
}

function onApplyRule({ field, rule }) {
  if (!updateSourceField(field, rule)) return;
}
function gotoEditor() { window.scrollTo({ top: 0, behavior: "smooth" }); }

async function save() {
  if (!source.value) return;
  const s = JSON.parse(JSON.stringify(source.value));
  s.bookSourceType = Number(s.bookSourceType) || 0;
  if (![0, 1, 2, 3].includes(s.bookSourceType)) s.bookSourceType = 0;
  if (!String(s.bookSourceName || "").trim()) return ElMessage.warning("名称不能为空");
  try {
    await saveSource(s, userTags.value, sysLocked.value);
    markSourceSaved();
    ElMessage.success("已保存");
  } catch (e) { ElMessage.error("保存失败：" + e.message); }
}

function onBeforeUnload(e) {
  if (!sourceDirty.value) return;
  e.preventDefault();
  e.returnValue = "";
}

async function leaveWorkbench() {
  if (!sourceDirty.value) return router.push({ name: "sources" });
  try {
    await ElMessageBox.confirm("当前有未保存修改，确定要离开吗？", "未保存", {
      confirmButtonText: "丢弃修改", cancelButtonText: "继续编辑", type: "warning",
    });
    router.push({ name: "sources" });
  } catch (e) { /* 用户继续编辑 */ }
}

function onKeydown(e) { if (e.key === "Escape" && running.value) cancelRun(); }
onMounted(() => {
  window.addEventListener("keydown", onKeydown);
  window.addEventListener("beforeunload", onBeforeUnload);
});
 onUnmounted(() => {
  window.removeEventListener("keydown", onKeydown);
  window.removeEventListener("beforeunload", onBeforeUnload);
});
</script>

<template>
  <div class="workbench-page">
    <header class="wb-head wb-panel">
      <div class="wb-head-main">
        <el-button size="small" link @click="leaveWorkbench">← 返回列表</el-button>
        <div class="wb-source"><b>{{ name }}</b><span class="mono muted wb-source-url" :title="url">{{ url }}</span></div>
      </div>
      <div class="wb-head-actions">
        <el-tag v-if="sourceDirty" size="small" type="warning">有未保存修改</el-tag><el-tag v-else-if="source" size="small" :type="running ? 'warning' : 'info'">{{ running ? '调试进行中' : '编辑工作台' }}</el-tag>
        <el-button size="small" @click="openSettings">源设置</el-button>
        <el-button size="small" type="primary" @click="save">保存</el-button>
      </div>
    </header>

    <section class="wb-entry wb-panel">
      <div class="wb-panel-title"><div><b>开始调试</b><span class="muted wb-panel-subtitle">选择一个入口，结果会在下面按步骤展开</span></div><el-tag v-if="env" size="small" :type="env.ok ? 'success' : 'warning'">{{ envLoading ? '正在检查环境…' : envTitle }}</el-tag></div>
      <div class="wb-primary-row">
        <el-select v-model="target" size="small" class="wb-target" aria-label="调试目标"><el-option v-for="t in DEBUG_TARGETS" :key="t.value" :value="t.value" :label="'目标：' + t.label" /></el-select>
        <el-input v-model="query" size="small" class="wb-query" :placeholder="debugHint" @keyup.enter="debugRun()" />
        <el-button size="small" type="primary" :loading="running" @click="debugRun()">开始调试</el-button>
        <el-button v-if="running" size="small" @click="cancelRun">取消等待</el-button>
      </div>
      <div class="wb-secondary-row" :class="{ 'is-hidden': entryCompact }">
        <span class="muted wb-option-label">通道</span><el-radio-group v-model="channel" size="small"><el-radio-button value="jvm">本机引擎</el-radio-button><el-radio-button value="app">连 App</el-radio-button></el-radio-group>
        <span class="muted wb-option-label">缓存</span><el-select v-model="cacheMode" size="small" class="wb-cache"><el-option v-for="m in DEBUG_CACHE_MODES" :key="m.value" :value="m.value" :label="m.label" /></el-select>
        <span v-if="budget" class="muted wb-budget">预算 {{ budget }} 秒</span><el-input v-if="channel === 'app'" v-model="host" size="small" class="wb-host" placeholder="App IP，如 192.168.1.5" /><span v-if="running" class="muted wb-elapsed">已等待 {{ elapsed }} 秒<template v-if="budget"> / {{ budget }} 秒</template></span>
      </div>
      <!-- 有结果后：一眼看到「本次跑的是什么」，参数折着不占位 -->
      <div v-if="result" class="wb-options-summary">
        <span class="muted">{{ channelLabel }} · {{ cacheLabel }}<template v-if="budget"> · 预算 {{ budget }} 秒</template></span>
        <el-button size="small" link type="primary" @click="advancedOpen = !advancedOpen">
          {{ advancedOpen ? "收起调试选项" : "调试选项" }}
        </el-button>
      </div>
      <p v-if="channel === 'app' && preflightState" class="wb-preflight muted">App 预检：{{ preflightState.state || '未知' }}</p>
    </section>

    <!-- 规则四段与源类型由工作台从 source 自己派生，这里只传 source 一份 -->
    <DebugWorkbench class="wb-body" :initial-step="initialStep" :source="source || {}" @apply-rule="onApplyRule" @rerun-from="rerunFromStep" @goto="gotoEditor" />
    <el-drawer v-model="settingsOpen" title="源设置" size="min(520px, 92vw)" append-to-body>
      <SourceFields v-if="source" :source="source" v-model:user-tags="userTags" @update:source="applySourceUpdate" />
      <el-empty v-else description="源尚未加载" :image-size="60" />       <el-button v-if="source" class="raw-entry" size="small" link type="info" @click="rawOpen = !rawOpen">
         {{ rawOpen ? "收起原始 JSON" : "编辑原始 JSON" }}
       </el-button>
       <div v-if="rawOpen && source" class="raw-panel">
         <el-divider content-position="left">原始 JSON</el-divider>
         <el-input v-model="settingsJson" type="textarea" :rows="12" class="settings-json" spellcheck="false" />
         <el-button size="small" type="primary" plain style="margin-top: 8px" @click="applyRawSettings">应用 JSON 到当前会话</el-button>
         <p class="muted settings-tip">这里只修改当前会话；点击右上角“保存”才会写入数据库。</p>
       </div>
    </el-drawer>
   </div>
</template>

<style scoped>
.workbench-page { min-height: 100%; padding: 20px clamp(14px, 3vw, 36px) 36px; background: var(--app-bg); }
.wb-panel { background: var(--app-surface); border: 1px solid var(--app-border-light); border-radius: 10px; box-shadow: 0 1px 2px rgb(0 0 0 / 3%); }
/* 三层结构靠「阴影 + 底色 + 状态条」区分，而不是三张一样的白卡：
   页面层只交代源身份（扁平），入口层是一张卡，工作区的步骤卡片最强 */
.wb-head { display: flex; align-items: center; justify-content: space-between; gap: 16px; padding: 12px 16px; box-shadow: none; }
.wb-head-main, .wb-head-actions, .wb-source { display: flex; align-items: center; gap: 10px; min-width: 0; }
.wb-source { gap: 8px; }.wb-source-url { max-width: min(48vw, 620px); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }.wb-head-actions { flex: 0 0 auto; }
.wb-entry { margin-top: 12px; padding: 14px 16px; }.wb-panel-title, .wb-primary-row, .wb-secondary-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }.wb-panel-title { justify-content: space-between; gap: 12px; margin-bottom: 12px; }.wb-panel-subtitle { margin-left: 8px; }.wb-primary-row { flex-wrap: nowrap; }.wb-target { width: 126px; flex: 0 0 auto; }.wb-query { min-width: 160px; flex: 1 1 280px; }.wb-secondary-row { margin-top: 10px; }.wb-secondary-row.is-hidden { display: none; }.wb-options-summary { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; margin-top: 8px; }.wb-option-label { margin-left: 2px; }.wb-cache { width: 112px; }.wb-host { width: 210px; }.wb-budget, .wb-elapsed { margin-left: 4px; }.wb-preflight { margin: 8px 0 0; }.wb-body { margin-top: 14px; }
@media (max-width: 720px) {
  .workbench-page { padding: 10px 10px 24px; }
  .wb-head { display: block; padding: 10px 12px; }
  .wb-head-main { width: 100%; align-items: flex-start; }
  .wb-source { min-width: 0; flex: 1 1 auto; flex-direction: column; align-items: flex-start; gap: 2px; }
  .wb-source-url { width: 100%; max-width: 100%; }
  .wb-head-actions { width: 100%; margin-top: 10px; flex-direction: row; align-items: center; gap: 6px; }
  .wb-head-actions .el-tag { margin-right: auto; }
  .wb-head-actions .el-button { flex: 1 1 0; min-width: 0; }
  .wb-entry { padding: 12px; }
  .wb-primary-row { flex-wrap: wrap; }
  .wb-target, .wb-query { width: 100%; flex-basis: 100%; }
  .wb-primary-row .el-button { flex: 1 1 auto; }
  .wb-secondary-row { align-items: flex-start; }
  .wb-host { width: 100%; }
}
</style>

<style>
/* el-drawer teleport 到 body，内部控件样式不能依赖 scoped 属性。 */
.settings-json textarea { font-family: Consolas, Monaco, monospace; font-size: 12px; }
.settings-tip { margin: 8px 0 0; font-size: 12px; line-height: 1.5; }
</style>
