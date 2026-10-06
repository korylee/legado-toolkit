<!-- 调试工作台容器：由 SourceWorkspaceDrawer 挂载，运行态与结果全在 useDebugSession（单例）。
     源身份与保存动作在壳顶栏；规则四段在调试页决策卡下方**内联编辑**（DebugWorkbench），
     元数据在壳顶栏「编辑」切到编辑视图改——这里不再有第二套编辑面。 -->
<script setup>
import {
  ref,
  computed,
  watch,
  nextTick,
  onMounted,
  onUnmounted,
  inject,
} from "vue";
import { ElMessage } from "element-plus";
import DebugWorkbench from "../components/DebugWorkbench.vue";
import {
  useDebugSession,
  DEBUG_TARGETS,
  DEBUG_CACHE_MODES,
  STEP_TARGETS,
} from "../composables/useDebugSession";
import { SOURCE_WORKSPACE_KEY } from "../composables/useSourceWorkspace";

const props = defineProps({
  initialSource: { type: Object, default: null },
  initialStep: { type: String, default: "" },
  initialQuery: { type: String, default: "" },
});
const {
  clearPreflight,
  setActiveSourceUrl,
  result,
  resultRevision,
  budget,
  channel,
  target,
  query,
  cacheMode,
  host,
  env,
  envLoading,
  envTitle,
  preflightState,
  running,
  startRun,
  cancelRun,
  confirmPush,
} = useDebugSession();

const workspace = inject(SOURCE_WORKSPACE_KEY);
if (!workspace)
  throw new Error("DebugWorkbenchView must be mounted under MainLayout");

const source = ref(null);

function cloneSource(value) {
  return value == null ? null : JSON.parse(JSON.stringify(value));
}
//: 只播种本地镜像，不回写草稿——workspace.state.source 已经是权威份
//（壳的 replaceSource / open 落的位），这里回写只会空转一遍等值判断
function seedSource(value) {
  source.value = cloneSource(value);
  setActiveSourceUrl(source.value?.bookSourceUrl || "");
}
function applySourceUpdate(next) {
  if (!next || typeof next !== "object") return;
  const previousUrl = String((source.value || {}).bookSourceUrl || "").trim();
  const nextSource = JSON.parse(JSON.stringify(next));
  source.value = nextSource;
  workspace.updateDraft(nextSource);
  const nextUrl = String(nextSource.bookSourceUrl || "").trim();
  if (nextUrl !== previousUrl) {
    clearPreflight();
    setActiveSourceUrl(nextUrl);
  }
}

watch(
  source,
  (next) => {
    if (next) workspace.updateDraft(next);
  },
  { deep: true },
);

//: 壳的详情加载可能晚于本视图挂载（列表直达调试）：加载完成把源补种进来。
//: 自己写入的内容会被等值判断拦住（applySourceUpdate → updateDraft 的回声）
watch(
  () => workspace.state.source,
  (next) => {
    if (!next) return;
    if (JSON.stringify(next) === JSON.stringify(source.value)) return;
    seedSource(next);
  },
);

const hasExploreConfig = computed(
  () => !!String((source.value || {}).exploreUrl || "").trim(),
);
const currentTarget = computed(
  () => DEBUG_TARGETS.find((t) => t.value === target.value) || DEBUG_TARGETS[0],
);
//: 入口层在有结果之后收成一行：结果一出来，视线该落在步骤卡片上，
//: 通道/缓存/IP 这些**本次运行参数**折进「调试选项」（AGENTS #68：动作与结果同屏）
const advancedOpen = ref(false);
const entryCompact = computed(() => !!result.value && !advancedOpen.value);
const channelLabel = computed(() =>
  channel.value === "app" ? "连 App" : "本机引擎",
);
const cacheLabel = computed(
  () =>
    (DEBUG_CACHE_MODES.find((m) => m.value === cacheMode.value) || {}).label ||
    "",
);
const initialStep = computed(() => String(props.initialStep || ""));
const resultStale = computed(() => !!result.value && resultRevision.value != null
  && resultRevision.value !== workspace.state.draftRevision);
const debugHint = computed(() =>
  target.value === "explore" && !hasExploreConfig.value
    ? "这个源没配 exploreUrl，请填一个发现页 URL"
    : currentTarget.value.hint,
);

onMounted(() => {
  if (props.initialSource) seedSource(props.initialSource);
  if (props.initialQuery) query.value = String(props.initialQuery);
});

