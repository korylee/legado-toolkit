<!--
  调试工作台本体（手工调试主流程）：步骤页签 → 本步规则输入（详情步覆盖 ruleBookInfo
  全组字段）→ 本步实测摘要（事件里有据可查的统计与字段）→ 三个页签（提取结果 /
  调试事件 / 材料）。运行态读 useDebugSession；编辑上下文（源/规则/类型）走 props。
 -->
<script setup>
// 调试工作台（手工主流程）：规则输入 → 本步实测摘要 → 材料（大纲 / 命中片段 / 页面原文，
// 只读查看）。完整事件只在「调试事件」页签。
//
// **引擎只有一台**（App 引擎：连 App / 本机），判定与逐段结论都来自它。
// 命中片段只显示引擎返回的 `steps[].matched_html`，自报「取证，可能不全」；
// 命中多少以真引擎实测为准，本地不提供第二种判定。
import { ref, computed, watch, nextTick, onMounted, onUnmounted } from "vue";
import { ElMessage } from "element-plus";

// 步骤名 → 中文的**唯一**一份（编辑视图共用），别再在本组件里写第二份。
// RULE_FIELD_DEFS 是规则字段的唯一词表（内联编辑框的标签/占位符也从它取）
import {
  STEP_LABELS,
  RULE_FIELD_DEFS,
  FIELD_TO_STRUCTURED,
  AUTOFILL_RULE_FIELDS,
} from "../utils/steps";
import { useDebugSession } from "../composables/useDebugSession";
import { compareRuns, statusLabel } from "../utils/debugCompare";
// 语法速查（唯一一份，编辑弹框与工作台共用）：规则输入区旁的折叠块
import GrammarList from "./GrammarList.vue";
import { stepResult, structuredList } from "../utils/debugStepResult";
// 源码的显示层（折行缩进 + 实体展开，纯函数）：好看的和能抄的是两份东西，见 htmlView
import { formatMaterial } from "../utils/htmlView";
// WebView 能力边界是**运行级事实**：这里只渲染告警条
import {
  unsupportedForStep,
  webviewUnsupportedTitle,
  webviewUnsupportedView,
} from "../utils/webviewCapability";
// 材料栏的标签/默认材料/层证据行：**材料是层的函数**，但「这一层真有什么」由结果数据回答
import {
  layerRow,
  materialOrigin,
  renderLine,
  materialTabs,
  networkText,
  preferredMaterial,
  runtimeText,
  UNFAITHFUL_TEXT,
  canFastVerify,
  verifyAction,
} from "../utils/debugMaterial";
import { agentPlan, ruleTry, ruleOutline } from "../api/rules";
// 一轮 AI 补全的观测与收尾（**唯一一份**）：提交、逐字段事件、结果体、重挂
import { useAgentRun } from "../composables/useAgentRun";
import AgentRunDialog from "./AgentRunDialog.vue";
// 在途调试的相位码 → 中文（唯一一份；后端只给码与事实）
import { debugRunState } from "../utils/debugRun";
// 「下一步做什么」的取词与事实组装（判据在后端 core/agent_plan，前端只送事实）
import { gapLine, planSnapshot } from "../utils/agentPlan";

const props = defineProps({
  initialStep: { type: String, default: "" },
  //: 完整源对象（表单里那份）：定层要读源**自己声明的能力**——`ruleContent.webJs` /
  //: `loginUrl` / URL 规则里的 `webView`（AGENTS #13：能推导的别让用户填）
  source: { type: Object, default: null },
  draftRevision: { type: [Number, String], default: null },
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
  runId,
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
  "rerunFrom",
  "applyRule",
]);

//: 运行条上那一行字。相位码与事实来自后端（`runStatus` 轮询），**词在这里出**
//: （utils/debugRun）。后端说不知道就照实说「已等待 N 秒」，不编进度
const runLine = computed(() => debugRunState(runStatus.value, elapsedMs.value));

//: 运行条的 live tail：最近一条引擎事件（截断）——「现在在干什么」内联可见，
//: 完整时间线在任务中心（那里是同一本账的渲染，这里不再放第二个渲染器）
const runTail = computed(() => {
  const evs = events.value;
  if (!running.value || !evs.length) return "";
  const text = String((evs[evs.length - 1] || {}).text || "");
  return text.length > 100 ? text.slice(0, 100) + "…" : text;
});

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
// explore 是发现链路的产出步（key 带 `发现::` 时后端才产出它）
//: 每次渲染的字符数。整页 HTML 可能 100 万字符，全量进 DOM 会卡
const RENDER_CHUNK = 20000;
//: 搜索最多索引的命中数。整页 HTML 里搜 div / class 必然远超此数，
//: 超限时必须显式告知，否则计数器会把「前 200 处」说成全部
const HIT_LIMIT = 200;
//: 空闲自动快验的等待：停止输入这么久之后才花引擎验一次（编辑期间不断重置）
const QUICK_VERIFY_IDLE_MS = 800;

const activeStep = ref("");

//: 材料查看器；默认看**大纲**（写选择器要看的是标签/class/层级，不是 30 万字符
//: 原文），要看正则/属性原文时切「页面原文」。**显示一律是格式化后的**：原文只在
//: 复制与搜索计数时用（同一个开关既管显示又管复制，迟早把「看到的」当成「原文」）。
const materialKind = ref("outline");
//: 用户在同一步里手选过的材料（步名 → 材料）：换步时默认材料跟着换，但**不抢用户
//: 自己挑的那一份**；换任务清空——那是另一份材料了
const materialPickedFor = ref({});
//: 渲染窗口（原文字符数）：先截断再格式化，见 headRaw
const renderLimit = ref(RENDER_CHUNK);
const searchKey = ref("");
const activeHit = ref(0);

const steps = computed(() => result.value?.steps || []);
const pages = computed(() => result.value?.pages || []);

//: 结果来源。**两台都是 App 引擎**，差别只在环境，所以必须标出来：
//:   - `app`：连 App 实测（手机上跑，登录态/网络出口都是真的）
//:   - `jvm`：本机引擎（**同一段 App 代码**跑在本机：真规则、真 JS；差在环境——
//:     登录态要预热、没有手机的网络出口）
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
//: 引擎按同一套解析器算出的完整条目（搜索/发现 20 条书目、目录样本列表）——
//: 摘要只显示首条，全量在这里展开；null = 该步没有列表材料
const currentList = computed(() => structuredList(current.value || {}));
// ---- 运行时对象：只采形状不采值，挂在 content 步；它作为材料出现在材料栏 ----
const currentRuntime = computed(() => {
  const r = (current.value || {}).runtime;
  return r && typeof r === "object" ? r : null;
});
const currentPage = computed(() => {
  if (!current.value || !current.value.page_id) return null;
  return pages.value.find((p) => p.id === current.value.page_id) || null;
});
//: 这一页的层与证据（后端判完挂在 `pages[].page_layer`）：**材料该看哪一份、规则为什么
//: 取不到，答案都在这行事实里**。只渲染，不给下一步动作——下一步的判据只有一处。
const currentLayer = computed(() => layerRow(currentPage.value));
//: 这份材料**是谁取回来的**：`engine` = 引擎手上那份，`fetch` = 我们另抓的一份。
//: 「验证」验的就是它，所以它必须说得出来（两份可能不是同一页，L2–L4 尤其）
const currentOrigin = computed(() => materialOrigin(currentPage.value));
//: 这一页走没走浏览器渲染（后端判到 `pages[].rendered`）：与「材料是谁取的」是两件事
//: ——`origin=engine` 也可能是静态页，判据只能读渲染事实（AGENTS #12）
const currentRender = computed(() => renderLine(currentPage.value));
//: 源级静态错配（后端 `static_notes`）：与这次抓取无关的事实，直出后端文案
//: （典型：「webJs 配了但 URL 规则没开 webView → 不会生效」）
const staticNotes = computed(() => {
  const notes = (result.value || {}).static_notes;
  return Array.isArray(notes) ? notes.map((n) => String(n || "")).filter(Boolean) : [];
});
//: 本步该给哪个动作（判据在 utils/debugMaterial）：材料不对味时要先取渲染材料——
//: 那与「重调本步验证」是**同一个动作**（`rerunFrom`），只是措辞按材料说真话
const currentRun = computed(() =>
  verifyAction({
    layer: (currentLayer.value || {}).code || "",
    origin: (currentPage.value || {}).origin || "",
  }),
);
const runActionLabel = computed(() =>
  currentRun.value.kind === "render" ? "取渲染材料" : "重调本步验证",
);
//: 这次调试带没带登录态（sidecar 的 `cookie_len` / `cookie_note`）——本机与真机的第一差异。
//: 设备通道没有这份事实（**读不到 ≠ 没带**），那里返回 null
const loginState = computed(() => {
  const r = result.value || {};
  if (String(r.source || "") !== "jvm") return null;
  const len = Number(r.cookie_len || 0);
  if (len > 0) return { text: "带登录态 " + len + " 字符", hint: "" };
  return {
    text: "没带登录态",
    hint: String(r.cookie_note || "") || "源域下没有可注入的 cookie：登录墙的源会取不到内容",
  };
});
//: 正文的产出（几个字 / 几页 / 首段 / 全文）：只有正文步非空
const currentContent = computed(() => currentSummary.value.content || null);
//: 这一步的材料标签：真有的才出（判据在 utils/debugMaterial）
const currentTabs = computed(() =>
  materialTabs({
    name: (current.value || {}).name || "",
    content: currentContent.value,
    runtime: currentRuntime.value,
    network: (result.value || {}).network || null,
  }),
);
/** 摘要里 URL 字段的「打开」入口：会拼出一个看着像链接、点开 404 的地址，
 *  比不给按钮更糟。 */
