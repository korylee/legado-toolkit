<!--
  调试工作台本体（手工调试主流程）：步骤页签 → 本步规则输入（详情步覆盖 ruleBookInfo
  全组字段）→ 本步实测摘要（事件里有据可查的统计与字段）→ 三个页签（提取结果 /
  调试事件 / 材料）。运行态读 useDebugSession；编辑上下文（源/规则/类型）走 props。
 -->
<script setup>
// 调试工作台（手工主流程）：规则输入 → 本步实测摘要 → 材料（命中片段 / 整页源码，
// 只读查看）。完整事件只在「调试事件」页签。
//
// **引擎只有一台**（App 引擎：连 App / 本机），判定与逐段结论都来自它。
// 命中片段只显示引擎返回的 `steps[].matched_html`，自报「取证，可能不全」；
// 命中多少以真引擎实测为准，本地不提供第二种判定。
import { ref, computed, watch, nextTick, onMounted } from "vue";
import { ElMessage } from "element-plus";

// 步骤名 → 中文的**唯一**一份（编辑视图共用），别再在本组件里写第二份。
// RULE_FIELD_DEFS 是规则字段的唯一词表（内联编辑框的标签/占位符也从它取）
import { STEP_LABELS, RULE_FIELD_DEFS } from "../utils/steps";
import { useDebugSession } from "../composables/useDebugSession";
import { compareRuns, statusLabel } from "../utils/debugCompare";
import { stepResult } from "../utils/debugStepResult";
// 源码的显示层（折行缩进 + 实体展开，纯函数）：好看的和能抄的是两份东西，见 htmlView
import { formatMaterial } from "../utils/htmlView";
// WebView 能力边界是**运行级事实**：这里只渲染告警条
import {
  unsupportedForStep,
  webviewUnsupportedTitle,
  webviewUnsupportedView,
} from "../utils/webviewCapability";
// 在途调试的相位码 → 中文（唯一一份；后端只给码与事实）
import { debugRunState } from "../utils/debugRun";
import DebugTimeline from "./DebugTimeline.vue";

const props = defineProps({
  initialStep: { type: String, default: "" },
  //: 源有没有声明 cookie jar。登录墙判定要用它：那一档「200 + 登录词」以它为前提
  //: （「请登录」在正常页面的导航栏里太常见）
  enabledCookieJar: { type: Boolean, default: false },
  //: 完整源对象（表单里那份）：定层要读源**自己声明的能力**——`ruleContent.webJs` /
  //: `loginUrl` / URL 规则里的 `webView`（AGENTS #13：能推导的别让用户填）
  source: { type: Object, default: null },
  draftRevision: { type: [Number, String], default: null },
  //: 「从此步重跑」在途（父组件的 appDebugging）。重跑是 App 实测动作，
  //: 在途时按钮要转圈、并挡住连点——两次分段调试的 WS 会话会互相顶掉
  //: 自动生成失败时保留的上游原因；调试材料是补救路径，不得覆盖这条原因。
  entryError: { type: String, default: "" },
  //: 「从此步重跑」的执行者（父组件的 rerunFromStep，返回 startRun 的 Promise）。
  //: **函数 prop 而不是 emit**：apply_and_rerun 的结果要等运行结束才知道
  //: （提交失败 / lane 忙 / 取消 / 读取失败），emit 是发后即忘，等不到它。
  onRerun: { type: Function, default: null },
});

// 结果、在途、对比基线直接读**会话**（第二期收尾）：本组件不再经 props 接收
// 运行态——弹框与工作台页面挂它，看到的是同一份。运行中的等待/预算/取消
// 显示在下面那条 sticky 运行条里（唯一一份，入口卡不另设）
//
// **两个 channel 别弄反**：会话里这个（别名 `debugChannel`）是**入口表单选的**通道，
// 决定下一次跑哪儿；而 `channel` / `isAppResult` / `isEngineResult` 吃的是下面那个 computed
// ——**这份结果自带**的来源通道 `result.source`。拿错不报错，只会把归因说反。
const {
  result,
  resultRevision,
  compare,
  running,
  elapsedMs,
  runStatus,
  liveEvents,
  lastRunAt,
  lastRunMs,
  budget,
  channel: debugChannel,
  cancelRun,
  resumeRunning,
} = useDebugSession();
const rerunning = running;
const prevResult = computed(() => compare.value.prev);
const emit = defineEmits([
  "update:modelValue",
  "goto",
  "rerunFrom",
  "applyRule",
]);

