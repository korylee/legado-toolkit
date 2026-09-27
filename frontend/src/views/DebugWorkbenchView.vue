<!-- 调试工作台页面（ux-debug-shell）：#/debug/:url，列表页与编辑弹框都直达。
     运行态与结果全在 useDebugSession（单例）；本体是 DebugWorkbench（自抽屉
     抽出的判定/诊断/证据/规则编辑）。保存走 saveSource，标签沿用加载时的
     拆分结果——这里不编辑标签。 -->
<script setup>
import { ref, computed, onMounted, onUnmounted } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ElMessage } from "element-plus";
import DebugWorkbench from "../components/DebugWorkbench.vue";
import { useDebugSession, DEBUG_TARGETS, DEBUG_CACHE_MODES } from "../composables/useDebugSession";
import { getDetail, saveSource } from "../api/sources";
import { ensureTagMeta, splitSystemUser } from "../utils/tags";
import { debugKeyOf, rerunKey } from "../utils/debugKeys";

const route = useRoute();
const router = useRouter();
const {
  source, setSource, result, running, elapsed, budget,
  channel, target, query, cacheMode, host,
  env, envLoading, envTitle,
  preflightState, checking, pushed,
  startRun, cancelRun, loadEnvironment,
} = useDebugSession();

const loading = ref(false);
const userTags = ref([]);
const sysLocked = ref(false);

const name = computed(() => (source.value || {}).bookSourceName || "（无名）");
const url = computed(() => (source.value || {}).bookSourceUrl || "");

// 每步的规则（DebugWorkbench 的草稿与「重新调试本步」的基准）
const ruleByStep = computed(() => {
  const rs = (source.value || {}).ruleSearch || {};
  const rt = (source.value || {}).ruleToc || {};
  const rc = (source.value || {}).ruleContent || {};
  return {
    search: rs.bookList || "",
    bookUrl: rs.bookUrl || "",
    toc: rt.chapterList || "",
    content: rc.content || "",
  };
});
const hasExploreConfig = computed(() =>
  !!(String((source.value || {}).exploreUrl || "").trim()));
const currentTarget = computed(
  () => DEBUG_TARGETS.find((t) => t.value === target.value) || DEBUG_TARGETS[0]);
const debugHint = computed(() => {
  if (target.value === "explore" && !hasExploreConfig.value) {
    return "这个源没配 exploreUrl，请填一个发现页 URL";
  }
  return currentTarget.value.hint;
});

// 加载：会话里已有同一源（编辑弹框跳转的交接）就直接用，否则按 URL 拉
onMounted(async () => {
  loadEnvironment();
  const urlParam = decodeURIComponent(route.params.url || "");
  const handoff = source.value
    && String(source.value.bookSourceUrl || "") === urlParam;
  if (handoff || !urlParam) return;
  loading.value = true;
  try {
    const d = await getDetail(urlParam);
    try { await ensureTagMeta(); } catch (e) { /* 拆不出系统标签时按原样存 */ }
    setSource(d.source);
    const parsed = splitSystemUser(d.source.bookSourceGroup || "");
    userTags.value = parsed.user;
    sysLocked.value = !!d.system_tags_locked;
    if (route.query.key) query.value = String(route.query.key);
  } catch (e) {
    ElMessage.error("加载源失败：" + e.message);
  } finally {
    loading.value = false;
  }
});

async function debugRun() {
  const key = debugKeyOf(target.value, query.value,
                         (source.value || {}).exploreUrl);
  if (!key) {
    return ElMessage.warning("这个源没配 exploreUrl，请先填发现页 URL");
  }
  const r = await startRun({ source: source.value, key });
  // key 进 URL：刷新可恢复、问题场景可直接分享
  router.replace({ query: { key } });
  return r;
}

function rerunFromStep(stepName) {
  const step = ((result.value || {}).steps || [])
    .find((s) => s.name === stepName) || {};
  const key = stepName === "search"
    ? debugKeyOf(target.value, query.value, (source.value || {}).exploreUrl)
    : rerunKey(stepName, step.url);
  if (!key) {
    return ElMessage.warning(
      "上一轮结果里没有这一步的链接。先跑一次完整调试，再重试这一步");
  }
  return startRun({ source: source.value, key });
}