async function debugRun() {
  // 后端对同样的输入会 400 说同一件事；这里先挡是为了不发一次注定失败的请求
  if (
    target.value === "explore" &&
    !query.value.trim() &&
    !String((source.value || {}).exploreUrl || "").trim()
  ) {
    return ElMessage.warning("这个源没配 exploreUrl，请先填发现页 URL");
  }
  const r = await startRun({
    source: source.value,
    target: target.value,
    query: query.value,
    confirmPush,
    draftRevision: workspace.state.draftRevision,
  });
  return r;
}

function rerunFromStep(stepName) {
  const step =
    ((result.value || {}).steps || []).find((s) => s.name === stepName) || {};
  if (stepName === "search") return debugRun(); // 搜索段重跑 = 整链入口
  if (!String(step.url || "").trim()) {
    return ElMessage.warning(
      "上一轮结果里没有这一步的链接。先跑一次完整调试，再重试这一步",
    );
  }
  return startRun({
    source: source.value,
    target: STEP_TARGETS[stepName],
    query: step.url,
    confirmPush,
    draftRevision: workspace.state.draftRevision,
  });
}

//: 规则字段的唯一写入口（内联编辑、候选「用这条」共用）：整对象替换，
//: 源里缺规则组时顺手补上。词表见 utils/steps 的 RULE_FIELD_DEFS
function setRuleField(group, key, value) {
  if (!source.value || !group || !key) return;
  applySourceUpdate({
    ...source.value,
    [group]: { ...(source.value[group] || {}), [key]: value },
  });
}

function onApplyRule({ field, rule }) {
  const [group, key] = String(field || "").split(".");
  if (!group || !key) return;
  setRuleField(group, key, rule);
}

//: 「去补规则 / 改选择器」的落点：工作台里当前步骤的规则框就在决策卡下方，
//: 滚过去并聚焦即可（data-rule-field 与 utils/steps 的 field 同一词表）。
//: basic（去改类型这类元数据动作）→ 顶栏切「编辑」；草稿在 workspace 里，
//: 切回来不丢。requestAnimationFrame 等渲染与过渡起步——单 nextTick 偶发
//: 抢在 DOM 落位之前
function focusRuleField(v) {
  const field =
    v && typeof v === "object" && v.tab === "rules"
      ? String(v.field || "")
      : "";
  if (!field) {
    workspace.setMode("edit");
    return;
  }
  nextTick(() => {
    requestAnimationFrame(() => {
      const el = document.querySelector('[data-rule-field="' + field + '"]');
      if (!el) return;
      el.scrollIntoView({ block: "center", behavior: "smooth" });
      const input = el.querySelector("textarea, input");
      if (input) input.focus({ preventScroll: true });
    });
  });
}

function onBeforeUnload(e) {
  if (!workspace.dirty.value) return;
  e.preventDefault();
  e.returnValue = "";
}

function onKeydown(e) {
  if (e.key === "Escape" && running.value) cancelRun();
}
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
    <section class="wb-entry wb-panel">
      <div class="wb-panel-title">
        <div>
          <b>调试</b
          ><span class="muted wb-panel-subtitle">选择入口并查看调试结果</span>
        </div>
        <div class="wb-title-side">
          <el-tag
            v-if="env"
            size="small"
            :type="env.ok ? 'success' : 'warning'"
            >{{ envLoading ? "正在检查环境…" : envTitle }}</el-tag
          >
        </div>
      </div>
      <div class="wb-primary-row">
        <el-select v-model="target" class="wb-target" aria-label="调试目标"
          ><el-option
            v-for="t in DEBUG_TARGETS"
            :key="t.value"
            :value="t.value"
            :label="'目标：' + t.label"
        /></el-select>
        <el-input
          v-model="query"
          class="wb-query"
          :placeholder="debugHint"
          @keyup.enter="debugRun()"
        />
        <el-button type="primary" :loading="running" @click="debugRun()"
          >调试</el-button
        >
      </div>
      <div class="wb-secondary-row" :class="{ 'is-hidden': entryCompact }">
        <span class="muted wb-option-label">通道</span
        ><el-radio-group v-model="channel" size="small"
          ><el-radio-button value="jvm">本机引擎</el-radio-button
          ><el-radio-button value="app">连 App</el-radio-button></el-radio-group
        >
        <span class="muted wb-option-label">缓存</span
        ><el-select v-model="cacheMode" size="small" class="wb-cache"
          ><el-option
            v-for="m in DEBUG_CACHE_MODES"
            :key="m.value"
            :value="m.value"
            :label="m.label"
        /></el-select>
        <span v-if="budget" class="muted wb-budget">预算 {{ budget }} 秒</span
        ><el-input
          v-if="channel === 'app'"
          v-model="host"
          size="small"
          class="wb-host"
          placeholder="App IP，如 192.168.1.5"
        />
      </div>
      <!-- 有结果后：一眼看到「本次跑的是什么」，参数折着不占位 -->
      <div v-if="result" class="wb-options-summary">
        <span class="muted"
          >{{ channelLabel }} · {{ cacheLabel
          }}<template v-if="budget"> · 预算 {{ budget }} 秒</template></span
        >
        <el-button
          size="small"
          link
          type="primary"
          @click="advancedOpen = !advancedOpen"
        >
          {{ advancedOpen ? "收起调试选项" : "调试选项" }}
        </el-button>
      </div>
      <p v-if="channel === 'app' && preflightState" class="wb-preflight muted">
        App 预检：{{ preflightState.state || "未知" }}
      </p>
    </section>

    <el-alert v-if="resultStale" type="warning" show-icon :closable="false"
              title="规则已修改，本次调试结果不是当前草稿的验证结果，请重新调试。" />

    <!-- 规则四段与源类型由工作台从 source 自己派生，这里只传 source 一份；
         enabledCookieJar 从源自己声明推导（AGENTS #13：能算的不传） -->
    <DebugWorkbench
      class="wb-body"
      :initial-step="initialStep"
      :source="source || {}"
      :enabled-cookie-jar="!!((source || {}).enabledCookieJar)"
      @apply-rule="onApplyRule"
      @rerun-from="rerunFromStep"
      @goto="focusRuleField"
    />
  </div>