//: 运行条上那一行字。相位码与事实来自后端（`runStatus` 轮询），**词在这里出**
//: （utils/debugRun）。后端说不知道就照实说「已等待 N 秒」，不编进度
const runLine = computed(() => debugRunState(runStatus.value, elapsedMs.value));

//: 这次结果是**在本组件之外跑完的**（抽屉关着时跑完、或另一个标签页跑的）：
//: 结果已经在会话里，而用户没看见它跑——不标出来，它看起来就像「从没跑过」
//: （关掉调试弹窗后重开正是这个情形）。挂载时刻之后才结束的运行才算
const openedAt = ref(Date.now());
const finishedInBackground = computed(
  () =>
    !running.value &&
    result.value !== null &&
    lastRunAt.value !== null &&
    lastRunAt.value > openedAt.value,
);

//: 规则四段与源类型都从 **source 自己**派生，宿主只传 source 一份（AGENTS #13：
//: 算得出的不传）——历史上它们是两个独立 props，靠宿主记得传齐；现在由覆盖层
//: 统一传 source。再要加派生输入，从这里出，别加 props。
const sourceType = computed(
  () => Number((props.source || {}).bookSourceType) || 0,
);

// explore 是发现链路的产出步（key 带 `发现::` 时后端才产出它）
//: 每次渲染的字符数。整页 HTML 可能 100 万字符，全量进 DOM 会卡
const RENDER_CHUNK = 20000;
//: 搜索最多索引的命中数。整页 HTML 里搜 div / class 必然远超此数，
//: 超限时必须显式告知，否则计数器会把「前 200 处」说成全部
const HIT_LIMIT = 200;

const staleSteps = ref(new Set());

function markStepStale(name) {
  if (!name) return;
  staleSteps.value = new Set([...staleSteps.value, name]);
}

// 新的真实引擎结果到达后，只有实际返回的步骤才清除对应 stale 标记；
// 错误体或空结果不应把“规则已修改、尚未验证”洗掉。
watch(result, (next) => {
  if (
    resultRevision.value != null &&
    props.draftRevision != null &&
    resultRevision.value !== props.draftRevision
  )
    return;
  const names = ((next && next.steps) || [])
    .map((step) => step.name)
    .filter(Boolean);
  if (!names.length) return;
  const nextStale = new Set(staleSteps.value);
  names.forEach((name) => nextStale.delete(name));
  staleSteps.value = nextStale;
});

const activeStep = ref("");
const subTab = ref("values");

//: 命中片段和整页共用只读材料查看器；默认原文，格式化只改变显示。
const materialKind = ref("matched");
const formatSource = ref(false);
const renderLimit = ref(RENDER_CHUNK);
const searchKey = ref("");
const activeHit = ref(0);

const steps = computed(() => result.value?.steps || []);
const pages = computed(() => result.value?.pages || []);

//: 结果来源。**两台都是 App 引擎**，差别只在环境，所以必须标出来：
//:   - `app`：连 App 实测（手机上跑，登录态/网络出口都是真的）
//:   - `jvm`：本机引擎（**同一段 App 代码**跑在本机：真规则、真 JS；差在环境——
//:     登录态要预热、没有手机的网络出口）见 lessons §六十三
//: 本地调试不再是结果来源；命中源码只显示引擎返回的片段。
const channel = computed(() => String((result.value || {}).source || ""));
const isAppResult = computed(() => channel.value === "app");
const webviewUnsupported = computed(
  () => webviewUnsupportedView(result.value) || [],
);
const webviewTitle = computed(() =>
  webviewUnsupportedTitle(webviewUnsupported.value),
);
//: 跑的是不是「App 的真引擎」——连 App 与本机引擎都算。事件流页签、分段重跑、
//: 「用实测值当基准」这些只对真引擎成立
const isEngineResult = computed(
  () => channel.value === "app" || channel.value === "jvm",
);
//: App 推来的原始事件流。steps[].values 是它去掉耗时前缀后的段内文本，
//: 排查「哪一步慢」「App 到底推了什么」只能看这里。
//: **在跑的时候看流里的那份**（引擎逐条 flush，已经被搬进事件账本）——原来的
//: 同步长轮询要等跑完才有事件可读，等待期是空等待。
const events = computed(() =>
  running.value && liveEvents.value.length
    ? liveEvents.value
    : (result.value && result.value.events) || [],
);
//: 默认子页签：真引擎看事件流；没有事件时看提取结果
const defaultSubTab = "values";