function openableUrl(v) {
  const s = String(v || "").trim();
  if (!s || /\s/.test(s)) return "";
  const base = (currentPage.value && currentPage.value.url) || "";
  if (/^https?:\/\//i.test(s)) return s;
  if (s.startsWith("//")) return "https:" + s; // 协议相对
  if (s.startsWith("/") && base) {
    try {
      return new URL(s, base).href;
    } catch (e) {
      return "";
    }
  }
  return "";
}
// —— 材料栏宽度（可拖、存本地）：40% 是按屏幕拍的，源码要宽要窄是每个站点的事 ——
//: 存的是 px；不存 = 用 CSS 里的默认比例（40%）。窄容器下整栏堆叠，拖拽条隐藏。
const MATERIAL_WIDTH_KEY = "legado.debugMaterialWidth";
const MATERIAL_MIN_PX = 280;
const MATERIAL_MIN_MAIN_PX = 420;
const materialEl = ref(null);
const materialWidth = ref(readMaterialWidth());

function readMaterialWidth() {
  try {
    const v = Number.parseInt(localStorage.getItem(MATERIAL_WIDTH_KEY) || "", 10);
    return Number.isFinite(v) && v >= MATERIAL_MIN_PX ? v : 0;
  } catch (e) {
    return 0; // localStorage 不可用（隐私模式）不拦功能
  }
}
const materialStyle = computed(() =>
  materialWidth.value ? { flexBasis: materialWidth.value + "px" } : {},
);
function resetMaterialWidth() {
  materialWidth.value = 0;
  try {
    localStorage.removeItem(MATERIAL_WIDTH_KEY);
  } catch (e) {
    /* 同上 */
  }
}

let dragState = null;
function startMaterialDrag(e) {
  const el = materialEl.value;
  const root = el && el.parentElement;
  if (!el || !root) return;
  const width = el.getBoundingClientRect().width;
  // 上限按容器算：主列至少要留 MATERIAL_MIN_MAIN_PX，别把编辑面挤没
  const max = Math.max(MATERIAL_MIN_PX,
                       root.getBoundingClientRect().width - MATERIAL_MIN_MAIN_PX);
  dragState = { startX: e.clientX, startWidth: width, max };
  e.preventDefault();
  window.addEventListener("pointermove", onMaterialDrag);
  window.addEventListener("pointerup", endMaterialDrag);
}
function onMaterialDrag(e) {
  if (!dragState) return;
  const next = dragState.startWidth - (e.clientX - dragState.startX);
  materialWidth.value = Math.round(
    Math.min(Math.max(next, MATERIAL_MIN_PX), dragState.max));
}
function endMaterialDrag() {
  dragState = null;
  window.removeEventListener("pointermove", onMaterialDrag);
  window.removeEventListener("pointerup", endMaterialDrag);
  try {
    if (materialWidth.value) {
      localStorage.setItem(MATERIAL_WIDTH_KEY, String(materialWidth.value));
    }
  } catch (e) {
    /* 同上 */
  }
}
onUnmounted(endMaterialDrag);

// —— 只读材料查看器（大纲 / 命中片段 / 整页源码 共用一套）——
// 材料就是引擎带回或补抓的原文；格式化只改显示（formatMaterial），复制永远是原文。
// 大纲来自后端（`/rules/outline`，与 AI 提示词同一份实现），前端不解析 HTML。
const outlineText = ref("");
const outlineLoading = ref(false);
const outlineError = ref("");
const outlineTruncated = ref(false);
//: 这份大纲是哪一步的（同一份不重复拉；换步/换任务号才重拉）
const outlineFor = ref("");

async function loadOutline() {
  const step = (current.value || {}).name || "";
  if (!runId.value || !step) return;
  if (outlineLoading.value) return;
  outlineLoading.value = true;
  outlineError.value = "";
  try {
    const out = await ruleOutline({ job_id: runId.value, step });
    outlineText.value = String((out && out.text) || "");
    outlineTruncated.value = !!(out && out.truncated);
    outlineFor.value = step;
  } catch (e) {
    outlineError.value = String((e && e.message) || e);
    outlineText.value = "";
  } finally {
    outlineLoading.value = false;
  }
}
//: 这一步默认先看哪份材料：`运行时形状 → 接口条目 → 正文全文 → 大纲` 里第一个真有的
//: （判据在 utils/debugMaterial）。用户在同一步里手选过就听他的，选的那份不在了才回落。
function applyDefaultMaterial() {
  const step = (current.value || {}).name || "";
  const values = currentTabs.value.map((tab) => tab.value);
  const picked = materialPickedFor.value[step];
  const want = values.includes(picked) ? picked : preferredMaterial(currentTabs.value);
  if (want) materialKind.value = want;
}

//: 「下一步做什么」只由后端判（`core/agent_plan`），这里只存那一句话与失败原因。
//: **只问正文这一步**：判据虽然按层分段通用，但其余段的缺口码还没补齐，摊开只会多出
//: 一堆说不到点上的话——先接一格，接稳了再扩（见 `planSnapshot` 的调用条件）。
const planLine = ref(null);
const planError = ref("");

async function loadPlan() {
  planLine.value = null;
  planError.value = "";
  const step = current.value || null;
  if (!step || String(step.name || "") !== "content") return;
  const body = planSnapshot({
    source: props.source,
    step,
    page: currentPage.value,
    layer: (currentLayer.value || {}).code || "",
    channel: String((result.value || {}).source || ""),
    webviewUnsupported: unsupportedForStep(result.value, String(step.url || "")),
  });
  if (!body) return;
  try {
    planLine.value = gapLine(await agentPlan(body));
  } catch (e) {
    // 「下一步做什么」取不到也要说：静默消失会让界面看着一切正常（AGENTS #4）
    planError.value = "判据没取到：" + String((e && e.message) || e);
  }
}

watch([() => (current.value || {}).name, runId, resultRevision], () => {
  void loadPlan();
});

// 换步或换任务号：默认材料跟着这一步换，这份大纲作废（看大纲时按需重拉——不预取，
// 多数时候用户在改规则）
watch([() => (current.value || {}).name, runId], ([, nextRun], [, prevRun]) => {
  if (nextRun !== prevRun) materialPickedFor.value = {};
  applyDefaultMaterial();
  outlineFor.value = "";
  outlineText.value = "";
  outlineTruncated.value = false;
  outlineError.value = "";
  if (materialKind.value === "outline") void loadOutline();
});
watch(materialKind, (kind) => {
  if (kind !== "outline") return;
  const step = (current.value || {}).name || "";
  if (outlineFor.value === step && outlineText.value) return;
  void loadOutline();
});

const materialRaw = computed(() => {
  if (materialKind.value === "outline") return outlineText.value;
  if (materialKind.value === "page")
    return (currentPage.value && currentPage.value.html) || "";
  if (materialKind.value === "content")
    return (currentContent.value && currentContent.value.fullText) || "";
  if (materialKind.value === "runtime") return runtimeText(currentRuntime.value || {});
  if (materialKind.value === "network")
    return networkText((result.value || {}).network || []);
  return (current.value || {}).matched_html || "";
});
const materialEmptyHint = computed(() => {
  if (materialKind.value === "outline") {
    if (outlineLoading.value) return "正在取大纲…";
    if (outlineError.value) return outlineError.value;
    if (!runId.value) return "大纲要用本机引擎那次调试的页面材料：先跑一次本机引擎调试";
    return "这一步没有页面材料";
  }
  if (materialKind.value === "page") return "这一步没有抓到页面";
  if (materialKind.value === "content") return "这一步没有取到正文文本";
  if (materialKind.value === "runtime") return "这次没采到运行时形状";
  if (materialKind.value === "network") return "这次没抓到接口请求";
  if (isAppResult.value)
    return "App 调试通道只推文本，取不到命中片段；改用本机引擎调试可以看到";
  return "本机引擎未返回命中片段，规则可能没选中任何节点";
});
const materialTruncatedNote = computed(() => {
  // 大纲/命中片段各自的自报不全；「原文被截断」只属于页面原文那一份——
  // 别的材料套上它会说成「这份材料被截了」（其实是另一份的事）
  if (materialKind.value === "outline") {
    return outlineTruncated.value
      ? "大纲只到前 400 行——后面还有内容，完整结构看「页面原文」"
      : "";
  }
  if (materialKind.value === "page") {
    return currentPage.value && currentPage.value.truncated
      ? "原文 " +
          currentPage.value.len +
          " 字符，已截断到前 " +
          currentPage.value.html.length +
          " 字符"
      : "";
  }
  if (materialKind.value === "matched") {
    return /…（已截断/.test(materialRaw.value) ? "命中子树过长，已截断" : "";
  }
  return "";
});
// 命中片段是**取证**而非完整命中集合（引擎只回传前几个节点）：标题必须自报不全，
// 不能让「片段里有 3 条」被读成「引擎只取到 3 条」。
const MATERIAL_TITLES = {
  outline: "DOM 大纲（标签 · class · 层级；括号里的短文本就是判据）",
  page: "页面原文（显示已格式化，复制的是原文）",
  matched: "命中片段（取证，可能不全）",
  content: "正文全文（引擎这次取到的）",
  runtime: "运行时形状（只采类型 / 键名 / 计数，不采值）",
  network: "接口条目（本次运行最后一次渲染发过的请求）",
};
const materialTitle = computed(() => MATERIAL_TITLES[materialKind.value] || "");
const pageOriginNote = computed(() => {
  // 与字段行旁那行出身**同一份判据**（utils/debugMaterial.materialOrigin）：两处各写一份
  // 迟早会说成两句话
  if (materialKind.value !== "page" || !currentOrigin.value) return "";
  return currentOrigin.value.label +
    (currentOrigin.value.kind === "engine" ? "：过了它的 JS / cookie / UA" : "");
});

//: 用户手选材料：记住是**这一步**选的（换步时默认材料才不会被上一次的手选顶住）
function pickMaterial(value) {
  const step = (current.value || {}).name || "";
  materialKind.value = value;
  renderLimit.value = RENDER_CHUNK;
  materialPickedFor.value = { ...materialPickedFor.value, [step]: value };
}

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

//: 每步的 verdict 与原因（一份映射给整条步骤条用）。verdict 与原因来自引擎结果；
//: **过期不再按步挂芯片**——上面那条「规则已修改…」的 alert 已经说了全局结论，
//: 每个步骤再挂一枚只是把步骤条变彩条
const stepMetaMap = computed(() => {
  const out = {};
  for (const s of steps.value) {
    const sum = stepResult(s);
    out[s.name] = {
      verdict: String(s.verdict || sum.verdict || "").trim(),
      reason: String(sum.reason || "").trim(),
    };
  }
  return out;
});
function stepMetaOf(name) {
  return stepMetaMap.value[name] || { verdict: "", reason: "" };
}
function verdictLabel(verdict) {
  return { pass: "通过", fail: "失败", unknown: "无法判定" }[verdict] || "";
}
function verdictTagType(verdict) {
  return ({ pass: "success", fail: "danger" }[verdict]) || "info";
}
// 搜索在**完整原文**上做（纯字符串扫描，结果不进 DOM），最多记 HIT_LIMIT 处
const hitOffsets = computed(() => {
  const key = searchKey.value.trim();
  const html = materialRaw.value;
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
  const html = materialRaw.value;
  if (!key || !html) return false;
  const last = offsets[offsets.length - 1];
  return html.indexOf(key, last + Math.max(1, key.length)) >= 0;
});

//: 渲染窗口（按**原文**字符数算）：先截断再格式化——30 万字符整份格式化再截前
//: 20000，等于每次重算都为看不见的部分付一遍钱（原先只在开「格式化」时付）
const headRaw = computed(() => materialRaw.value.slice(0, renderLimit.value));
const hasMore = computed(() => renderLimit.value < materialRaw.value.length);
//: 屏幕上真正显示的那份：**总是格式化后的**（原文只用于复制与搜索计数）
const headText = computed(() => formatMaterial(headRaw.value));

//: 命中在**显示文本**里的位置（模板按它画高亮）：格式化会改字符偏移，原文偏移
//: 直接拿去切片必然错位。按顺序找，第 i 处对应原文第 i 处——顺序在格式化下不变
//: （去空白/展开实体/缩进都不会打乱命中的先后）。
const displayHitOffsets = computed(() => {
  const key = searchKey.value.trim();
  const head = headText.value;
  if (!key || !head) return [];
  const out = [];
  let from = 0;
  while (out.length < HIT_LIMIT) {
    const i = head.indexOf(key, from);
    if (i < 0) break;
    out.push(i);
    from = i + Math.max(1, key.length);
  }
  return out;
});
//: 渲染窗口里应有多少处（原文口径）。与显示口径不等时说明高亮位置不全——如实说，
//: 不假装对齐（AGENTS #12：读不到/对不上都不许当成「就是这些」）
const hitsInWindow = computed(() => {
  const limit = headRaw.value.length;
  return hitOffsets.value.filter((off) => off < limit).length;
});
const hitsMisaligned = computed(
  () => !!searchKey.value.trim()
    && hitsInWindow.value !== displayHitOffsets.value.length,
);

// 把已渲染的片段按命中位置切成 [普通, 高亮, 普通, ...]
const segments = computed(() => {
  const head = headText.value;
  const key = searchKey.value.trim();
  if (!key) return [{ text: head, hit: false, index: -1 }];
  const segs = [];
  let cursor = 0;
  displayHitOffsets.value.forEach((off, hitIndex) => {
    if (off < cursor) return;
    if (off > cursor)
      segs.push({ text: head.slice(cursor, off), hit: false, index: -1 });
    segs.push({
      text: head.slice(off, off + key.length),
      hit: true,
      index: hitIndex,
    });
    cursor = off + key.length;
  });
  if (cursor < head.length)
    segs.push({ text: head.slice(cursor), hit: false, index: -1 });
  return segs;
});

onMounted(() => {
  if (result.value) {
    activeStep.value =
      props.initialStep || (steps.value[0] && steps.value[0].name) || "";
    applyDefaultMaterial();
    // 默认材料是大纲时挂载就把它取回来（其余几份随结果体已经在手上）
    if (materialKind.value === "outline") void loadOutline();
  }
  // 刷新/重开页面时把那次**还在跑**的调试重新挂上：任务行还在就挂得上。
  // 观测登记放在进程内存里时，一次刷新就等于"那次调试从界面上消失了"。
  void resumeRunning();
  // AI 补全也在跑的那种情况：同一条重挂纪律
  void resumeAiJob();
});
watch(result, (show) => {
  if (!show) return;
  // 抽屉常开时也可能收到**别处跑完**的结果（另一个标签页 / 后台那次收尾）：
  // 把「打开时刻」推到这一刻，「后台跑完」那条提示才不会把它误标成没看见过
  openedAt.value = Date.now();
  if (show)
    activeStep.value =
      props.initialStep || (steps.value[0] && steps.value[0].name) || "";
  // 新结果 = 这一步的材料可能换了（比如这次才采到运行时形状）
  applyDefaultMaterial();
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
  renderLimit.value = RENDER_CHUNK;
  searchKey.value = "";
  activeHit.value = 0;
}

//: 搜索跟焦点走：字段的输入框获得焦点时，把该字段规则的首段选择器灌进
//: 材料搜索框并跳到第一处命中——「改规则」和「看页面」不用来回滚。
//: 首段不像选择器（@js:、属性名等）就不灌，不往搜索框里塞垃圾。
function onFieldFocus(def) {
  if (!def) return;
  const rule = String(flatRuleOf(def.field) || "").trim().split("##")[0];
  const first = rule.split("@")[0].trim();
  const isSelector = /^(class|id|tag)\./.test(first)
    || /^[.#][\w-]/.test(first);
  if (!isSelector) return;
  // 焦点进规则框 = 要写选择器：材料那栏切回大纲（默认那份是「取到了什么」，不是结构）
  if (materialKind.value !== "outline") pickMaterial("outline");
  searchKey.value = first;
  activeHit.value = 0;
  // 大纲还没拉过：拉回来再定位（拉取与报错都在 loadOutline 里，这里只补一次触发）
  if (!materialRaw.value) {
    void loadOutline().then(() => {
      if (hitOffsets.value.length) gotoHit(0);
    });
    return;
  }
  if (hitOffsets.value.length) gotoHit(0);
}

//: 内联规则编辑：当前步骤的全部可编辑字段（RULE_FIELD_DEFS 同一词表），**一律平铺**
//: ——详情步 8 个字段、正文步 3 个，下拉逐个切换是「填一个切一下」的交互税；
//: 单字段的步（目录）平铺就是一个框，观感与下拉相同。
const stepFields = computed(() =>
  RULE_FIELD_DEFS.filter(
    (d) => d.step === ((current.value || {}).name || ""),
  ),
);
//: 编辑面的容器标（`ruleBookInfo.*` 这类）：这一组的字段属于哪个规则组，取第一个
//: 字段的组名。行内每行各自的 `data-rule-field` 是精确定位用的那一份
const ruleGroupAttr = computed(() =>
  stepFields.value[0] ? stepFields.value[0].group + ".*" : "",
);
//: structured 里该字段的当前实测值（引擎取到的）——填的时候立刻有反馈，
//: 空就显眼地标「未取到」。详情步的 structured 是**平对象**，所以 ruleSearch.bookUrl
//: 的产物（详情页地址）从这一步自己的 `current.url` 取。
//: 规则字段 → structured 键的映射在 `utils/steps`（AI 写入落点的逆表也读它）。
function structuredValueOf(field) {
  // 详情步自己的 URL 就是 ruleSearch.bookUrl 的产出（平铺行显示它）
  if (field === "ruleSearch.bookUrl") return String((current.value || {}).url || "");
  const s = (current.value || {}).structured;
  const key = FIELD_TO_STRUCTURED[field];
  if (!s || !key) return "";
  return String(s[key] || "");
}
//: 行内实测值的显示上限：正文全文可能有几万字，挂在 title 上只会把浏览器拖住；
//: 全文在「正文结果」与材料栏里，这里只回答「取到了没有、头一句是什么」
const FACT_VALUE_MAX = 300;
function clipFactValue(value) {
  const text = String(value == null ? "" : value);
  return text.length > FACT_VALUE_MAX ? text.slice(0, FACT_VALUE_MAX) + "…" : text;
}
//: 正文步的实测值提供者（**不走 FIELD_TO_STRUCTURED**，理由见上）。
//: 口径与 `utils/debugStepResult` 的正文事实同一份：事件流优先、结构化样本回落，
//: 所以连 App 通道（没有 structured）也拿得到值
function contentEngineValue(field) {
  const facts = currentContent.value;
  if (!facts) return null;
  if (field === "ruleContent.content") {
    return facts.hasText
      ? { count: 1, values: [clipFactValue(facts.fullText)], error: "" }
      : null;
  }
  if (field === "ruleContent.nextContentUrl") {
    const links = Array.isArray(facts.nextLinks) ? facts.nextLinks : [];
    return links.length
      ? { count: links.length, values: links.map(clipFactValue), error: "" }
      : null;
  }
  return null;
}
//: 该字段这一轮引擎观测到的值（`{count, values, error}` 或 null = 没有实测值）
function engineValueOf(field) {
  const fact = contentEngineValue(field);
  if (fact) return fact;
  const value = structuredValueOf(field);
  return value ? { count: 1, values: [value], error: "" } : null;
}
// 换步/换结果：字段值重新由引擎观测写入（谁跑完谁写入，编辑即清——只有这一个状态）
watch([activeStep, result], () => {
  writeEngineValues();
  prefillSuggestions();
});

function rerunCurrentStep() {
  const name = (current.value || {}).name;
  if (name) emit("rerunFrom", name);
}

//: 平铺编辑单字段的输入（程序写入用：AI 补全/初选——它们写入后紧跟着就有自己的
//: 观测写回来，不清字段值也不安排快验）
function onFlatRuleInput(field, value) {
  emit("applyRule", { field, rule: value });
}

//: **用户**编辑：同上，外加把字段值清掉——旧规则取到的值描述的不是眼前这条规则，
//: 留着它就是把旧观测当成当前的（编辑即作数，不用簿记判"哪个新"）
function onUserRuleInput(field, value) {
  fieldValues.value = { ...fieldValues.value, [field]: null };
  onFlatRuleInput(field, value);
  scheduleQuickVerify(field);
}

//: 该字段草稿的只读镜像（平铺行用）：源草稿里那份
function flatRuleOf(field) {
  const [group, key] = String(field || "").split(".");
  const rs = (props.source || {})[group] || {};
  return String(rs[key] || "");
}

// ---- AI 补全 / 行内验证（详情组）----
// 建议只有两个出处：免费初选（attach_autofill 随两条通道带回，**只有 preselect
// 敢拍板的那条带 rule**）与模型提议（agent-loop，提议完先 rule_try 自查）。
// **落进输入框的都验过**：免费的靠实测值拍板，模型的靠自查取到值；验不过的
// 一律不出规则文本——用户实测反馈「填的几乎都不可用，不如不填」，宁可空着。
// 行内验证（rule_try）只回答「这条草稿选不选得中」，验收仍走「重调本步验证」
// （AGENTS #3）。AI 补全是花钱动作，只能由用户显式点按钮触发。
//
// **一轮 AI 补全的观测与收尾在 `composables/useAgentRun`**（唯一一份）：提交任务、
// 吃观测流、逐字段落框、拉结果体。本组件只做两件它才知道的事——落框写进草稿、
// 打开明细弹窗。
const agent = useAgentRun();
const aiRunning = agent.running;
const agentDialog = ref(null);
//: 提交时整组补全要带上的字段集（后端 FILL_SPECS 的蛇形键，按本步的词表反查）
const aiAllFields = computed(() =>
  stepFields.value.map((d) => autofillKeyOf((current.value || {}).name || "", d.field)).filter(Boolean),
);

//: 刷新页面时把那次**还在跑**的 AI 补全重挂上（任务行还在就挂得上）——观测登记
//: 放在进程内存里时，一次刷新就等于「那次补全从界面上消失了」。
//: 重挂挂到**别的调试**起的那次补全时不写框：规则会落进错的源（判据是后台那一轮
//: 的 `debug_job_id`，由 useAgentRun 从 `ai_started` 取）。
async function resumeAiJob() {
  if (!runId.value || aiRunning.value) return;
  await agent.resume({
    debugJobId: runId.value,
    step: (current.value || {}).name || "",
    // 终态才落框：重挂的那一轮还在跑时没有结果体可写
    onDone: (result) => {
      if (result && String(agent.debugJobIdOf()) === String(runId.value)) {
        applyAiResult(result, { fill: true });
      }
    },
  });
}
//: 建议值（field 路径 → 规则文本）：底纹的判据——草稿值等于它就是「未改过的建议」，
// 纯派生，不需要手工维护状态（AGENTS #13：算得出的不让用户管）
const suggestedRules = ref({});
//: **字段值是唯一一个状态**（field → {loading} | {count, values, error} | null）：
//: 引擎跑完写入、快验跑完写入、编辑即清——谁最新谁在，没有"哪个新"的判据
const fieldValues = ref({});

//: 本步的内嵌建议（免费段，attach_autofill 随调试带回）
const autofillData = computed(() => {
  const data = (current.value || {}).autofill;
  return data && Array.isArray(data.fields) ? data : null;
});
//: AI 补全：材料在任务行里，所以只支持本机引擎通道
const canAiFill = computed(
  () => !!autofillData.value && debugChannel.value === "jvm" && !!runId.value,
);
const canAiFillNote = computed(() =>
  debugChannel.value === "app"
    ? "AI 补全要用本机引擎的任务材料；连 App 调试的结论照常可看"
    : "先跑一次本机引擎的完整调试");


//: 免费初选拍板的值直接落进输入框（只填空的，不覆盖用户内容）：写草稿走
//: applyRule，底纹标 suggestedRules，用户一改即消失
function prefillSuggestions() {
  const step = (current.value || {}).name || "";
  const map = AUTOFILL_RULE_FIELDS[step] || {};
  for (const row of (autofillData.value || {}).fields || []) {
    if (!row.rule) continue;
    const target = map[row.field];
    if (!target) continue;
    if (String(flatRuleOf(target) || "").trim()) continue;
    suggestedRules.value = { ...suggestedRules.value, [target]: String(row.rule) };
    onFlatRuleInput(target, String(row.rule));
  }
}

//: 引擎观测写入字段值：调试结果到达 / 换步时，用 structured 写。
//: **版次一致才写**——结果里的值是提交那一刻的规则快照跑出来的，不一致时它
//: 描述的不是眼前草稿（步骤条上方那条 alert 说的就是这件事）
function writeEngineValues() {
  const fresh = resultRevision.value != null && props.draftRevision != null
    && resultRevision.value === props.draftRevision;
  if (!fresh) return;
  const next = {};
  for (const d of stepFields.value) {
    const value = engineValueOf(d.field);
    if (value) next[d.field] = value;
  }
  fieldValues.value = next;
}

//: 快验小标签：点开看值（有值才展开；失败标签只有悬停原因）
const quickOpen = ref({});
function toggleQuickValues(field) {
  const fact = fieldValues.value[field];
  if (!fact || !fact.values || !fact.values.length) return;
  quickOpen.value = { ...quickOpen.value, [field]: !quickOpen.value[field] };
}
function quickTagType(fact) {
  if (!fact) return "info";
  return fact.error ? "danger" : (fact.count ? "success" : "info");
}


/** AI 补全：提交成**任务**，然后吃它的观测流（观测与收尾在 `useAgentRun`）。
 *
 * 为什么不是一次同步请求：一轮补全 = 若干次模型调用 + 若干次 rule_try，几十秒到
 * 几分钟。同步请求的等待期界面上没有任何产物（进度只能是假的），刷新就丢。
 *
 * 提交后**立刻开明细弹窗**：过程（正在提议哪个字段、哪条验过、取到几条）是这一轮的
 * 主内容，弹窗一列到底；工作台这边只留输入框里的落框与字段行上的结论。
 */
async function startAiFill() {
  if (aiRunning.value || !canAiFill.value) return;
  await runAiJob(null);
}

/** 单字段补全：同一套流程，只带一个字段（一颗按钮只影响一行）。 */
async function fieldAi(field) {
  if (aiRunning.value || !canAiFill.value || !field) return;
  await runAiJob(field);
}

async function runAiJob(onlyField) {
  const step = (current.value || {}).name || "bookUrl";
  const only = onlyField ? [autofillKeyOf(step, onlyField)].filter(Boolean) : [];
  // 拿到任务号就开弹窗：过程要看现场，跑完再开等于什么也没看到
  const { jobId, done } = await agent.submit({
    debugJobId: runId.value,
    step,
    only,
    allFields: aiAllFields.value,
  });
  if (agentDialog.value) agentDialog.value.openRun();
  if (!jobId) return;
  const result = await done;
  if (result) applyAiResult(result, { fill: true });
}

/** 「再补这 N 个字段」：弹窗把没补上的字段集给回来，同一套流程只带它们。 */
async function rerunAgentFields(fields) {
  const step = (current.value || {}).name || "bookUrl";
  const { done } = await agent.submit({
    debugJobId: runId.value,
    step,
    only: fields,
    allFields: aiAllFields.value,
  });
  const result = await done;
  if (result) applyAiResult(result, { fill: true });
}

/** 结果体兜底：过程行漏了（流接晚了）也要把验过的规则落进输入框。 */
function applyAiResult(result, { fill } = {}) {
  const step = (current.value || {}).name || "bookUrl";
  const map = AUTOFILL_RULE_FIELDS[step] || {};
  const rows = (result && result.fields) || [];
  for (const row of rows) {
    const target = map[row.field];
    const checked = row.try_result;
    if (!target || !row.rule) continue;
    // 快验答不了（材料 × 规则形态不匹配）：规则照落（AI 写的就填），但不升格——
    // 「快验通过」不是「可用」（D1），验收仍走重调本步；底纹也就不给
    if (row.pending_engine || (checked && checked.unfaithful)) {
      if (fill && !String(flatRuleOf(target) || "").trim()) onFlatRuleInput(target, String(row.rule));
      continue;
    }
    if (!checked || !checked.ok) continue;
    if (fill && String(flatRuleOf(target) || "").trim()) continue;
    suggestedRules.value = { ...suggestedRules.value, [target]: String(row.rule) };
    if (fill) onFlatRuleInput(target, String(row.rule));
    // try_result 是 rule_try 的应答（带值）：原样收下，标签/悬停/展开都从它取
    fieldValues.value = {
      ...fieldValues.value,
      [target]: {
        count: Number(checked.count || 0),
        values: (checked.values || []).map((v) => String(v)),
        error: String(checked.error || ""),
      },
    };
  }
}

/** 反查：规则字段路径 → 后端 FILL_SPECS 的蛇形键（AI 任务按它认字段）。 */
function autofillKeyOf(step, field) {
  const map = AUTOFILL_RULE_FIELDS[step] || {};
  return Object.keys(map).find((k) => map[k] === field) || "";
}

//: 快验的在途代次与空闲计时器（迟到的旧请求不许覆盖新结论）
const quickGen = {}; // field → 代次
const quickTimers = {}; // field → 空闲计时器

/** 空闲自动快验：停止输入 ~0.8 秒后，验**刚刚改过的那一个字段**。
 *
 * 只验改过的那一个（不是整组）：成本可控，而且它正是用户盯着的那一行。
 * 编辑期间每敲一个字就重置计时器，所以不会把打字过程变成一串引擎排队。
 * Ctrl+Enter 立即验（不等空闲）。
 */
function scheduleQuickVerify(field) {
  if (!field || !runId.value) return; // 连 App 没有任务行：没有材料可验
  if (!verifyAllowed(field)) return; // 没有快验口径的行（分页 / webJs）不排
  window.clearTimeout(quickTimers[field]);
  quickTimers[field] = window.setTimeout(() => {
    if (!String(flatRuleOf(field) || "").trim()) return;
    void verifyField(field);
  }, QUICK_VERIFY_IDLE_MS);
}

//: 这一行能不能行内「验证」，两半：**这一行有没有快验口径**（`def.verify`，如分页取一组
//: 链接）+ **材料与规则形态对得上**（`canFastVerify` 镜像后端 `core.material`）。
//: 字段族那半边（页面脚本要不要 `page_vars`）由请求里的 `field` 交给后端判。
function verifyAllowed(field) {
  const def = stepFields.value.find((d) => d.field === field);
  if (!def || def.verify === "none") return false;
  return canFastVerify((currentPage.value || {}).material_kind, flatRuleOf(field));
}

//: 快验（rule_try）：验的是**这行草稿规则**，结论贴在它自己的行上。
//: 它只回答「选不选得中」，验收仍走「重调本步验证」（AGENTS #3）。
async function verifyField(field) {
  // 连 App 调试没有任务行（材料在后端），行内验证只在本机引擎通道可用
  if (!runId.value || !field) return;
  const rule = String(flatRuleOf(field) || "").trim();
  if (!rule) {
    // 空规则点下去什么都不跑：不给原因的话就是静默失败（AGENTS #4）
    fieldValues.value = {
      ...fieldValues.value,
      [field]: { count: 0, values: [], error: UNFAITHFUL_TEXT.rule_empty },
    };
    return;
  }
  const gen = (quickGen[field] = (quickGen[field] || 0) + 1);
  fieldValues.value = {
    ...fieldValues.value,
    [field]: { loading: true },
  };
  try {
    const out = await ruleTry({
      job_id: runId.value,
      step: (current.value || {}).name || "bookUrl",
      field: autofillKeyOf((current.value || {}).name || "bookUrl", field),
      rule,
    });
    if (gen !== quickGen[field]) return; // 期间又改了：这次结论已经过期
    const ok = !!(out && out.ok);
    // 快验答不了这条（材料种类 × 规则形态不匹配）：说清是「快验测不了」，
    // 不冒充「这条规则取不到」——方向相反的结论比没有结论更坏（AGENTS #4）
    if (out && out.unfaithful) {
      fieldValues.value = {
        ...fieldValues.value,
        [field]: {
          count: 0,
          values: [],
          error: UNFAITHFUL_TEXT[out.reason_code] || String(out.reason_code || "快验测不了这条规则"),
        },
      };
      return;
    }
    // 应答带值（values ≤5 条）：原样收下，标签/悬停/展开都从它取
    fieldValues.value = {
      ...fieldValues.value,
      [field]: {
        count: Number((out && out.count) || 0),
        values: ((out && out.values) || []).map((v) => String(v)),
        error: ok ? "" : String((out && out.error) || "未知原因"),
      },
    };
  } catch (e) {
    if (gen !== quickGen[field]) return;
    fieldValues.value = {
      ...fieldValues.value,
      [field]: {
        count: 0,
        values: [],
        error: String((e && e.message) || e),
      },
    };
  }
}


//: autofill 行的 field（FILL_SPECS 蛇形键）→ 写入草稿的规则字段路径：**唯一一份**在
//: `utils/steps`（AI 补全弹窗也要按同一份把字段名说成人话）。这里只留反查。
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
    <!-- 运行态的**固定显示**：定高页面里它常驻顶部（不滚）。
         那一行字说**当时到底在等什么**：已等多久 + 此刻占着引擎的是谁（utils/debugRun），
         下面一行是最近一条引擎事件（live tail）——「现在在干什么」内联可见，
         完整时间线在任务中心 -->
    <div v-if="running" class="debug-runchip">
      <span class="debug-runchip-text">
        {{ runLine.text
        }}<template v-if="budget"> · 预算 {{ budget }} 秒</template>
        <span v-if="runTail" class="debug-runchip-tail">{{ runTail }}</span>
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
        <!-- 步骤上移成顶部条：4 个条目不值得一条 190px 竖栏，省下的宽度给编辑面与材料面 -->
        <div class="debug-step-strip">
          <div class="debug-step-strip-title">调试步骤</div>
          <div class="debug-step-tabs">
            <!-- 高亮要跟着「实际显示的那一步」（current 在 activeStep 失效时会回退到
                 steps[0]），否则重跑后会出现「有内容、没有任何页签高亮」 -->
            <button
              v-for="s in steps"
              :key="s.name"
              type="button"
              class="debug-step-tab"
              :class="{ active: !!current && s.name === current.name }"
              :title="stepMetaOf(s.name).reason"
              @click="selectStep(s.name)"
            >
              <i class="dot" :class="dotClass(s)"></i>
              <span class="debug-step-tab-label">{{
                STEP_LABELS[s.name] || s.name
              }}</span>
              <!-- verdict 芯片：这一步过了没有，是唯一该常驻可见的整步结论。
                   plain 效果降噪：四个步骤各挂一枚，实底标签会让步骤条变成彩条 -->
              <el-tag
                v-if="stepMetaOf(s.name).verdict"
                size="small"
                effect="plain"
                :type="verdictTagType(stepMetaOf(s.name).verdict)"
                class="diff-tag"
                >{{ verdictLabel(stepMetaOf(s.name).verdict) }}</el-tag
              >
              <el-tag
                v-if="diffOf(s.name) && diffOf(s.name).status !== 'same'"
                size="small"
                effect="plain"
                :type="diffTagType(diffOf(s.name).status)"
                class="diff-tag"
                >{{ statusLabel(diffOf(s.name).status) }}</el-tag
              >
            </button>
          </div>
        </div>
        <!-- 面板行：主列与材料栏各自整列滚动（定高页面里这行吃满剩余高度） -->
        <div class="debug-panes">
          <section class="debug-step-main">
          <!-- 与上次相比（ux-debug-loop）：重跑不清场，差异摆在一眼能看见的地方 -->
          <p v-if="diffSummary" class="muted" style="margin: 6px 0 0">
            {{ diffSummary }}
          </p>

          <!-- 这一页在哪一层：**材料该看哪一份、规则为什么取不到，答案都在这行事实里**。
               只陈述事实（层码 + 证据 + 判不出的原因），不给下一步动作——下一步的判据
               只有 `core/agent_plan` 一处，接上 `/agent-plan` 之前不新增第二条 -->
          <div v-if="currentLayer" class="debug-layer">
            <el-tag v-if="currentLayer.code" size="small" effect="plain">
              {{ currentLayer.code }}
            </el-tag>
            <span v-if="currentLayer.label" class="debug-layer-label">
              {{ currentLayer.label }}
            </span>
            <span v-if="currentLayer.lines[0]" class="debug-layer-evidence muted">
              {{ currentLayer.lines[0] }}
            </span>
            <details v-if="currentLayer.more" class="debug-layer-more">
              <summary class="muted">还有 {{ currentLayer.more }} 条</summary>
              <div
                v-for="(line, i) in currentLayer.lines.slice(1)"
                :key="i"
                class="muted debug-layer-evidence"
              >
                {{ line }}
              </div>
            </details>
            <span v-if="currentLayer.unsure" class="debug-layer-evidence muted">
              {{ currentLayer.unsure }}
            </span>
          </div>

          <!-- 这一行的材料是哪来的：**「验证」验的就是它**。引擎那份（0 请求）与我们另抓的
               一份可能不是同一页（L2–L4 尤其）；「渲染过没有」是另一件事实（origin=engine
               也可能是静态页）；登录态是本机与真机的第一差异。三样一起摆出来 -->
          <div v-if="currentOrigin || currentRender || loginState" class="debug-origin muted">
            <span v-if="currentOrigin" :title="currentOrigin.hint">材料：{{ currentOrigin.label }}</span>
            <template v-if="currentRender">
              <span v-if="currentOrigin">·</span>
              <span :title="currentRender.hint">{{ currentRender.text }}</span>
            </template>
            <template v-if="loginState">
              <span v-if="currentOrigin || currentRender">·</span>
              <span :title="loginState.hint">{{ loginState.text }}</span>
            </template>
          </div>

          <!-- 源级静态错配（后端的运行事实，不是下一步动作）：最典型的是「配了 webJs
               但 URL 规则没开 webView → 这段 JS 不会生效」。文案直接用后端那句，不在这里重写 -->
          <p v-for="note in staticNotes" :key="note" class="debug-static-note muted">
            {{ note }}
          </p>

          <!-- 下一步做什么：**判据只有一处**（后端 core/agent_plan 按事实给一条缺口），
               这里只渲染那一句。同一现象不在这里再说第二遍——其余缺口在 title 里备查 -->
          <p v-if="planLine" class="debug-plan">{{ planLine.text }}</p>
          <p v-else-if="planError" class="debug-plan muted">{{ planError }}</p>

          <!-- 本步规则：唯一编辑入口；改一个字符即入草稿并标过期，
            验收仍走「重调本步验证」的真引擎，这里不提供第二种判定。
            字段一律平铺——详情 8 个、正文 3 个、目录 1 个走同一套行 -->
          <div
            v-if="stepFields.length"
            class="inline-rule"
            :data-rule-field="ruleGroupAttr"
          >
            <div class="inline-rule-label">
              <span>{{
                stepFields.length > 1
                  ? (STEP_LABELS[currentSummary.name] || currentSummary.name) +
                    " · 全组字段"
                  : stepFields[0].label
              }}</span>
              <span class="inline-rule-actions">
                <!-- 组级补全：一次补这一步的全部字段（验过的提议逐字段落进输入框）。
                     这一步没有 AI 材料（后端 FILL_SPECS 里没它）就**不渲染**——
                     摆一个点了没用的按钮比没有更糟 -->
                <el-tooltip
                  v-if="autofillData"
                  :disabled="canAiFill"
                  :content="canAiFillNote"
                  placement="top"
                >
                  <el-button
                    size="small"
                    plain
                    :loading="aiRunning"
                    :disabled="!canAiFill"
                    @click="startAiFill"
                  >全部补全</el-button>
                </el-tooltip>
                <el-button
                  v-if="aiRunning"
                  size="small"
                  @click="agent.cancel()"
                >取消补全</el-button>
                <!-- 同一个动作（本步重跑），措辞按材料说真话：材料是另抓的静态页 + 层 L2 时
                     叫「取渲染材料」——那才是用户此刻该做的那一步 -->
                <el-tooltip
                  :disabled="currentRun.kind !== 'render'"
                  :content="currentRun.hint"
                  placement="top"
                >
                  <el-button
                    size="small"
                    link
                    type="primary"
                    :disabled="rerunning || !canRerun"
                    @click="rerunCurrentStep"
                    >{{ runActionLabel }}</el-button
                  >
                </el-tooltip>
              </span>
            </div>
            <!-- 失败原因：走到用户眼前（AGENTS #4）。逐字段过程与调用明细在弹窗里
                 （点「全部补全」就打开了）；这里只留这一轮起不来的原因——那时没有
                 任务号，弹窗里没有内容可显示 -->
            <div v-if="agent.error.value" class="autofill-error muted">
              {{ agent.error.value }}
            </div>
            <!-- 每行一字段：状态与验证结论都在行内，不再有预览表 + 勾选 -->
            <div
              v-for="d in stepFields"
              :key="d.field"
              class="flat-rule-row"
              :class="{ 'is-suggested': !!flatRuleOf(d.field) && suggestedRules[d.field] === flatRuleOf(d.field) }"
              :data-rule-field="d.field"
            >
              <div class="flat-rule-head">
                <span class="flat-rule-label">{{ d.label }}</span>
                <el-tag
                  v-if="flatRuleOf(d.field) && suggestedRules[d.field] === flatRuleOf(d.field)"
                  size="small"
                  type="info"
                  class="suggest-tag"
                  >建议</el-tag
                >
                <!-- 字段值 = 唯一一个状态（引擎/快验谁跑完谁写入，编辑即清）。
                     一个值显示值本身，多个值显示条数标签，点开看列表。
                     `fact: "none"` 的行没有实测值（它不是取值规则）——那一格整个不渲染 -->
                <span
                  v-if="d.fact !== 'none'"
                  class="flat-rule-facts"
                >
                  <span
                    v-if="!fieldValues[d.field]"
                    class="flat-rule-fact state-empty"
                    >{{ flatRuleOf(d.field) ? "还没有实测值" : "还没有填规则" }}</span
                  >
                  <span
                    v-else-if="fieldValues[d.field].loading"
                    class="flat-rule-fact"
                    >验证中…</span
                  >
                  <el-tag
                    v-else-if="fieldValues[d.field].error"
                    size="small"
                    effect="plain"
                    type="danger"
                    class="quick-tag"
                    :title="fieldValues[d.field].error"
                    >取不到</el-tag
                  >
                  <template v-else-if="fieldValues[d.field].count > 1">
                    <el-tag
                      size="small"
                      effect="plain"
                      type="success"
                      class="quick-tag"
                      :class="{ 'is-open': !!quickOpen[d.field] }"
                      @click="toggleQuickValues(d.field)"
                      >{{ fieldValues[d.field].count }} 条</el-tag
                    >
                    <span
                      class="flat-rule-fact"
                      :title="(fieldValues[d.field].values || []).join(' · ')"
                      >{{ (fieldValues[d.field].values || [])[0] || "" }}</span
                    >
                  </template>
                  <template v-else-if="fieldValues[d.field].count === 1">
                    <span
                      class="flat-rule-fact"
                      :title="fieldValues[d.field].values[0]"
                      >{{ fieldValues[d.field].values[0] }}</span
                    >
                    <a
                      v-if="openableUrl(fieldValues[d.field].values[0])"
                      :href="openableUrl(fieldValues[d.field].values[0])"
                      target="_blank"
                      rel="noopener noreferrer"
                      class="val-open"
                      >打开</a
                    >
                  </template>
                  <el-tag
                    v-else
                    size="small"
                    effect="plain"
                    type="info"
                    class="quick-tag"
                    >0 条</el-tag
                  >
                </span>
                <span class="flat-rule-actions">
                  <!-- 没快验口径、或者这一行的规则为空时**不渲染按钮**：空规则点下去什么都验不了，
                       把「该不该给按钮」留给按钮自己的存在与否，不再配一句解释（提示只在不出现会卡住时才加） -->
                  <el-button
                    v-if="verifyAllowed(d.field)"
                    size="small"
                    link
                    type="primary"
                    :loading="fieldValues[d.field] && fieldValues[d.field].loading"
                    :disabled="!runId || !flatRuleOf(d.field)"
                    :title="runId ? '' : '快验要用本机引擎的任务材料；连 App 调试只有整步结论'"
                    @click="verifyField(d.field)"
                  >验证</el-button>
                  <el-tooltip
                    v-if="autofillData"
                    :disabled="canAiFill"
                    :content="canAiFillNote"
                    placement="top"
                  >
                    <el-button
                      size="small"
                      link
                      :disabled="!canAiFill"
                      title="AI 只补这一个字段（当前草稿会被覆盖）"
                      @click="fieldAi(d.field)"
                    >AI 补全</el-button>
                  </el-tooltip>
                </span>
              </div>
              <el-input
                :model-value="flatRuleOf(d.field)"
                type="textarea"
                :autosize="{ minRows: 1, maxRows: 4 }"
                :placeholder="d.placeholder"
                @focus="onFieldFocus(d)"
                @keydown.ctrl.enter="verifyAllowed(d.field) && verifyField(d.field)"
                @update:model-value="(v) => onUserRuleInput(d.field, v)"
              />
              <!-- 取到的值（点标签展开）：URL 给打开，长值截断、悬停看全文 -->
              <div
                v-if="quickOpen[d.field] && (fieldValues[d.field] || {}).values && fieldValues[d.field].values.length"
                class="quick-values"
              >
                <div
                  v-if="fieldValues[d.field].count > fieldValues[d.field].values.length"
                  class="muted quick-values-note"
                >
                  共 {{ fieldValues[d.field].count }} 条，显示前
                  {{ fieldValues[d.field].values.length }} 条
                </div>
                <div
                  v-for="(v, i) in fieldValues[d.field].values"
                  :key="i"
                  class="quick-value"
                >
                  <span :title="v">{{ v.length > 80 ? v.slice(0, 80) + "…" : v }}</span>
                  <a
                    v-if="openableUrl(v)"
                    :href="openableUrl(v)"
                    target="_blank"
                    rel="noopener noreferrer"
                    class="val-open"
                    >打开</a
                  >
                </div>
              </div>
            </div>
            <!-- 语法速查：默认收起，写规则时展开对照（GrammarList 唯一一份） -->
            <details class="grammar-details">
              <summary class="muted">规则语法速查</summary>
              <GrammarList />
            </details>
          </div>
          <el-empty
            v-if="!stepFields.length"
            description="这一步没有单字段规则（发现页走完整链路）"
            :image-size="60"
          />

          <!-- 本步实测摘要来自事件中明确记录的统计/字段。verdict 与过期标记已在
               步骤条芯片上（常驻可见），这里不再重复一份整步结论 -->
          <section v-if="isEngineResult && current" class="matched-result">
            <div class="debug-evidence">
              <b
                >{{
                  STEP_LABELS[currentSummary.name] || currentSummary.name
                }}结果</b
              >
              <span v-if="currentSummary.total !== null">
                {{ currentSummary.totalLabel }} {{ currentSummary.total }}
              </span>
            </div>
            <!-- 正文的产出是**一次提取的结果**，不是字段值：先说几个字、几页、首段长什么样。
                 字符数以事件流那次为准（上游 contentDebug 走 needSave=false，正文全文就在
                 事件里）；结构化样本是引擎另算的一份，短于全文时如实标出来（截断不静默） -->
            <div v-if="currentContent" class="content-result">
              <div class="content-result-facts">
                <template v-if="currentContent.hasText">
                  <span>{{ currentContent.chars }} 字符</span>
                  <span v-if="currentContent.pages !== null" class="muted">
                    · {{ currentContent.pages }} 页
                  </span>
                  <span
                    v-if="currentContent.sampleChars && currentContent.sampleChars < currentContent.chars"
                    class="muted"
                  >
                    · 引擎样本只到 {{ currentContent.sampleChars }} 字符
                  </span>
                </template>
                <span v-else class="muted">这次没取到正文文本</span>
                <el-button
                  size="small"
                  link
                  type="primary"
                  :disabled="!currentContent.hasText"
                  @click="pickMaterial('content')"
                  >看全文</el-button
                >
              </div>
              <p
                v-if="currentContent.firstLine"
                class="content-result-first muted"
              >
                首段：{{ currentContent.firstLine }}
              </p>
            </div>
            <!-- 这一步的失败原因直接亮出来（原先在折叠块里，等于看不见）；
                 verdict 已在步骤条芯片上 -->
            <p v-if="currentSummary.reason" class="debug-result-reason">
              {{ currentSummary.reason }}
            </p>
            <!-- 完整列表：引擎解析器算出的完整条目（structured 材料），只对列表步
                 （搜索/目录）出现——详情步的值已在字段行的引擎事实里 -->
            <details v-if="currentList" class="debug-value-details">
              <summary class="muted">
                完整列表（{{ currentList.rows.length }} 条<template
                  v-if="currentList.total !== null"
                >
                  · 共 {{ currentList.total }} 条</template
                >{{ currentList.truncated ? " · 仅保留前一部分" : "" }}）
              </summary>
              <table class="debug-list-table">
                <thead>
                  <tr>
                    <th
                      v-for="col in currentList.columns"
                      :key="col.key"
                    >
                      {{ col.label }}
                    </th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="(row, i) in currentList.rows" :key="i">
                    <td v-for="col in currentList.columns" :key="col.key">
                      <a
                        v-if="col.url && openableUrl(row[col.key])"
                        :href="openableUrl(row[col.key])"
                        target="_blank"
                        rel="noopener noreferrer"
                        class="val-open"
                        >打开</a
                      >
                      <span :title="row[col.key]">{{ row[col.key] }}</span>
                    </td>
                  </tr>
                </tbody>
              </table>
            </details>
            <!-- 什么都没记录时才说这句话：正文步的产出在上面的「正文结果」里，
                 这里再说一遍「未记录样本」会与它自相矛盾；运行时对象是材料
                 （材料栏那一页），有它时同样不说这句 -->
            <p
              v-if="currentList === null && currentSummary.total === null && !currentContent && !currentRuntime"
              class="muted"
            >
              本步未记录可提取的样本；完整过程在任务中心的时间线
            </p>
          </section>
        </section>
        <!-- 分栏分隔条：宽度可拖、双击复位。窄容器下整栏回落成堆叠（见 @container） -->
        <div
          class="debug-split-handle"
          role="separator"
          aria-label="调整材料栏宽度"
          title="拖动调整材料栏宽度；双击复位"
          @pointerdown="startMaterialDrag"
          @dblclick="resetMaterialWidth"
        ></div>
        <!-- 材料常驻右栏（宽屏）：改规则与看页面同屏，不用来回滚。
             大纲 / 命中片段 / 页面原文共用一套只读查看器（显示格式化、复制原文、搜索） -->
        <aside ref="materialEl" class="debug-material-panel" :style="materialStyle">
          <div class="debug-material-title">{{ materialTitle }}</div>
          <div class="toolbar" style="margin-bottom: 8px">
            <el-radio-group
              :model-value="materialKind"
              size="small"
              @update:model-value="pickMaterial"
            >
              <!-- 标签 = 这一步**真有**的材料（判据在 utils/debugMaterial）：
                   L3 上是「运行时形状」、L4 上是「接口条目」，没有的那份不摆空标签 -->
              <el-radio-button
                v-for="tab in currentTabs"
                :key="tab.value"
                :value="tab.value"
                >{{ tab.label }}</el-radio-button
              >
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
            <el-button
              size="small"
              :disabled="!materialRaw"
              @click="copyMaterial"
            >
              复制原文
            </el-button>
          </div>
          <!-- 高亮的定位口径：位置按**显示文本**找，处数按**原文**数。两者不等时
               如实说——不假装对齐（AGENTS #12） -->
          <p v-if="hitsMisaligned" class="muted" style="margin: 0 0 8px; font-size: 12px">
            已渲染的这段里有 {{ displayHitOffsets.length }} 处标出来了，按原文数应为
            {{ hitsInWindow }} 处（格式化会改字符位置）：点「加载更多」或换关键词再看。
          </p>
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
                {{ materialRaw.length }} 字符）
              </el-button>
            </div>
          </template>
          <el-empty
            v-else
            :description="materialEmptyHint"
            :image-size="60"
          />
        </aside>
        </div>
      </div>
    </template>

    <!-- 一轮 AI 补全自己的明细（不复用任务明细弹窗）；「再补 N 个」回到这里同一套落框 -->
    <AgentRunDialog ref="agentDialog" :rerun="rerunAgentFields" />
  </div>
</template>

<style scoped>
/* 工作台专属令牌挂在组件根节点：调试页换配色不外溢到列表、回收站和任务抽屉 */
/* 页内定高（IDE 式）：根节点是 flex 列——运行条/提示在上（不滚），workspace 吃满
   剩余高度。主列与材料栏各自整列滚动，外层不再有第二个滚动源 */
.debug-workbench {
  --debug-panel-bg: var(--app-surface);
  --debug-panel-border: var(--app-border-light);
  --debug-surface-muted: var(--el-fill-color-lighter, #fafafa);
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
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
.inline-rule-actions {
  margin-left: auto;
  display: inline-flex;
  align-items: center;
  gap: 8px;
}
/* AI 补全的报因/进度一行 */
.autofill-error {
  margin-bottom: 8px;
  font-size: 12px;
}
/* 行内验证结论：取到（绿）/ 取不到（红），一眼分出 */
.try-ok { color: var(--el-color-success); }
.try-bad { color: var(--el-color-danger); }
/* 平铺编辑（详情步）：每字段一行，行头是标签 + 建议底纹 + 验证状态 + 行内动作 */
.flat-rule-row {
  margin-bottom: 8px;
}
.flat-rule-row:last-child {
  margin-bottom: 0;
}
.flat-rule-head {
  display: flex;
  align-items: baseline;
  gap: 8px;
  margin-bottom: 3px;
  font-size: 12px;
  min-width: 0;
}
.flat-rule-label {
  flex: 0 0 auto;
  color: var(--app-text-secondary);
}
/* 行头一个事实：字段值（引擎/快验谁跑完谁写入）。只展示这一份——两套形状摆一起
   就看不出「引擎和快验取到的是不是同一个值」 */
.flat-rule-facts {
  flex: 1 1 auto;
  display: flex;
  align-items: baseline;
  gap: 8px;
  min-width: 0;
  font-size: 12px;
}
.flat-rule-fact {
  overflow: hidden;
  /* 折两行：路径/URL 类值一行放不下，截成省略号等于没给（title 里是全文） */
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  white-space: normal;
  word-break: break-all;
}
.state-empty { color: var(--app-text-muted, #909399); }
/* 快验小标签：有值才可点开（cursor 是可点的暗示）；展开的值列表贴在输入框下 */
.quick-tag.is-open { cursor: pointer; }
.quick-tag:not(.is-open) { cursor: default; }
.quick-values {
  margin: 4px 0 0;
  padding: 4px 8px;
  background: var(--app-bg);
  border: 1px solid var(--app-border-light);
  border-radius: var(--app-radius-sm);
  font-size: 12px;
}
.quick-values-note { margin: 0 0 2px; }
.quick-value {
  display: flex;
  align-items: baseline;
  gap: 8px;
  min-width: 0;
}
.quick-value span {
  flex: 1 1 auto;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.flat-rule-actions {
  flex: 0 0 auto;
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
/* 语法速查折叠块：写规则时展开对照，默认收起不占版面 */
.grammar-details {
  margin-top: 8px;
}
.grammar-details summary {
  cursor: pointer;
  font-size: 12px;
}
/* 建议底纹：值来自建议且未被用户改过；一改即消失（判据是纯派生的相等比较）。
   el-input 的内层元素是子组件的 DOM，scoped 够不到，必须 :deep() */
.flat-rule-row.is-suggested :deep(.el-textarea__inner) {
  background: var(--el-color-primary-light-9, #ecf5ff);
}
.suggest-tag {
  transform: scale(0.85);
  flex: 0 0 auto;
}
/* 运行态条：定高页面里它固定在顶部（不滚），不再需要 sticky 与「让位」逻辑——
   页面本身不滚，它天然一直可见 */
.debug-runchip {
  flex: 0 0 auto;
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
  margin-top: 10px;
}
/* live tail：最近一条引擎事件，muted 小字跟着主行走 */
.debug-runchip-tail {
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12px;
  color: var(--app-text-secondary);
}
.val-open {
  margin-left: 6px;
  font-size: 12px;
}
/* 工作区：两层结构（步骤条一行 / 面板行吃满剩余高度）+ 容器查询的**查询容器**
   ——按工作台实拿到的宽度分栏，而不是按窗口宽度；窄容器回落成单页滚动 */
.debug-workspace {
  container-type: inline-size;
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
/* 面板行：主列与材料栏各自整列滚动。**这层的 min-height: 0 不能漏**——它没有
   确定高度时，两个面板尺寸到内容就止，超出部分被根节点的 overflow:hidden 裁掉，
   表现是「哪里都滚不动、材料掉到主列下面」（漏写这层 CSS 真踩过一次） */
.debug-panes {
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
  gap: 14px;
  align-items: stretch;
}
/* 步骤条：4 个条目横排，固定一行（定高页面里它常驻可见）。
   **不做卡片**：页卡 → 步骤条卡 → 左右面板卡 → 字段组卡是四层边框，
   去掉这层的底色与描边，它就是一条工具行 */
.debug-step-strip {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
  padding: 0 2px;
}
.debug-step-strip-title {
  flex: 0 0 auto;
  color: var(--app-text);
  font-size: 13px;
  font-weight: 600;
}
.debug-step-tabs {
  display: flex;
  flex-direction: row;
  gap: 6px;
  min-width: 0;
  overflow-x: auto;
}
.debug-step-tab {
  display: flex;
  align-items: center;
  gap: 7px;
  flex: 0 0 auto;
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
  flex: 0 0 auto;
}
/* 主列：与材料栏对称的面板（它自己滚）——内容短的时候，下面的空白读起来是
   「编辑面」而不是页面破了个洞 */
.debug-step-main {
  min-width: 0;
  flex: 1 1 420px;
  order: 1;
  min-height: 0;
  overflow-y: auto;
  padding: var(--app-space-3);
  background: var(--debug-panel-bg);
  border: 1px solid var(--debug-panel-border);
  border-radius: var(--app-radius-lg);
}
/* 分栏分隔条：拖动改材料栏宽度，双击复位 */
.debug-split-handle {
  order: 2;
  flex: 0 0 6px;
  align-self: stretch;
  min-height: 120px;
  cursor: col-resize;
  border-radius: 3px;
  background: linear-gradient(
    to right,
    transparent 2px,
    var(--debug-panel-border) 2px,
    var(--debug-panel-border) 4px,
    transparent 4px
  );
}
.debug-split-handle:hover {
  background: var(--el-color-primary-light-7, #a0cfff);
}
/* 材料栏：定高页面里它是一个**整列自滚的窗**——不再是 sticky + 52vh 的盒子，
   也就不需要与运行条抢 top 的让位逻辑。默认 40%，拖过以存下的 px 为准 */
.debug-material-panel {
  flex: 0 0 40%;
  order: 3;
  min-width: 0;
  overflow-y: auto;
  padding: var(--app-space-3);
  background: var(--debug-panel-bg);
  border: 1px solid var(--debug-panel-border);
  border-radius: var(--app-radius-lg);
}
.debug-material-title {
  margin: 0 2px var(--app-space-2);
  color: var(--app-text);
  font-size: 13px;
  font-weight: 600;
}
.debug-material-panel .toolbar {
  flex-wrap: wrap;
}
/* 事件流折叠块：等待期有流可看，跑完也能展开排查 */
.debug-events-details,
.debug-value-details {
  margin: var(--app-space-2) 0 0;
}
.debug-events-details > summary,
.debug-value-details > summary {
  cursor: pointer;
  font-size: 12px;
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
/* 层与证据：一行事实（这一页在哪一层、凭什么这么判）——不是动作区，不给下一步 */
/* 材料出身一行：比层证据更安静（它是定语，不是结论） */
.debug-origin {
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 6px;
  margin: 0 0 var(--app-space-2);
  font-size: 12px;
}
/* 下一步做什么：一次只说一条，所以给它一行自己的位置（不塞进上面的材料行内） */
.debug-plan {
  margin: 0 0 var(--app-space-2);
  font-size: 12px;
}
/* 源级静态错配：事实区，一行一条；与「下一步做什么」分开（一个是事实，一个是动作） */
.debug-static-note {
  margin: 0 0 var(--app-space-2);
  font-size: 12px;
}
.debug-layer {
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 8px;
  margin: 6px 0 var(--app-space-2);
  font-size: 12px;
  min-width: 0;
}
.debug-layer-label {
  color: var(--app-text-secondary);
}
.debug-layer-evidence {
  min-width: 0;
  word-break: break-all;
}
.debug-layer-more summary {
  cursor: pointer;
}
/* 正文结果：几个字 / 几页 / 首段——正文那一步的主产出（字段只是输入面） */
.content-result {
  margin: 0 0 var(--app-space-2);
  padding: var(--app-space-2) 10px;
  background: var(--debug-surface-muted);
  border: 1px solid var(--app-border-light);
  border-radius: var(--app-radius-sm);
}
.content-result-facts {
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 6px;
  font-size: 13px;
}
.content-result-first {
  margin: 6px 0 0;
  font-size: 12px;
  word-break: break-all;
}
.debug-result-reason {
  margin: 0 0 var(--app-space-2);
}
/* structured 完整列表：折叠块内一张窄表，横向滚动防长 URL 撑破布局 */
.debug-structured-list {
  margin: 0 0 var(--app-space-2);
}
.debug-structured-list summary {
  cursor: pointer;
  font-size: 12px;
}
.debug-list-table {
  width: 100%;
  margin-top: 6px;
  border-collapse: collapse;
  font-size: 12px;
  table-layout: fixed;
}
.debug-list-table th,
.debug-list-table td {
  padding: 3px 6px;
  border: 1px solid var(--debug-panel-border);
  text-align: left;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.debug-list-table th {
  color: var(--app-text-muted);
  font-weight: 600;
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
  margin: 0;
}
.debug-pre-wrap {
  white-space: pre-wrap;
  word-break: break-all;
}
.debug-hit-active {
  outline: 2px solid var(--app-status-fail);
}

/* 窄容器回落成**单页滚动**：工作区自己滚（一次性替代外层），主列/材料不再各自滚，
   材料整栏掉到主列下方、拖拽条没有意义。步骤条钉在工作区顶部（它是滚动容器的
   后代，sticky 生效）——查询的是工作台实拿到的宽度（container-type 挂在
   .debug-workspace 上），窗口缩放、抽屉宽度、别的挂载点都走同一份判据 */
@container (max-width: 900px) {
  .debug-workspace {
    display: block;
    overflow-y: auto;
  }
  .debug-panes {
    display: block;
    overflow: auto;
  }
  .debug-step-main {
    overflow-y: visible;
  }
  .debug-step-strip {
    position: sticky;
    top: 0;
    z-index: 5;
  }
  .debug-material-panel {
    flex: 1 1 100%;
    order: 4;
    overflow-y: visible;
    margin-top: 10px;
  }
  .debug-split-handle {
    display: none;
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