</template>

<style scoped>
.workbench-page {
  min-height: 100%;
  padding: 14px clamp(14px, 3vw, 36px) 36px;
  background: var(--app-bg);
}
.wb-panel {
  background: var(--app-surface);
  border: 1px solid var(--app-border-light);
  border-radius: 10px;
  box-shadow: 0 1px 2px rgb(0 0 0 / 3%);
}
/* 调试页的两层结构靠「阴影 + 底色」区分：入口层是一张卡，工作区的步骤卡片最强。
   源身份与保存动作在壳顶栏，这里不再重复一条头部 */
.wb-entry {
  padding: 14px 16px;
}
.wb-panel-title,
.wb-primary-row,
.wb-secondary-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.wb-panel-title {
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
}
.wb-title-side {
  display: flex;
  align-items: center;
  gap: 8px;
}
.wb-panel-subtitle {
  margin-left: 8px;
}
/* 调试入口主行：输入框与选择框的高度来自两处不同声明——`.el-input` 靠 `--el-input-height`
   同时决定行高与内层高度，`.el-select` 只由 wrapper 的 min-height 决定。两边都得钉住同一个量，
   只给两个 wrapper 设 min-height 就是对不齐（窄屏那条全局 min-height 也会被 size 掉）。 */
.wb-primary-row {
  flex-wrap: nowrap;
}
.wb-target {
  width: 126px;
  flex: 0 0 auto;
}
.wb-query {
  min-width: 160px;
  flex: 1 1 280px;
}
.wb-primary-row .wb-target,
.wb-primary-row .wb-query,
.wb-primary-row .el-button {
  height: var(--app-touch-size);
  box-sizing: border-box;
}
.wb-primary-row .wb-query {
  --el-input-height: var(--app-touch-size);
}
.wb-primary-row :deep(.el-select__wrapper) {
  height: var(--app-touch-size);
  min-height: var(--app-touch-size);
  box-sizing: border-box;
}
.wb-secondary-row {
  margin-top: 10px;
}
.wb-secondary-row.is-hidden {
  display: none;
}
.wb-options-summary {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 8px;
}
.wb-option-label {
  margin-left: 2px;
}
.wb-cache {
  width: 112px;
}
.wb-host {
  width: 210px;
}
.wb-budget {
  margin-left: 4px;
}
.wb-preflight {
  margin: 8px 0 0;
}
.wb-body {
  margin-top: 14px;
}
@media (max-width: 720px) {
  .workbench-page {
    padding: 10px 10px 24px;
  }
  .wb-entry {
    padding: 12px;
  }
  .wb-primary-row {
    flex-wrap: wrap;
  }
  .wb-target,
  .wb-query {
    width: 100%;
    flex-basis: 100%;
  }
  .wb-primary-row .el-button {
    flex: 1 1 auto;
  }
  .wb-secondary-row {
    align-items: flex-start;
  }
  .wb-host {
    width: 100%;
  }
}
</style>