// ---------------------------------------------------------------- 与上次相比（ux-debug-loop）
const diffRows = computed(() =>
  prevResult.value ? compareRuns(prevResult.value, result.value) : [],
);
const diffBy = computed(() =>
  Object.fromEntries(diffRows.value.map((d) => [d.name, d])),
);
const diffSummary = computed(() => {
  if (!prevResult.value || !diffRows.value.length) return "";
  const changed = diffRows.value
    .filter((d) => d.status !== "same")
    .map((d) => `${STEP_LABELS[d.name] || d.name} ${statusLabel(d.status)}`);
  return changed.length
    ? "与上次相比：" + changed.join(" · ") + "；其余相同"
    : "与上次相比：全部相同";
});
function diffOf(name) {
  return diffBy.value[name];
}
function diffTagType(status) {
  return (
    { regressed: "danger", changed: "warning", moved: "info", new: "success" }[
      status
    ] || "info"
  );
}
const current = computed(
  () =>
    steps.value.find((s) => s.name === activeStep.value) ||
    steps.value[0] ||
    null,
);
const currentSummary = computed(() => stepResult(current.value || {}));
const currentStale = computed(
  () =>
    staleSteps.value.has((current.value || {}).name) ||
    (resultRevision.value != null &&
      props.draftRevision != null &&
      resultRevision.value !== props.draftRevision),
);
const currentPage = computed(() => {
  if (!current.value || !current.value.page_id) return null;
  return pages.value.find((p) => p.id === current.value.page_id) || null;
});
// —— 只读材料查看器（命中片段 / 整页源码 共用一套）——
// 材料就是引擎带回或补抓的原文；格式化只改显示（formatMaterial），复制永远是原文。
const materialRaw = computed(() =>
  materialKind.value === "page"
    ? (currentPage.value && currentPage.value.html) || ""
    : (current.value || {}).matched_html || "",
);
const materialText = computed(() =>
  formatSource.value ? formatMaterial(materialRaw.value) : materialRaw.value,
);
const materialEmptyHint = computed(() => {
  if (materialKind.value === "page") return "这一步没有抓到页面";
  if (isAppResult.value)
    return "App 调试通道只推文本，取不到命中片段；改用本机引擎调试可以看到";
  return "本机引擎未返回命中片段，规则可能没选中任何节点";
});
const materialTruncatedNote = computed(() => {
  if (materialKind.value !== "matched") {
    return currentPage.value && currentPage.value.truncated
      ? "原文 " +
          currentPage.value.len +
          " 字符，已截断到前 " +
          currentPage.value.html.length +
          " 字符"
      : "";
  }
  return /…（已截断/.test(materialRaw.value) ? "命中子树过长，已截断" : "";
});
// 命中片段是**取证**而非完整命中集合（引擎只回传前几个节点）：标题必须自报不全，
// 不能让「片段里有 3 条」被读成「引擎只取到 3 条」。
const materialTitle = computed(() =>
  materialKind.value === "page" ? "整页源码" : "命中片段（取证，可能不全）",
);
const pageOriginNote = computed(() => {
  const p = currentPage.value;
  if (materialKind.value !== "page" || !p) return "";
  if (p.origin === "engine") return "本机引擎取回";
  return (p.cached ? "来自缓存" : "本次新抓取") + "（另抓的一份）";
});

function copyMaterial() {
  return copyRaw(materialRaw.value, "已复制材料原文");
}

// 三态圆点：fail 红 / unknown 灰 / pass 且有附注 黄 / 纯 pass 绿
function dotClass(s) {
  if (!s) return "";
  if (s.verdict === "fail") return "err";
  if (s.verdict === "unknown") return "unknown";
  return s.has_notes ? "warn" : "ok";
}
// 搜索在**完整原文**上做（纯字符串扫描，结果不进 DOM），最多记 HIT_LIMIT 处
const hitOffsets = computed(() => {
  const key = searchKey.value.trim();
  const html = materialText.value;
  if (!key || !html) return [];
  const out = [];
  let from = 0;
  while (out.length < HIT_LIMIT) {
    const i = html.indexOf(key, from);
    if (i < 0) break;
    out.push(i);
    from = i + Math.max(1, key.length);
  }
  return out;
});

// 命中是否被 HIT_LIMIT 截断：索引已占满，且最后一处之后还能再找到
// （只看长度不够——恰好 200 处时不该提示「命中过多」）
const hitsTruncated = computed(() => {
  const offsets = hitOffsets.value;
  if (offsets.length < HIT_LIMIT) return false;
  const key = searchKey.value.trim();
  const html = materialText.value;
  if (!key || !html) return false;
  const last = offsets[offsets.length - 1];
  return html.indexOf(key, last + Math.max(1, key.length)) >= 0;
});