function onApplyRule({ field, rule }) {
  const [group, key] = String(field || "").split(".");
  if (!group || !key || !source.value[group]) return;
  source.value[group][key] = rule;
}

// DebugWorkbench 的「去改规则」：工作台里规则编辑就在本页顶部，滚回去即可
function gotoEditor() {
  window.scrollTo({ top: 0, behavior: "smooth" });
}

async function save() {
  if (!source.value) return;
  const s = JSON.parse(JSON.stringify(source.value));
  s.bookSourceType = Number(s.bookSourceType) || 0;
  if (![0, 1, 2, 3].includes(s.bookSourceType)) s.bookSourceType = 0;
  if (!String(s.bookSourceName || "").trim()) return ElMessage.warning("名称不能为空");
  try {
    await saveSource(s, userTags.value, sysLocked.value);
    ElMessage.success("已保存");
  } catch (e) {
    ElMessage.error("保存失败：" + e.message);
  }
}

// 键盘流：Esc 取消等待（Enter 重跑在输入框上）
function onKeydown(e) {
  if (e.key === "Escape" && running.value) cancelRun();
}
onMounted(() => window.addEventListener("keydown", onKeydown));
onUnmounted(() => window.removeEventListener("keydown", onKeydown));
</script>

<template>
  <div class="workbench-page">
    <div class="wb-head">
      <el-button size="small" link @click="router.push({ name: 'sources' })">← 返回列表</el-button>
      <b>{{ name }}</b>
      <span class="mono muted">{{ url }}</span>
      <span class="grow" />
      <el-button size="small" type="primary" @click="save">保存</el-button>
    </div>

    <!-- 运行入口：通道 / 目标 / 关键词 / 预算。失败下一步与证据都在下面的本体里 -->
    <div class="wb-controls">
      <el-radio-group v-model="channel" size="small">
        <el-radio-button value="jvm">本机引擎</el-radio-button>
        <el-radio-button value="app">连 App</el-radio-button>
      </el-radio-group>
      <el-radio-group v-model="target" size="small">
        <el-radio-button v-for="t in DEBUG_TARGETS" :key="t.value" :value="t.value">
          {{ t.label }}
        </el-radio-button>
      </el-radio-group>
      <el-input v-model="query" size="small" :placeholder="debugHint"
                style="flex: 1 1 180px" @keyup.enter="debugRun()" />
      <el-select v-model="cacheMode" size="small" style="width: 112px">
        <el-option v-for="m in DEBUG_CACHE_MODES" :key="m.value" :value="m.value" :label="m.label" />
      </el-select>
      <el-button size="small" type="primary" :loading="running" @click="debugRun()">开始调试</el-button>
      <el-button v-if="running" size="small" @click="cancelRun">取消等待</el-button>
    </div>
    <p v-if="running" class="muted" style="margin: 4px 0 0">
      已等待 {{ elapsed }} 秒<template v-if="budget"> / 预算 {{ budget }} 秒</template>
    </p>
    <p v-else-if="budget" class="muted" style="margin: 4px 0 0">
      调试预算 {{ budget }} 秒
    </p>
    <div v-if="channel === 'app'" style="margin-top: 6px">
      <el-input v-model="host" size="small"
                placeholder="App 的 IP，如 192.168.1.5" style="max-width: 260px" />
    </div>

    <DebugWorkbench class="wb-body" :rules="ruleByStep"
                    :source-type="Number((source || {}).bookSourceType) || 0"
                    :source="source || {}"
                    @apply-rule="onApplyRule" @rerun-from="rerunFromStep"
                    @goto="gotoEditor" />
  </div>
</template>

<style scoped>
.wb-head { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; }
.wb-controls { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.grow { flex: 1 1 auto; }
.mono { font-family: Consolas, Monaco, monospace; }
.wb-body { margin-top: 12px; }
</style>