// 只渲染前 renderLimit 个字符，避免百万字符全量进 DOM
const headText = computed(() => materialText.value.slice(0, renderLimit.value));
const hasMore = computed(() => renderLimit.value < materialText.value.length);

// 把已渲染的片段按命中位置切成 [普通, 高亮, 普通, ...]
const segments = computed(() => {
  const head = headText.value;
  const key = searchKey.value.trim();
  if (!key) return [{ text: head, hit: false, index: -1 }];
  const segs = [];
  let cursor = 0;
  let hitIndex = 0;
  hitOffsets.value.forEach((off) => {
    if (off >= head.length) return;
    if (off > cursor)
      segs.push({ text: head.slice(cursor, off), hit: false, index: -1 });
    segs.push({
      text: head.slice(off, off + key.length),
      hit: true,
      index: hitIndex,
    });
    hitIndex += 1;
    cursor = off + key.length;
  });
  if (cursor < head.length)
    segs.push({ text: head.slice(cursor), hit: false, index: -1 });
  return segs;
});

onMounted(() => {
  if (result.value)
    activeStep.value =
      props.initialStep || (steps.value[0] && steps.value[0].name) || "";
  // 刷新/重开页面时把那次**还在跑**的调试重新挂上：任务行还在就挂得上。
  // 观测登记放在进程内存里时，一次刷新就等于"那次调试从界面上消失了"。
  void resumeRunning();
});
watch(result, (show) => {
  if (!show) return;
  // 抽屉常开时也可能收到**别处跑完**的结果（另一个标签页 / 后台那次收尾）：
  // 把「打开时刻」推到这一刻，「后台跑完」那条提示才不会把它误标成没看见过
  openedAt.value = Date.now();
  // 每次打开都重拉：用户可能刚在「设置 → 模型」里配好，不该还看着上一次的结论
  refreshLLMStatus();
  if (show)
    activeStep.value =
      props.initialStep || (steps.value[0] && steps.value[0].name) || "";
  subTab.value = defaultSubTab;
  renderLimit.value = RENDER_CHUNK;
  searchKey.value = "";
  activeHit.value = 0;
});

// 「从此步重跑」完成后父组件会更新 initialStep 想定位到重跑的那一步。
// 抽屉常开时 modelValue 不变（上面那个 watch 不触发），必须自己盯着 initialStep 走
watch(
  () => props.initialStep,
  (v) => {
    if (v) selectStep(v);
  },
);

//: 「从此步重跑」可不可点：搜索步永远可以（等于整链）；其余步要上轮结果里
//: 有这一步的 URL（拼 App 的 ++/--/裸URL key 用），没有就只能先跑一次完整链路
const canRerun = computed(() => {
  if (!isEngineResult.value) return false;
  const s = current.value || {};
  if (s.name === "search") return true;
  return !!String(s.url || "").trim();
});

// 跳到第 i 处命中（i 为负则向前），必要时先扩大渲染范围
function gotoHit(i) {
  const total = hitOffsets.value.length;
  if (!total) return;
  activeHit.value = ((i % total) + total) % total;
  const off = hitOffsets.value[activeHit.value];
  if (off + RENDER_CHUNK > renderLimit.value)
    renderLimit.value = off + RENDER_CHUNK;
  nextTick(() => {
    const el = document.getElementById("debug-hit-" + activeHit.value);
    if (el) el.scrollIntoView({ block: "center", behavior: "smooth" });
  });
}

function selectStep(name) {
  activeStep.value = name;
  subTab.value = defaultSubTab;
  renderLimit.value = RENDER_CHUNK;
  searchKey.value = "";
  activeHit.value = 0;
}

//: 当前编辑字段的规则：**只读镜像**源草稿里那一份（可写入口在下面的内联编辑框，
//: 经 applyRule 事件由宿主写回 workspace）。详情步每个字段各自镜像，切换即换。
const currentRule = computed(() => {
  const field = (inlineDef.value || {}).field;
  if (!field) return "";
  const [group, key] = field.split(".");
  const rs = (props.source || {})[group] || {};
  return String(rs[key] || "");
});

//: 内联规则编辑：当前步骤的全部可编辑字段（RULE_FIELD_DEFS 同一词表），
//: 详情步覆盖 ruleBookInfo 全组。编辑目标用 ref（切换字段不丢草稿——每个字段
//: 各自镜像源草稿那份，切换即换镜像）。每次输入：标过期 + emit 给宿主写草稿。
const stepFields = computed(() =>
  RULE_FIELD_DEFS.filter(
    (d) => d.step === ((current.value || {}).name || ""),
  ),
);
const activeField = ref("");
const inlineDef = computed(() =>
  stepFields.value.find((d) => d.field === activeField.value)
    || stepFields.value[0]
    || null,
);
watch([activeStep, stepFields], () => {
  if (!stepFields.value.some((d) => d.field === activeField.value)) {
    activeField.value = (stepFields.value[0] || {}).field || "";
  }
});

function rerunCurrentStep() {
  const name = (current.value || {}).name;
  if (name) emit("rerunFrom", name);
}

function onInlineRuleInput(value) {
  const name = (current.value || {}).name || "";
  markStepStale(name);
  emit("applyRule", { field: (inlineDef.value || {}).field, rule: value });
}

//: 入口表单在页面顶部：自动生成失败时把人带回那里重新调试搜索
function focusRunEntry() {
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function loadMore() {
  renderLimit.value += RENDER_CHUNK;
}

//: 复制一律走这里，且**只复制原文**：格式化视图里换行是加的、实体是展开的，
//: 拿它去写规则/正则就会以为页面上真是那样（原来那句「复制出来的就是原文」照旧成立）
async function copyRaw(text, okMsg) {
  try {
    // navigator.clipboard 只在安全上下文（https / localhost）存在。
    // 本前端开了 server.host，用户会从局域网 IP 用 http 打开——
    // 那里它是 undefined，直接调用会同步抛 TypeError，界面毫无反应。
    // 必须包在 try 里，并给出明确反馈。
    await navigator.clipboard.writeText(text);
    ElMessage.success(okMsg);
  } catch (e) {
    ElMessage.warning("复制失败，请手动选中文本");
  }
}
</script>

<template>
  <div class="debug-workbench" :class="{ running }">
    <!-- 运行态的**固定显示**（ux-debug-reading）：等待/预算/取消收在这条 sticky 条里，
         滚到证据区仍找得到；结束后整个消失，不占版面。
         那一行字说**当时到底在等什么**：调试是同步长轮询，等待期没有产物可读，
         所以只有「已等多久 + 此刻占着引擎的是谁」两条事实（utils/debugRun） -->
    <div v-if="running" class="debug-runchip">
      <span class="debug-runchip-text">
        {{ runLine.text
        }}<template v-if="budget"> · 预算 {{ budget }} 秒</template>
      </span>
      <el-button size="small" @click="cancelRun">取消等待</el-button>
    </div>

    <!-- 结果是在本组件之外跑完的（关掉调试弹窗之后跑完的正是这种）：说出来，
         否则重开时结果突然出现，而等待过程一点痕迹都没有 -->
    <el-alert
      v-if="finishedInBackground"
      type="info"
      :closable="false"
      show-icon
      style="margin-bottom: 10px"
      title="这次调试已在后台跑完"
    >
      <span
        >用时
        {{ Math.max(1, Math.round(lastRunMs / 1000)) }}
        秒；下面的结论就是它的结果。</span
      >
    </el-alert>

    <el-alert
      v-if="entryError"
      type="warning"
      :closable="false"
      show-icon
      style="margin-bottom: 10px"
      title="自动生成未完成"
    >
      <span>{{ entryError }}</span>
      <el-button size="small" link type="primary" @click="focusRunEntry">
        重新调试搜索
      </el-button>
    </el-alert>
    <!-- 取消等待不是失败（cancelled 标记）：按提示色渲染，别把用户的主动操作画成红色错误 -->
    <el-alert
      v-if="result && result.error"
      :type="result.cancelled ? 'warning' : 'error'"
      :closable="false"
      show-icon
      style="margin-bottom: 10px"
      :title="String(result.error)"
    />
    <el-alert
      v-if="webviewUnsupported.length"
      type="warning"
      :closable="false"
      show-icon
      style="margin-bottom: 10px"
      :title="webviewTitle"
    >
      <div v-for="item in webviewUnsupported" :key="item.phase + item.code">
        {{ item.phase === "matched" ? "命中回填" : "调试主链" }}：{{ item.text
        }}<span v-if="item.where">（{{ item.where }}）</span>
      </div>
    </el-alert>

    <el-empty
      v-if="!steps.length"
      description="没有调试结果；可先检查上面的原因或重新调试"
      :image-size="80"
    />
    <template v-else>
      <div class="debug-workspace">
        <aside class="debug-step-panel">
          <div class="debug-step-panel-title">调试步骤</div>
          <div class="debug-step-tabs">
            <!-- 高亮要跟着「实际显示的那一步」（current 在 activeStep 失效时会回退到
                 steps[0]），否则重跑后会出现「有内容、没有任何页签高亮」 -->
            <button
              v-for="s in steps"
              :key="s.name"
              type="button"
              class="debug-step-tab"
              :class="{ active: !!current && s.name === current.name }"
              @click="selectStep(s.name)"
            >
              <i class="dot" :class="dotClass(s)"></i>
              <span class="debug-step-tab-label">{{
                STEP_LABELS[s.name] || s.name
              }}</span>
              <el-tag
                v-if="diffOf(s.name) && diffOf(s.name).status !== 'same'"
                size="small"
                :type="diffTagType(diffOf(s.name).status)"
                class="diff-tag"
                >{{ statusLabel(diffOf(s.name).status) }}</el-tag
              >
            </button>
          </div>
        </aside>
        <section class="debug-step-main">
          <!-- 与上次相比（ux-debug-loop）：重跑不清场，差异摆在一眼能看见的地方 -->
          <p v-if="diffSummary" class="muted" style="margin: 6px 0 0">
            {{ diffSummary }}
          </p>

          <!-- 本步规则：唯一编辑入口；改一个字符即入草稿并标过期，
            验收仍走「重新调试本步」的真引擎，这里不提供第二种判定。
            详情步覆盖 ruleBookInfo 全组字段，用下拉切换编辑目标。 -->
          <div
            v-if="inlineDef"
            class="inline-rule"
            :data-rule-field="inlineDef.field"
          >
            <div class="inline-rule-label">
              <el-select
                v-if="stepFields.length > 1"
                :model-value="inlineDef.field"
                size="small"
                style="width: 160px"
                @update:model-value="(v) => { activeField = v; }"
              >
                <el-option
                  v-for="d in stepFields"
                  :key="d.field"
                  :value="d.field"
                  :label="d.label"
                />
              </el-select>
              <span v-else>{{ inlineDef.label }}</span>
              <el-button
                size="small"
                link
                type="primary"
                :disabled="rerunning || !canRerun"
                @click="rerunCurrentStep"
                >重调本步验证</el-button
              >
            </div>
            <el-input
              :model-value="currentRule"
              type="textarea"
              :autosize="{ minRows: 2, maxRows: 8 }"
              :placeholder="inlineDef.placeholder"
              @update:model-value="onInlineRuleInput"
            />
          </div>
          <el-empty
            v-if="!inlineDef"
            description="这一步没有单字段规则（发现页走完整链路）"
            :image-size="60"
          />

          <!-- 本步实测摘要来自事件中明确记录的统计/字段；完整事件只在下面展示。 -->
          <section v-if="isEngineResult && current" class="matched-result">
            <div class="debug-evidence">
              <b
                >{{
                  STEP_LABELS[currentSummary.name] || currentSummary.name
                }}结果</b
              >
              <el-tag
                size="small"
                :type="
                  currentSummary.verdict === 'pass'
                    ? 'success'
                    : currentSummary.verdict === 'fail'
                      ? 'danger'
                      : 'info'
                "
              >
                {{
                  currentSummary.verdict === "pass"
                    ? "通过"
                    : currentSummary.verdict === "fail"
                      ? "失败"
                      : "无法判定"
                }}
              </el-tag>
              <el-tag v-if="currentStale" size="small" type="warning"
                >上次实测 · 已过期</el-tag
              >
              <span v-if="currentSummary.total !== null">
                {{ currentSummary.totalLabel }} {{ currentSummary.total }}
              </span>
            </div>
            <div
              v-for="item in currentSummary.fields"
              :key="item.label"
              class="debug-value"
            >
              <span class="debug-value-idx">{{ item.label }}</span>
              <!-- URL 字段（封面/详情页/目录链接）按本步 URL 绝对化后给直接入口 -->
              <a
                v-if="item.url && openableUrl(item.value)"
                :href="openableUrl(item.value)"
                target="_blank"
                rel="noopener noreferrer"
                class="val-open"
                >打开</a
              >
              <pre class="debug-pre debug-pre-wrap">{{
                item.value.length > 300
                  ? item.value.slice(0, 300) + "…（更多见调试事件）"
                  : item.value
              }}</pre>
            </div>
            <p v-if="currentSummary.reason" class="debug-result-reason">
              {{ currentSummary.reason }}
            </p>
            <p
              v-else-if="
                currentSummary.total === null && !currentSummary.fields.length
              "
              class="muted"
            >
              本步未记录可提取的样本；完整过程见调试事件
            </p>
          </section>

          <el-tabs v-model="subTab">
            <el-tab-pane v-if="events.length" name="events">
              <template #label>调试事件 ({{ events.length }})</template>
              <DebugTimeline :events="events" />
            </el-tab-pane>

            <!-- 材料：命中片段与整页源码共用一套只读查看器（原文/格式化/搜索/复制原文） -->
            <el-tab-pane name="material">
              <template #label>{{ materialTitle }}</template>
              <div class="toolbar" style="margin-bottom: 8px">
                <el-radio-group
                  :model-value="materialKind"
                  size="small"
                  @update:model-value="
                    (v) => {
                      materialKind = v;
                      renderLimit = RENDER_CHUNK;
                    }
                  "
                >
                  <el-radio-button value="matched">命中片段</el-radio-button>
                  <el-radio-button value="page">整页源码</el-radio-button>
                </el-radio-group>
                <el-input
                  v-model="searchKey"
                  size="small"
                  style="width: 200px"
                  placeholder="搜索（如 content）"
                  clearable
                />
                <template v-if="hitOffsets.length">
                  <el-button size="small" @click="gotoHit(activeHit - 1)"
                    >上一处</el-button
                  >
                  <el-button size="small" @click="gotoHit(activeHit + 1)"
                    >下一处</el-button
                  >
                  <span class="muted">
                    第 {{ activeHit + 1 }} / {{ hitOffsets.length
                    }}{{ hitsTruncated ? "+" : "" }} 处
                  </span>
                </template>
                <span v-else-if="searchKey.trim()" class="muted">未找到</span>
                <el-radio-group v-model="formatSource" size="small">
                  <el-radio-button :value="false">原文</el-radio-button>
                  <el-radio-button :value="true">格式化</el-radio-button>
                </el-radio-group>
                <el-button
                  size="small"
                  :disabled="!materialRaw"
                  @click="copyMaterial"
                >
                  复制原文
                </el-button>
              </div>
              <p v-if="pageOriginNote" class="muted" style="margin: 0 0 8px">
                <span v-if="currentPage && currentPage.fetched_at">
                  页面抓取于 {{ currentPage.fetched_at }} · </span
                >{{ pageOriginNote }}
              </p>
              <el-alert
                v-if="materialTruncatedNote"
                type="warning"
                :closable="false"
                show-icon
                style="margin-bottom: 8px"
                :title="materialTruncatedNote"
              />
              <template v-if="materialRaw">
                <pre class="debug-pre debug-pre-wrap"><template
              v-for="(seg, i) in segments" :key="i"><span
              v-if="!seg.hit">{{ seg.text }}</span><mark
              v-else :id="'debug-hit-' + seg.index"
              :class="{ 'debug-hit-active': seg.index === activeHit }">{{ seg.text }}</mark></template></pre>
                <div v-if="hasMore" class="toolbar" style="margin-top: 8px">
                  <el-button size="small" @click="loadMore">
                    加载更多（已渲染 {{ renderLimit }} /
                    {{ materialText.length }} 字符）
                  </el-button>
                </div>
              </template>
              <el-empty
                v-else
                :description="materialEmptyHint"
                :image-size="60"
              />
            </el-tab-pane>
          </el-tabs>
        </section>
      </div>
    </template>
  </div>
</template>

<style scoped>
/* 工作台专属令牌挂在组件根节点：调试页换配色不外溢到列表、回收站和任务抽屉 */
.debug-workbench {
  --debug-panel-bg: var(--app-surface);
  --debug-panel-border: var(--app-border-light);
  --debug-surface-muted: var(--el-fill-color-lighter, #fafafa);
}

/* 本步实测摘要：规则框下方的本步主产出 */
.matched-result {
  margin: var(--app-space-2) 0;
}
/* 内联规则框：编辑面用中性灰（它是「正在改的东西」，不是证据材料） */
.inline-rule {
  margin: 0 0 var(--app-space-2);
  padding: var(--app-space-2) 10px;
  background: var(--debug-surface-muted);
  border: 1px solid var(--app-border-light);
  border-radius: var(--app-radius-sm);
}
.inline-rule-label {
  margin-bottom: 4px;
  color: var(--app-text-secondary);
  font-size: 12px;
}
/* 材料（命中片段 / 整页）只读查看器；浏览器画布与本地 CSS 判定已随减法移除。 */
/* 运行态条：sticky 是它存在的意义——滚到证据区仍找得到等待/预算/取消。
   本组件不经 teleport 挂在页面流里，scoped 写在这里生效（对比 #15 的弹窗情形） */
.debug-runchip {
  position: sticky;
  top: 0;
  z-index: 20;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 12px;
  margin-bottom: 10px;
  background: var(--app-surface);
  border: 1px solid var(--el-color-primary-light-5, #a0cfff);
  border-radius: var(--app-radius-md);
  box-shadow: 0 2px 6px rgb(0 0 0 / 8%);
}
.debug-runchip-text {
  flex: 1 1 auto;
  min-width: 0;
  font-size: 13px;
  color: var(--app-text);
}
.val-open {
  margin-left: 6px;
  font-size: 12px;
}
.debug-workspace {
  display: flex;
  gap: 14px;
  align-items: flex-start;
}
.debug-step-panel {
  flex: 0 0 190px;
  position: sticky;
  top: 0;
  padding: var(--app-space-3);
  background: var(--debug-panel-bg);
  border: 1px solid var(--debug-panel-border);
  border-radius: var(--app-radius-lg);
}
/* 运行态条与步骤面板都钉在 top:0，宽屏运行中向下滚时前者会盖住后者顶部（标题+前几个页签）。
   运行态下让面板让位，不运行时不留空档；46px ≈ 条高 42（padding 8×2＋内容 24＋边框 2）+
   余量，防那条文字换行后仍被盖 */
.debug-workbench.running .debug-step-panel {
  top: 46px;
}
.debug-step-panel-title {
  margin: 0 2px var(--app-space-2);
  color: var(--app-text);
  font-size: 13px;
  font-weight: 600;
}
.debug-step-tabs {
  display: flex;
  flex-direction: column;
  gap: 5px;
}
.debug-step-tab {
  display: flex;
  align-items: center;
  gap: 7px;
  width: 100%;
  min-width: 0;
  padding: var(--app-space-2) 9px;
  border: 1px solid transparent;
  border-radius: var(--app-radius-md);
  background: transparent;
  color: var(--app-text-secondary);
  font: inherit;
  font-size: 13px;
  text-align: left;
  cursor: pointer;
}
.debug-step-tab:hover {
  background: var(--el-fill-color-light, #f5f7fa);
}
.debug-step-tab.active {
  border-color: var(--el-color-primary-light-5, #a0cfff);
  background: var(--el-color-primary-light-9, #ecf5ff);
  color: var(--app-primary);
}
.debug-step-tab-label {
  min-width: 0;
  flex: 1 1 auto;
}
.debug-step-main {
  min-width: 0;
  flex: 1 1 auto;
}
/* 原样渲染：App 给的行首已带对齐的 [mm:ss.SSS]，再叠一层我们自己量的耗时
   就是两套时间戳，反而更难读。pre-wrap 保住行内空格 */
.debug-event {
  padding: 1px 0;
  font-family: Consolas, Monaco, monospace;
  font-size: 12px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-all;
}
.debug-evidence {
  display: flex;
  gap: 14px;
  flex-wrap: wrap;
  color: var(--app-text-muted);
  font-size: 12px;
  margin-bottom: var(--app-space-2);
}
.debug-value {
  margin-bottom: 10px;
}
.debug-value-idx {
  color: var(--app-text-muted);
  font-size: 12px;
  margin-bottom: 2px;
}
.debug-pre {
  font-family: Consolas, Monaco, monospace;
  font-size: 12px;
  background: var(--app-bg);
  padding: var(--app-space-2);
  border-radius: var(--app-radius-sm);
  max-height: 52vh;
  overflow: auto;
  margin: 0;
}
.debug-pre-wrap {
  white-space: pre-wrap;
  word-break: break-all;
}
.debug-hit-active {
  outline: 2px solid var(--app-status-fail);
}
.quality-metric {
  color: var(--app-text-muted);
}

@media (max-width: 720px) {
  .debug-workspace {
    display: block;
  }
  .debug-step-panel {
    position: static;
    padding: 8px;
    margin-bottom: 10px;
  }
  .debug-step-panel-title {
    margin-bottom: 6px;
  }
  .debug-step-tabs {
    flex-direction: row;
    overflow-x: auto;
    gap: 6px;
    padding-bottom: 2px;
  }
  .debug-step-tab {
    flex: 0 0 auto;
    width: auto;
    min-width: 86px;
  }
  .debug-step-tab-label {
    flex: 0 0 auto;
  }
}
</style>

<!-- 抽屉 teleport 到 body，内部结构的样式只能写在这里（AGENTS #15）。
     .diff-tag 跟着「与上次相比」的标记走，小到不挤占页签 -->
<style>
.debug-step-tab .diff-tag {
  margin-left: 4px;
  transform: scale(0.85);
}
</style>
