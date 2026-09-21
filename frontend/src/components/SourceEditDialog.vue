<script setup>
// 新建 / 编辑书源：对话框形态。跳独立页面会丢掉列表的筛选和分页状态。
import { ref, computed, watch, nextTick } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { api, subscribeJob } from "../api/client";
import { getDetail, listTags, saveSource, sourceExists } from "../api/sources";
import { appDebug, appPreflight, jvmDebug } from "../api/rules";
import {
  canonicalTag, ensureTagMeta, isQualityTag, isStatusTag,
  mergeGroup, sourceTypes, splitSystemUser, statusTags, tagOfType, typeKeyOf,
} from "../utils/tags";
import { useMobile } from "../composables/useMobile";
// 步骤名 → 中文的**唯一**一份（调试抽屉共用），别再在本组件里写第二份
import { STEP_LABELS } from "../utils/steps";
import RuleDebugDrawer from "./RuleDebugDrawer.vue";

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  sourceUrl: { type: String, default: "" },   // 空 = 新建
});
const emit = defineEmits(["update:modelValue", "saved"]);
const isMobile = useMobile();

const visible = computed({
  get: () => props.modelValue,
  set: (v) => emit("update:modelValue", v),
});
// 另存为新源：本质是「以当前编辑中的源为模板，创建一个不同域名的新源」。
// 编辑模式下域名输入框被禁用（Legado 按域名做主键，改域名等于换源），
// 原先的提示只写了「请另存为新源」却没有任何入口——这个 ref 就是那个入口。
// 一旦置 true：域名输入框解禁、标题变「另存为新源」、跳过「域名已存在」探测。
const isDuplicate = ref(false);
// isNew 有两个真值来源：真正的新建（无 sourceUrl），以及从编辑态切过来的「另存」。
const isNew = computed(() => isDuplicate.value || !props.sourceUrl);
// App 的 IP 存 localStorage：它基本不变，每次打开弹窗重填一遍没有意义。
// 键名带 legado 前缀，避免同域下与别的应用串味。
const APP_HOST_STORAGE_KEY = "legado.appHost";
// 读回上次填过的 IP。隐私模式等 localStorage 不可用的场景静默降级成空值
function readAppHost() {
  try { return localStorage.getItem(APP_HOST_STORAGE_KEY) || ""; } catch (e) { return ""; }
}

const loading = ref(false);
const appHost = ref(readAppHost());  // App 的 IP（连 App 调试用）
const appDebugging = ref(false);
//: 调试通道：**默认本机引擎**（App 的源码跑在本机，不填 IP、不推送）。连 App 那条
//: 留着——登录态、网络出口、WebView 都在手机上，那是它不可替代的地方
const debugChannel = ref("jvm");

//: 页面缓存策略（每次调试选，不落 localStorage）。
//
// 不记住是有意的：「只读不补抓」是**针对这一次**的怀疑（比如「我想确认抽屉里
// 那份 HTML 就是刚才那份」），记住它会让下一次调试莫名其妙没有页面。
// 三个值对应 core.fetch 的 CACHE_*，后端按同一份枚举校验。**卡片上不解释
// 这三档的差别**：抽屉里每一页都标着「页面抓取于 X / 来自缓存」，那才是它该在
// 的地方——同一件事只写一处（AGENTS #10）。
const DEBUG_CACHE_MODES = [
  { value: "auto", label: "用缓存" },
  { value: "only", label: "只读缓存" },
  { value: "refresh", label: "忽略缓存" },
];
const appCacheMode = ref("auto");
// 预检结果：null=还没测过 / {state: ready|missing|unreachable, error}
const appPreflightState = ref(null);
const appChecking = ref(false);
// 调试目标 → App 的 key 形态。分派依据是 Legado 的 `Debug.kt:236-279`
// （那个 when 才是真正的规则），交互照 App 调试界面的 chip 行
// （`BookSourceDebugScreen.kt:174-182`：一排 ToggleChip 选目标 + 一个输入框）。
//
// **选中的目标只是「提示 + 前缀构造器」，不是硬约束**：App 是按 key 的形态分派的
// （isAbsUrl → contains("::") → ++ → -- → 兜底搜索）。所以「搜索」下填一个 URL
// 会被 App 当详情页跑，「详情」下填关键词会被当搜索跑。这不是我们拼错了——App
// 自己就是同一个 when，保持一致才是对的。
//: hint 只回答「这格填什么」。详情 / 目录 / 正文留空会退回搜索入口（理由见
//: debugKey），「可留空」写进各自 placeholder 就够；留空之后跑什么不复述——
//: 三格是同一个答案。例外是「发现」：留空落回配置里的 `exploreUrl` 而不是
//: 搜索，说不到一起，所以由它自己写
const DEBUG_TARGETS = [
  { value: "search", label: "搜索", hint: "关键词，如 我的" },
  { value: "explore", label: "发现", hint: "留空则用配置里的 exploreUrl" },
  { value: "info", label: "详情", hint: "详情页 URL，可留空" },
  { value: "toc", label: "目录", hint: "目录页 URL，可留空" },
  { value: "content", label: "正文", hint: "正文页 URL，可留空" },
];
const debugTarget = ref("search");
const debugQuery = ref("");
const currentTarget = computed(
  () => DEBUG_TARGETS.find((t) => t.value === debugTarget.value) || DEBUG_TARGETS[0],
);

//: 输入框的提示。**必须说实话**：选了「发现」而这个源又没配 `exploreUrl` 时，
//: 「留空则用配置里的 exploreUrl」就是一句假话——用户留空、点下去、失败，
//: 再回头看提示才发现被误导了。提示的价值就在于**在下手之前**说清这一格要什么
const debugHint = computed(() => {
  if (debugTarget.value === "explore" && !hasExploreConfig.value) {
    return "这个源没配 exploreUrl，请填一个发现页 URL";
  }
  return currentTarget.value.hint;
});

/** 目标 + 输入 → App 认的 key。前缀构造照抄 App 的 `ViewModel:91-97`。 */
function buildDebugKey() {
  const q = debugQuery.value.trim();
  switch (debugTarget.value) {
    // **发现页 URL 直接从配置取**——`exploreUrl` 就是它，库里 2659/3861 条源都有。
    // 让用户再抄一遍是重复劳动，而且抄错了就是一次静默失败（App 对认不出
    // 的目标是**无响应**，最难排查）。留空时才回落到输入框
    case "explore": {
      const url = q || String(form.value.exploreUrl || "").trim();
      return url ? `发现::${url}` : "";
    }
    // 幂等去前缀：用户手抄 URL 时常把 ++ / -- 一起带上，不去重就会拼成 ++++。
    // 只去**一次**（等价 Kotlin 的 removePrefix），写成 /^\++/ 会把真想要的
    // `+++url` 也一起吃掉、改成别的意思。
    case "toc": return q ? `++${q.replace(/^\+\+/, "")}` : "";
    case "content": return q ? `--${q.replace(/^--/, "")}` : "";
    case "info": return q;              // 详情页就是裸 URL，App 靠 isAbsUrl 认它
    // 搜索：空则用默认关键词。**详情/目录/正文留空时也落到这里**——见下
    default: return q || "我";
  }
}

/**
 * 交给 App 的调试 key。
 *
 * 与 `buildDebugKey` 的区别只有一处：**「详情/目录/正文」留空时不报错，而是退回
 * 搜索入口**。
 *
 * 依据是 App 自己的行为（`Debug.kt:279-297` 的 exploreDebug / searchDebug）：
 * 拿到入口后它会**自动沿规则链往下跑**（取第一本书 → 详情 → 目录 → 正文），
 * 用户从来不需要手填下游 URL。我们原来要求手填三者之一，等于把引擎该算出来的
 * 东西交给用户——而 App 里根本没有这一步。
 *
 * 退回搜索之后，调试抽屉照样会把每一步摊开，所以「想看某一步」的信息一点不丢。
 * 真的想**从中间切入**（手里已经有一本书的 URL）时，填上它即可，两条路都在。
 */
function debugKey() {
  const q = debugQuery.value.trim();
  if (!q && ["info", "toc", "content"].includes(debugTarget.value)) {
    return "我";        // 搜索入口，App 会自己往下串
  }
  return buildDebugKey();
}
//: 这个源**有没有发现配置**。判断口径与 `tabDot("discover")` 一致。
const hasExploreConfig = computed(
  () => !!(String(form.value.exploreUrl || "").trim()
           || Object.keys(form.value.ruleExplore || {}).length));

//: 「配了发现，但不想让它在 App 里出现」。
//
// `enabledExplore` 在 Legado 里是用户开关（`BookSourceDao.kt:87` 决定源是否进
// 发现模块），但它**必须由配置推导**——实测库里 739 条「开着却完全没配置」
// （App 里就是点了没反应的死项）、133 条「有配置却关着」（功能被静默关掉）。
// 手工维护的状态一定会漂，而它本来就算得出来。
//
// 所以这里只留**唯一一个显式用法**：配了但想藏起来。默认跟随配置。
const hideExplore = computed({
  get: () => !form.value.enabledExplore,
  set: (v) => { form.value.enabledExplore = !v; },
});

const testResult = ref(null);
const testStale = ref(false);      // 规则已改动，结果过期
const debugVisible = ref(false);   // 调试抽屉
const debugStep = ref("");         // 抽屉打开时定位到哪一步
const systemTags = ref([]);
const manualStatus = ref("");
// 健康状态是否锁定。锁定是开关语义，不该靠「select 的空值」表达——
// 那样用户以为只是选了个状态，实际是把校验结果锁死了
const statusLocked = ref(false);
const userTags = ref([]);
const userTagOptions = ref([]);
const quickUrl = ref("");
const quickDetailUrl = ref("");
const quickDiscover = ref(false);
const quickProbe = ref(true);
const quickLoading = ref(false);
const quickProgress = ref("");
const quickVerify = ref(null);
let quickStop = null;

// App IP 输入后立刻回写 localStorage（存 trim 后的值）。清空则删掉键——
// 下次打开就是干净的空值，点「连 App 调试」会正常给出「请先填 App 的 IP」。
watch(appHost, (value) => {
  const host = String(value || "").trim();
  try {
    if (host) localStorage.setItem(APP_HOST_STORAGE_KEY, host);
    else localStorage.removeItem(APP_HOST_STORAGE_KEY);
  } catch (e) {
    // 写不进去不影响本次调试，只是下次要重填
  }
});

const activeTab = ref("quick");
const activeRuleTab = ref("search");
const extActive = ref(["request"]);
const rawJsonText = ref("");
const rawJsonError = ref("");
const savedSnapshot = ref("");     // 打开弹窗 / 保存成功时的表单快照，用于脏检查
let rawDirty = false;              // 用户是否手改过「原始 JSON」文本域

function blank() {
  return {
    bookSourceName: "", bookSourceUrl: "", bookSourceType: 0,
    bookSourceGroup: "", bookSourceComment: "",
    enabled: true, enabledExplore: true, charset: "",
    customOrder: 0, weight: 0,
    searchUrl: "", header: "",
    loginUrl: "", loginCheckJs: "", jsLib: "", concurrentRate: 1,
    customButton: false, eventListener: false, variableComment: "",
    exploreUrl: "", ruleExplore: {},
    ruleSearch: { bookList: "", name: "", bookUrl: "", coverUrl: "", author: "", intro: "" },
    ruleBookInfo: { name: "", coverUrl: "", author: "", intro: "", lastChapter: "", tocUrl: "" },
    ruleToc: { chapterList: "", chapterName: "", chapterUrl: "", nextTocUrl: "" },
    ruleContent: { content: "", nextContentUrl: "", imageStyle: "", webJs: "" },
  };
}
const form = ref(blank());

/** 源当前的健康状态标签（未锁定时就是校验结果给的）。 */
const currentStatus = computed(
  () => systemTags.value.find((t) => isStatusTag(t)) || "",
);

/** 质量标签（如「规则完整」）：由校验判定，用户改不了，只能看。 */
const qualityTags = computed(
  () => systemTags.value.filter((t) => isQualityTag(t)),
);

// 保存时要写回 group 的完整系统标签：类型 + 健康状态 + 质量。
// 界面上三者分散在各自的控件里，不再把这串整个渲染成 chips——那会让类型和
// 健康状态各出现两遍。
const displaySystemTags = computed(() => {
  if (!systemTags.value.length && isNew.value) return [];
  // 4 之类的脏值在 Legado 里没有对应类型名，留空并被 filter 丢掉
  const typeTag = tagOfType(form.value.bookSourceType);
  const status = manualStatus.value || currentStatus.value;
  return [typeTag, status, ...qualityTags.value].filter(Boolean);
});

// 锁定时默认锁在当前状态上，省得再选一次；关掉即交回校验结果
watch(statusLocked, (on) => {
  manualStatus.value = on ? currentStatus.value : "";
});

// 输入别名立即归一（如「精品排版」→「精排」），别等保存后被后端改名——
// 那时用户会以为标签被动了手脚。canonicalTag 幂等，第二遍不会再触发
watch(userTags, (list) => {
  const fixed = list.map((t) => canonicalTag(t));
  const renamed = list.filter((t, i) => fixed[i] !== t);
  const deduped = [...new Set(fixed.filter(Boolean))];
  if (!renamed.length && deduped.length === list.length) return;
  userTags.value = deduped;
  if (renamed.length) {
    ElMessage.info("标签已归一：" + renamed.map((t) => `${t} → ${canonicalTag(t)}`).join("，"));
  }
});

function filled(name) {
  const f = form.value;
  if (name === "basic") return true;
  if (name === "search") return !!(f.searchUrl || (f.ruleSearch && f.ruleSearch.bookList));
  if (name === "detail") return !!(f.ruleBookInfo && Object.values(f.ruleBookInfo).some(Boolean));
  if (name === "toc") return !!(f.ruleToc && f.ruleToc.chapterList);
  if (name === "content") return !!(f.ruleContent && (f.ruleContent.content || f.ruleContent.webJs));
  if (name === "request") return !!(f.header || f.loginUrl || f.loginCheckJs || f.jsLib);
  if (name === "discover") return !!(f.exploreUrl || (f.ruleExplore && Object.keys(f.ruleExplore).length));
  if (name === "interaction") return !!(f.customButton || f.eventListener || f.variableComment);
  if (name === "raw") return !!rawJsonText.value;
  return false;
}

function syncRawFromForm() {
  rawJsonText.value = JSON.stringify(form.value, null, 2);
  rawJsonError.value = "";
  // 文本域已被整份重写成表单内容，不再算「手改过」。
  // 不复位的话这份状态会跨「关闭→再打开」残留：下次打开时黄色提示条会无故出现，
  // 且 watch(form) 会一直拒绝刷新快照，P0-③ 的修复在第二次会话里直接失效
  rawDirty = false;
}

// 表单是否有未保存改动
function isDirty() {
  if (!savedSnapshot.value) return false;
  return JSON.stringify(form.value) !== savedSnapshot.value;
}

// 点遮罩/取消/ESC 都走这里。改了几十条规则误点空白处就全丢，
// 这个代价太大，必须拦一道
function handleBeforeClose(done) {
  if (!isDirty()) return done();
  ElMessageBox.confirm("有未保存的修改，确定要关闭吗？", "未保存", {
    confirmButtonText: "丢弃修改",
    cancelButtonText: "继续编辑",
    type: "warning",
  }).then(() => done()).catch(() => {});
}

function applyRawJson() {
  try {
    const obj = JSON.parse(rawJsonText.value || "{}");
    if (!obj || typeof obj !== "object" || Array.isArray(obj)) {
      throw new Error("必须是 JSON 对象");
    }
    const apply = () => {
      form.value = { ...blank(), ...obj };
      const parsed = splitSystemUser(form.value.bookSourceGroup || "");
      systemTags.value = parsed.system;
      manualStatus.value = "";
      statusLocked.value = false;      // 原始 JSON 不说锁定与否，按「自动」处理
      userTags.value = parsed.user;
      rawJsonError.value = "";
      rawDirty = false;               // 已应用到表单，文本域与表单一致了
      ElMessage.success("已应用到表单");
    };
    // 手改过就确认一次：这一步会整份替换表单，不可撤销
    if (rawDirty) {
      ElMessageBox.confirm("将用这段 JSON 整份替换当前表单，确定吗？", "应用到表单", {
        type: "warning", confirmButtonText: "替换", cancelButtonText: "取消",
      }).then(apply).catch(() => {});
      return;
    }
    apply();
  } catch (e) {
    rawJsonError.value = e.message;
    ElMessage.error("JSON 解析失败：" + e.message);
  }
}

// 单步 → 圆点级别。**顺序必须与 tagTypeOf / 抽屉的 dotClass 一致**
// （fail → unknown → has_notes），否则同一步会出现「页签黄、徽章灰」。
// unknown 要排在 has_notes 之前：`unknown` + 附注时的附注只是**解释为什么测不了**
// （如「仅发现模式，无搜索规则」），不是「有疑点」，显示成黄是误导。
function stepDot(s) {
  // 兜底：快速生成的结果存在 SQLite 里，可能是改动前产生的旧结果（无 verdict）。
  // 与 tagTypeOf 同款兜底——不兜的话 `undefined === "fail"` 全不成立，
  // 旧结果的失败步会渲染成绿色
  if (!s.verdict) return s.ok ? "ok" : "err";
  if (s.verdict === "fail") return "err";
  if (s.verdict === "unknown") return "unknown";
  return s.has_notes ? "warn" : "ok";
}
const DOT_RANK = { err: 3, warn: 2, unknown: 1, ok: 0 };
// 取最 alarming 的一档。**无状态（""）与 ok 都不参与**，全无状态时返回 ""
function worstDot(steps) {
  let worst = "";
  for (const s of steps || []) {
    const d = stepDot(s);
    if (worst === "" || DOT_RANK[d] > DOT_RANK[worst]) worst = d;
  }
  return worst;
}

function tabDot(name) {
  if (name === "quick") {
    const steps = (quickVerify.value && quickVerify.value.steps) || [];
    return steps.length ? worstDot(steps) : "";
  }
  if (name === "basic") {
    // 名称与域名是保存的前置条件（见 save()），两者齐全才算填好。
    // 原来这里恒定返回 "ok"，名称/域名为空时也是绿的——零信息量
    const filledBasic = String(form.value.bookSourceName || "").trim()
      && String(form.value.bookSourceUrl || "").trim();
    return filledBasic ? "ok" : "err";
  }
  if (name === "rules") {
    const steps = (testResult.value && testResult.value.steps) || [];
    const dot = worstDot(steps);
    if (dot) return dot;
    return ["search", "detail", "toc", "content"].every(filled) ? "ok" : "warn";
  }
  if (name === "ext") {
    return ["request", "discover", "interaction"].some(filled) ? "ok" : "";
  }
  if (name === "raw") return rawJsonText.value ? "ok" : "";
  return "";
}

// 三态徽章：fail 红 / unknown 灰 / pass 且有附注 黄 / 纯 pass 绿
function tagTypeOf(step) {
  // 兜底：快速生成的结果存在 SQLite 里，可能是改动前产生的旧结果（无 verdict）。
  // 不给兜底的话 `undefined === "fail"` 全不成立，旧结果的 fail 步会渲染成绿色
  if (!step.verdict) return step.ok ? "success" : "danger";
  if (step.verdict === "fail") return "danger";
  if (step.verdict === "unknown") return "info";
  return step.has_notes ? "warning" : "success";
}
// explore 是发现链路的产出步（key 带 `发现::` 时后端才产出它）

// 整体汇总。**三态，不是二态**——原来只有「全部通过 / 未全部通过」，
// 而 `unknown`（我们的工具回放不了）被算进「未通过」会误报：
// 仅发现模式下 search 步是预期内的 skip（unknown），每个真实执行的步都通过，
// 整体却说「未全部通过」，还跟着「补充详情页 URL 后重试」——
// 而用户正是**主动**选的仅发现模式。
//
// 试跑与快速生成两份结果共用这一个判定，避免同一件事写两处。
function testSummary(steps) {
  const list = steps || [];
  if (!list.length) return { level: "none", text: "" };
  const failed = list.some((s) => s.verdict === "fail"
    || (!s.verdict && s.ok === false));        // 旧结果（无 verdict）兜底
  if (failed) return { level: "fail", text: "未通过" };
  if (list.some((s) => s.verdict === "unknown")) {
    return { level: "unknown", text: "部分无法判定" };
  }
  return { level: "pass", text: "全部通过" };
}
// 只有真的 fail 才提示「去改规则」——unknown 是工具的能力边界，改规则没用
function summaryHint(summary) {
  return summary.level === "fail" ? "，可手动修改规则，或补充详情页 URL 后重试。" : "";
}

function expandTestFailures(res) {
  if (!res || !Array.isArray(res.steps)) return;
  // 失败和有附注的都要把人带到对应页签——附注是「通过了但有疑点」，
  // 不引导过去的话用户根本不会看到
  const interesting = res.steps.filter((s) => s.verdict === "fail" || s.has_notes);
  if (!interesting.length) return;
  const map = { search: "search", bookUrl: "search", toc: "toc", content: "content" };
  const target = interesting.map((s) => map[s.name]).filter(Boolean);
  if (!target.length) return;
  activeTab.value = "rules";
  activeRuleTab.value = target[0];
}

async function loadTagOptions() {
  try {
    const tags = await listTags();
    userTagOptions.value = tags.filter((t) => t.kind === "user");
  } catch (e) {
    userTagOptions.value = [];
    ElMessage.warning("标签列表加载失败，请确认后端已重启");
  }
}

watch(() => props.modelValue, (show) => {
  if (!show && quickStop) {
    quickStop();
    quickStop = null;
    quickLoading.value = false;
  }
});

// 规则一改，上一轮试跑结论就作废了。不标过期的话，用户改了规则
// 还看到绿色的「全部通过」，会据此保存——这正是本次要消除的误导。
watch(form, () => {
  if (testResult.value) testStale.value = true;
  // 预检里的「已连接」= App 里那份与本地规则一致，规则一改这句话就不成立了。
  // 其余三态（连不上 / App 里没有 / 是旧版本）与本地规则无关，留着仍然成立
  if ((appPreflightState.value || {}).state === "ready") appPreflightState.value = null;
  // 用户没动过文本域时保持快照新鲜——否则「应用到表单」会把表单改动全部回滚
  if (!rawDirty) rawJsonText.value = JSON.stringify(form.value, null, 2);
}, { deep: true });

watch(() => [props.modelValue, props.sourceUrl], async ([show, url]) => {
  if (!show) return;
  testResult.value = null;
  // 关弹窗时抽屉会被 destroy-on-close 卸载，但 debugVisible 会留在 true，
  // 下次打开就会自己弹出来；而且挂载时 modelValue 已是 true，
  // 抽屉里那个没有 immediate 的 watch 不触发，:initial-step 会被忽略
  debugVisible.value = false;
  appPreflightState.value = null;   // 预检结果是上一次会话的，别带到这次
  pushed.value = "";
  // 同理：本组件在 SourcesView 里是常驻挂载、从不卸载的，isDuplicate 会跨
  // 「关闭 → 再打开」残留。不复位的话下次打开编辑弹窗会直接是「另存」状态
  // （域名框解禁、标题错成「另存为新源」、跳过查重），而用户以为自己只是在编辑。
  isDuplicate.value = false;
  loadTagOptions();
  activeTab.value = url ? "basic" : "quick";
  activeRuleTab.value = "search";
  extActive.value = ["request"];
  if (!url) {
    form.value = blank();
    systemTags.value = [];
    manualStatus.value = "";
    statusLocked.value = false;
    userTags.value = [];
    quickVerify.value = null;
    quickProgress.value = "";
    syncRawFromForm();
    savedSnapshot.value = JSON.stringify(form.value);   // 新建：记下初始快照
    return;
  }
  loading.value = true;
  try {
    const d = await getDetail(url);
    // 拆系统/用户标签依赖枚举。没就绪就拆，系统标签会被当成用户标签存进
    // userTags，之后再也不会纠正（拆的是快照）。已加载时这里是空操作。
    try { await ensureTagMeta(); } catch (e) { /* 入口已提示，不重复打扰 */ }
    form.value = { ...blank(), ...d.source };
    const parsed = splitSystemUser(d.source.bookSourceGroup || "");
    systemTags.value = parsed.system;
    manualStatus.value = d.system_tags_locked
      ? (parsed.system.find((t) => isStatusTag(t)) || "")
      : "";
    statusLocked.value = !!d.system_tags_locked;
    userTags.value = parsed.user;
    syncRawFromForm();
    savedSnapshot.value = JSON.stringify(form.value);   // 编辑：以加载到的源为基线
  } catch (e) {
    ElMessage.error("加载源失败：" + e.message);
  } finally {
    loading.value = false;
  }
});

async function applyGenerated(result) {
  const src = result.source || {};
  form.value = { ...blank(), ...src };
  const parsed = splitSystemUser(src.bookSourceGroup || "");
  systemTags.value = parsed.system;
  manualStatus.value = "";
  statusLocked.value = false;
  userTags.value = parsed.user;
  quickVerify.value = result.verify || null;
  activeTab.value = "rules";
  activeRuleTab.value = "search";
  syncRawFromForm();
  expandTestFailures(result.verify || null);
  ElMessage.success("已生成候选源，可在下方手动修改规则后保存");
  await nextTick();
  const el = document.querySelector(".edit-dialog .main-rule-form");
  if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
}

async function quickGenerate() {
  const url = quickUrl.value.trim();
  if (!url) return ElMessage.warning("请先填带真实关键词的搜索 URL");
  // 类型键名由后端下发（见 typeKeyOf）。**读空了不能猜**：猜成 "novel" 会把
  // 漫画/听书源生成成小说源，而且界面上完全看不出来。宁可挡在这里
  let typeKey = "";
  try {
    await ensureTagMeta();
    typeKey = typeKeyOf(Number(form.value.bookSourceType));
  } catch (e) { /* 下面统一挡 */ }
  if (!typeKey) {
    return ElMessage.warning("书源类型枚举还没就绪，请稍后重试");
  }
  quickLoading.value = true;
  quickProgress.value = "提交中...";
  quickVerify.value = null;
  try {
    const r = await api.post("/jobs", {
      kind: "add",
      payload: {
        url,
        name: form.value.bookSourceName || "",
        type: typeKey,
        detail_url: quickDetailUrl.value.trim(),
        probe: quickProbe.value,
        discover: quickDiscover.value,
        verify: true,
      },
    });
    quickProgress.value = "任务 " + r.job_id;
    if (quickStop) quickStop();
    quickStop = subscribeJob(
      r.job_id,
      (d) => {
        if (d && d.total) quickProgress.value = `${d.progress || 0}/${d.total}`;
      },
      (d) => {
        quickLoading.value = false;
        quickStop = null;
        if (d.status !== "done") {
          quickProgress.value = "";
          ElMessage.error("生成失败：" + (d.status || "未知错误"));
          return;
        }
        let result = {};
        try { result = JSON.parse(d.result_json || "{}"); } catch (e) { result = {}; }
        if (!result.ok) {
          quickProgress.value = "";
          ElMessage.error(result.error || "生成失败");
          return;
        }
        quickProgress.value = "";
        applyGenerated(result);
      },
    );
  } catch (e) {
    quickLoading.value = false;
    quickProgress.value = "";
    ElMessage.error("提交生成任务失败：" + e.message);
  }
}

// 连 App 调试：**唯一的调试入口**。
//
// 为什么撤掉那条本地离线试跑入口：我们对 Legado 语义的理解有偏差
// （`{{}}` 是 JS 求值不是字符串替换、`ruleContent.image` 字段不存在、
// `webView` 是 URL 规则选项、类型 3 是下载站……），而**本地跑出与 App 不同的
// 结果时它不报错，只是安静地给出另一个答案**——那比没有结果更糟。
// 跑不了 JS 规则的源更是只有 App 那边验得了（Rhino / cookie / webView 全在 App 里）。
// 结果**直接塞进 testResult**——App 调试返回的形状与离线回放一致，
// 所以卡片与调试抽屉零改动。
async function debugRun(keyOverride = "", rerunStep = "") {
  // keyOverride：「从此步重跑」拼好的分段 key（--/++/绝对URL）。空串走表单里选的入口
  // ——两个通道认的是**同一套 key 形态**（都跑 App 的分派），所以这里共享
  const key = keyOverride || debugKey();
  // 除「搜索」外都必须给出 URL（搜索空着会用默认关键词兜底）。放空进去会拼出
  // `发现::` / `++` 这种 App 认不了的目标——它对无效 key 是**静默无响应**，
  // 排查成本极高，宁可在这里挡住。
  if (!key) {
    return ElMessage.warning("这个源没配 exploreUrl，请先填发现页 URL");
  }
  testResult.value = null;
  testStale.value = false;
  if (debugChannel.value === "jvm") {
    // 本机引擎：**不填 IP、不推送、不预检**——它跑的就是 App 的源码。
    // 返回值与连 App 那条同形状，只是 source 是 "jvm"
    appDebugging.value = true;
    try {
      const r = await jvmDebug(form.value, key, 60, "", appCacheMode.value);
      testResult.value = r && r.error ? { error: r.error } : r;
    } catch (e) {
      testResult.value = { error: String(e.message) };
    } finally {
      appDebugging.value = false;
    }
    expandTestFailures(testResult.value);
    if (testResult.value && !testResult.value.error) openDebug(rerunStep);
    return;
  }
  const host = appHost.value.trim();
  if (!host) return ElMessage.warning("请先填 App 的 IP（App 通知栏里有）");
  appDebugging.value = true;
  pushed.value = "";
  try {
    // **先预检**。调试 WS 对 App 库里查不到的 tag 什么都不做，只能干等到超时；
    // 而且它跑的始终是 **App 里那份规则**，所以「App 里是旧版本」这种情况会
    // 悄悄答非所问——预检一并判掉
    const pf = await runPreflight(host);
    if (pf.state === "unreachable") {
      testResult.value = { error: pf.error };
      return;
    }
    // 没有 / 是旧版本 → 得先把当前表单推过去，否则调试跑的是 App 那份而不是你在
    // 改的这份。但推送会写 App 的数据，**必须问过用户**（见 confirmPush）
    const needPush = pf.state !== "ready";
    if (needPush && !(await confirmPush(pf.state))) {
      return;   // 取消：不推也不调试，预检结果留在卡片上
    }
    // 传 form.value（当前编辑中的源）：它的 bookSourceUrl 来自详情接口，是
    // 导入原文——后端要拿它当 tag，规范化过的 URL 会让 App 静默无响应
    const r = await appDebug(form.value, key, host, 0, needPush, appCacheMode.value);
    // 连不上时后端返回的是 {source:"app", error:"..."}，不是 HTTP 错误；
    // 这里翻成卡片认得的形状（卡片读 testResult.error）
    testResult.value = r && r.error ? { error: r.error } : r;
    if (!testResult.value.error) {
      pushed.value = needPush ? pf.state : "";
      appPreflightState.value = { state: "ready", error: "", detail: "" };
    }
  } catch (e) {
    testResult.value = { error: String(e.message) };
  } finally {
    appDebugging.value = false;
  }
  expandTestFailures(testResult.value);
  // 成功了才自动摊开证据；失败时卡片上那段错误信息本身就是要看的东西。
  // rerunStep：「从此步重跑」要直接定位到重跑的那一步
  if (testResult.value && !testResult.value.error) openDebug(rerunStep);
}

//: 步骤名 → 分段重跑的 key 构造。形态与 App `Debug.startDebug` 的 when 链
//: 一一对应：绝对URL → 详情起步（详情→目录→正文）/ `++` → 目录起步 /
//: `--` → 只跑正文；搜索/发现本来就是链头，重跑即整链。
const STEP_KEY_BUILDERS = {
  search: () => debugKey(),
  explore: (url) => `发现::${url}`,
  bookUrl: (url) => url,
  toc: (url) => `++${url}`,
  content: (url) => `--${url}`,
};

//: 抽屉里的「从此步重跑」：拿上轮结果里这一步的 URL 拼分段 key，整链重跑的
//: 摩擦（搜索→详情→目录全重来）就砍掉了。URL 用**原文**：它是 App 自己请求过的
//: 地址，传回去 App 端 AnalyzeUrl 能吃（含 ,{...} 选项形态）；库内那种
//: 规范化（AGENTS #5）是给关联键用的，这里做了反而改坏 App 的目标。
function rerunFromStep(stepName) {
  const build = STEP_KEY_BUILDERS[stepName];
  if (!build) return;
  const step = ((testResult.value || {}).steps || [])
    .find((s) => s.name === stepName) || {};
  // 幂等去前缀（同 buildDebugKey 的手抄兜底）：step.url 不该带前缀，防一手
  const url = String(step.url || "").trim().replace(/^(\+\+|--)/, "");
  const key = stepName === "search" ? build() : (url ? build(url) : "");
  if (!key) {
    return ElMessage.warning(
      "上一轮结果里没有这一步的链接。先跑一次完整调试，再重试这一步");
  }
  return debugRun(key, stepName);
}

//: 预检状态 → 卡片上的短标签与颜色
const PREFLIGHT_TEXT = {
  ready: "已连接", missing: "App 里没有该源",
  stale: "App 里是旧版本", unreachable: "连不上",
};
const PREFLIGHT_TYPE = {
  ready: "success", missing: "warning", stale: "warning", unreachable: "danger",
};
const preflightText = computed(
  () => PREFLIGHT_TEXT[(appPreflightState.value || {}).state] || "");
const preflightType = computed(
  () => PREFLIGHT_TYPE[(appPreflightState.value || {}).state] || "info");

//: 本次调试前做了什么推送："" | "missing"（新建）| "stale"（覆盖旧规则）
const pushed = ref("");

//: 在途的预检请求。输入框失焦和「连 App 调试」会来问同一个问题——点按钮时
//: 输入框必然先失焦——合成一次：它们是同一个问题，没必要问 App 两遍，
//: 并发还会让「检测中…」在第一个先回来时提前熄灭
let preflightPending = null;

// 只读预检：连不连得上、App 里有没有这个源、是不是旧版本。
// 结果照常写进 appPreflightState（卡片和调试流程都读它），不弹 toast——
// 标签就在输入框下一行，每次失焦弹一次太吵；动作的提示留给「连 App 调试」。
async function runPreflight(host) {
  if (!preflightPending) {
    preflightPending = (async () => {
      appChecking.value = true;
      try {
        // 用 form.value：它的 bookSourceUrl 是导入原文，App 那边按精确字符串匹配；
        // 整份传过去才能和 App 里那份比对规则（见 core/app_debug.py:preflight）
        appPreflightState.value = await appPreflight(form.value, host, 0);
      } catch (e) {
        appPreflightState.value = { state: "unreachable", error: String(e.message) };
      } finally {
        appChecking.value = false;
        preflightPending = null;
      }
      // 原始异常只记 console，不往界面上摆（那是开发者视角）。但也不能丢——
      // 「连不上」必须留得下痕迹
      const s = appPreflightState.value;
      if (s.detail) console.warn("[app-debug] 预检失败:", s.detail);
    })();
  }
  await preflightPending;
  return appPreflightState.value;
}

// IP 输入框的失焦 / Enter：只读预检，就地更新卡片上那个 tag。
//
// 它以前是「测试连接」按钮，撤掉的理由：这段预检「连 App 调试」本来就会跑
// （见 debugRun），两者结果落在同一个 tag 上——一个动作没必要占两个入口。
// 挪到输入框上反而更顺：填完 IP 松手就有反馈，还省下一行按钮。
async function appPreflightRun() {
  const host = appHost.value.trim();
  // 清空 IP 就把 tag 一并清掉：留着上一轮的「已连接」等于说假话
  if (!host) {
    appPreflightState.value = null;
    return;
  }
  // 新建 / 另存时域名还没填，后端会直接判 unreachable（core/app_debug.py:286）。
  // 那不是「连不上」，不该摆到卡片上——静默跳过，等域名填了再说
  if (!String(form.value.bookSourceUrl || "").trim()) return;
  await runPreflight(host);
}

//: 推送前确认，返回用户是否同意。
//:
//: 「调试」和「往 App 里写数据」是两件事——把后者做成前者的隐式副作用，用户点的
//: 是调试、App 里的源却被改了。少了这一步，「连 App 调试」就变成一个有写副作用的
//: 按钮，而这在界面上完全看不出来。
//:
//: stale 那档尤其要说清楚：它不只是确认，更是把「你现在调试的是 App 那份，不是你
//: 正在改的」摆到用户面前——不知道这件事的话，他会拿着一份答非所问的结果去改规则。
function confirmPush(state) {
  const text = state === "missing"
    ? "App 里没有这个源。要先推送到 App 再调试吗？"
    : "App 里是旧版本。直接调试会跑 App 里那份规则，不是你正在改的。"
      + "要先推过去覆盖它再调试吗？";
  return ElMessageBox.confirm(text, "推送并调试", {
    confirmButtonText: "推送并调试", cancelButtonText: "取消", type: "warning",
  }).then(() => true).catch(() => false);
}

//: 调试抽屉里点「用这条」→ 写进表单对应字段。
//:
//: **只改表单、不落库**：保存仍由用户自己决定（那个按钮在弹窗底部）。
//: 路径形如 `ruleSearch.bookList`，与 `utils/ruleCandidates.FIELD_OF_STEP` 同源
function onApplyRule({ field, rule }) {
  const [group, key] = String(field || "").split(".");
  if (!group || !key || !form.value[group]) return;
  form.value[group][key] = rule;
  ElMessage.success("已填入 " + field + "，在抽屉里点「重新调试本页」看效果");
}

function openDebug(step) {
  debugStep.value = step || "";
  debugVisible.value = true;
}

// 抽屉请求跳到某个页签。两种形态：
//   - 字符串：老的「去改类型」用法，只切主页签
//   - 对象 {tab, ruleTab}：失败步骤跳转，可以一路定位到规则子页签
// **这里绝不做任何写入**：改类型是写操作，必须走用户明确确认的保存流程，
// 此处只负责把人送过去。
function onDebugGoto(target) {
  debugVisible.value = false;
  if (!target || typeof target === "string") {
    activeTab.value = target || "basic";
    return;
  }
  if (target.tab) activeTab.value = target.tab;
  if (target.ruleTab) activeRuleTab.value = target.ruleTab;
}

//: 抽屉的「用本页重放」要按步骤取当前表单里的规则——所以改完规则不用保存、
//: 不用重跑链路，直接重放就能看判定变没变
const ruleByStep = computed(() => {
  const rs = form.value.ruleSearch || {};
  const rt = form.value.ruleToc || {};
  const rc = form.value.ruleContent || {};
  return {
    search: rs.bookList || "",
    bookUrl: rs.bookUrl || "",
    toc: rt.chapterList || "",
    content: rc.content || "",
  };
});

// 「另存为新源」：编辑模式下域名不可改，原先只提示「请另存为新源」却没有这个入口。
// 清空域名是刻意的——另存为必须换个域名（Legado 以域名为主键，域名相同就是覆盖原源）。
function startDuplicate() {
  isDuplicate.value = true;
  form.value.bookSourceUrl = "";
  ElMessage.info("已切换为另存模式，请填写新的域名");
}

async function save() {
  // 保存前必须 sanitize：bookSourceType 为 ""/[]/"2" 会让 Legado 导入报 IllegalStateException
  const s = JSON.parse(JSON.stringify(form.value));
  s.bookSourceType = Number(s.bookSourceType);
  // 合法取值只有 Legado 的 0/1/2/3；4 及其它脏值一律归 0（文本）
  if (![0, 1, 2, 3].includes(s.bookSourceType)) s.bookSourceType = 0;
  // `enabledExplore` **在这里推导**，不靠用户维护那个开关：没配发现规则一律关
  // （否则 App 的发现页会列出一堆点了没反应的源），配了则遵从「隐藏」那个例外
  s.enabledExplore = !!(String(s.exploreUrl || "").trim()
                        || Object.keys(s.ruleExplore || {}).length)
                     && !hideExplore.value;
  if (!String(s.bookSourceName || "").trim()) return ElMessage.warning("名称不能为空");
  if (!String(s.bookSourceUrl || "").trim()) return ElMessage.warning("域名不能为空");
  // 新建书源时若填了已存在的域名，后端 upsert_sources 会按 URL 主键
  // **静默覆盖原源的全部规则**——用户全程无感。保存前先探一下。
  // 另存模式下用户的旧域名已被清空，走的是全新域名，不该再拿旧域名去查重
  if (isNew.value && !isDuplicate.value) {
    try {
      const r = await sourceExists(s.bookSourceUrl);
      if (r.exists) {
        try {
          await ElMessageBox.confirm(
            `域名已存在（源名：${r.name || "（无名）"}），继续将覆盖其全部规则。`,
            "确认覆盖", { type: "warning", confirmButtonText: "覆盖", cancelButtonText: "取消" },
          );
        } catch (e) {
          return;   // 用户取消：必须中止保存，不能吞掉确认结果继续往下走
        }
      }
    } catch (e) {
      // 探测失败不阻塞保存，只在控制台留痕
      console.warn("exists 探测失败", e);
    }
  }
  await doSave(s);
}

async function doSave(s) {
  s.bookSourceGroup = mergeGroup(displaySystemTags.value, userTags.value);
  try {
    await saveSource(s, userTags.value, statusLocked.value && !!manualStatus.value);
    ElMessage.success("已保存");
    savedSnapshot.value = JSON.stringify(form.value);   // 保存成功后刷新快照
    emit("saved", s);
  } catch (e) {
    ElMessage.error("保存失败：" + e.message);
  }
}
</script>

<template>
  <el-dialog v-model="visible"
             width="1120px" top="4vh" destroy-on-close class="edit-dialog"
             modal-class="edit-dialog-overlay"
             :close-on-click-modal="false" :before-close="handleBeforeClose">
    <!-- 标题改用插槽：要在标题旁挂「另存为新源」入口（编辑模式专属） -->
    <template #header>
      <div class="dialog-header">
        <span>{{ isDuplicate ? "另存为新源" : (isNew ? "新建书源" : "编辑书源") }}</span>
        <el-button v-if="!isNew && !isDuplicate" size="small" link type="primary"
                   @click="startDuplicate">另存为新源</el-button>
      </div>
    </template>
    <el-row :gutter="16" v-loading="loading" class="main-rule-form">
      <el-col :xs="24" :sm="24" :md="15">
        <el-tabs v-model="activeTab" :tab-position="isMobile ? 'top' : 'left'"
                 class="source-tabs">
          <!-- 另存模式不显示「快速生成」：它是「整份替换表单」的入口
               （applyGenerated 里 form.value = {...blank(), ...src}），
               在另存模式下点它会丢掉正要另存的那份规则 -->
          <el-tab-pane v-if="isNew && !isDuplicate" name="quick">
            <template #label>
              <span class="tab-label">快速生成<i class="dot" :class="tabDot('quick')"></i></span>
            </template>
            <el-form size="small" label-width="76px">
              <el-form-item label="搜索 URL">
                <el-input v-model="quickUrl"
                          placeholder="https://site/search?q=关键词（必须带真实关键词）" />
              </el-form-item>
              <el-form-item label="详情页">
                <el-input v-model="quickDetailUrl"
                          placeholder="可选：详情页样例 URL，不填自动取搜索结果第一条" />
              </el-form-item>
              <el-form-item label="选项">
                <el-checkbox v-model="quickProbe">自动探测搜索端点</el-checkbox>
                <el-checkbox v-model="quickDiscover">仅发现模式</el-checkbox>
                <el-button type="primary" :loading="quickLoading" @click="quickGenerate"
                           style="margin-left: 12px">自动生成</el-button>
                <span v-if="quickProgress" class="muted" style="margin-left: 8px">{{ quickProgress }}</span>
              </el-form-item>
            </el-form>
            <div v-if="quickVerify" class="quick-verify">
              <!-- 这是**本地离线回放**的结果（verify_chain），不是 App 实测：
                   只判「取到值 / 不报错」，且跑不了 JS 规则。必须标出来——
                   它和右侧 App 调试的结果用的是同一套三态视觉，不标就分不清
                   哪份可信 -->
              <p class="muted" style="margin: 0 0 8px">
                <el-tag size="small" type="info">本地调试 · 仅供参考</el-tag>
                <span style="margin-left: 6px">
                  只检查是否取到值，不支持 JS 规则。要确认请用右侧「连 App 调试」。
                </span>
              </p>
              <!-- 与试跑卡片同一套三态渲染：同一个 as_step_dict 产出的数据，
                   这里若还用 ok 两态，「pass + 附注」会显示成绿色，与试跑卡片矛盾 -->
              <div v-for="s in quickVerify.steps" :key="s.name" class="quick-step">
                <el-tag size="small" :type="tagTypeOf(s)">{{ s.name }}</el-tag>
                <span class="muted">{{ s.detail }}</span>
              </div>
              <p v-if="quickVerify.steps && quickVerify.steps.length"
                 class="muted" style="margin: 6px 0 0">
                <b>{{ testSummary(quickVerify.steps).text }}</b>
                <span v-if="quickVerify.steps.some((s) => s.has_notes)">（有疑点，见调试详情）</span>
                <span>{{ summaryHint(testSummary(quickVerify.steps)) }}</span>
              </p>
            </div>
          </el-tab-pane>
          <el-tab-pane name="basic">
            <template #label>
              <span class="tab-label">基本信息<i class="dot" :class="tabDot('basic')"></i></span>
            </template>
            <el-form :label-width="isMobile ? 'auto' : '96px'"
                     :label-position="isMobile ? 'top' : 'right'" size="small">
              <el-form-item label="名称"><el-input v-model="form.bookSourceName" /></el-form-item>
              <el-form-item label="域名">
                <el-input v-model="form.bookSourceUrl" :disabled="!isNew"
                          placeholder="https://example.com" />
                <span v-if="!isNew" class="muted">编辑模式下域名不可改；需要更换请点右上角「另存为新源」。</span>
              </el-form-item>
              <el-form-item label="类型">
                <el-radio-group v-model="form.bookSourceType">
                  <el-radio-button v-for="t in sourceTypes" :key="t.value" :value="t.value">
                    {{ t.tag }}
                  </el-radio-button>
                </el-radio-group>
              </el-form-item>
              <el-form-item label="启用"><el-switch v-model="form.enabled" /></el-form-item>
              <el-form-item label="备注">
                <el-input v-model="form.bookSourceComment" type="textarea" :rows="2" />
              </el-form-item>
              <el-form-item label="健康状态">
                <el-switch v-model="statusLocked" :disabled="!currentStatus"
                           active-text="锁定" inactive-text="自动" inline-prompt />
                <el-select v-if="statusLocked" v-model="manualStatus"
                           style="width: 170px; margin-left: 10px">
                  <el-option v-for="t in statusTags" :key="t" :label="t" :value="t" />
                </el-select>
                <span v-else class="muted" style="margin-left: 10px">
                  跟随校验结果{{ currentStatus ? "：" + currentStatus : "（尚无校验结果）" }}
                </span>
              </el-form-item>
              <el-form-item label="标签">
                <div style="width: 100%">
                  <div style="display: flex; align-items: center; gap: 6px; flex-wrap: wrap">
                    <span class="muted">自动维护</span>
                    <el-tooltip v-for="t in qualityTags" :key="t" placement="top"
                                content="由校验结果自动判定，不可手动修改">
                      <el-tag size="small" type="info" effect="plain">{{ t }}</el-tag>
                    </el-tooltip>
                    <span v-if="!qualityTags.length" class="muted">（暂无）</span>
                  </div>
                  <el-select v-model="userTags" multiple filterable allow-create default-first-option
                             :reserve-keyword="false" style="width: 100%; margin-top: 8px"
                             placeholder="我的标签：选择或输入，如 原创、精排、R18">
                    <el-option v-for="t in userTagOptions" :key="t.tag" :value="t.tag"
                               :label="t.tag + ' (' + t.count + ')'" />
                  </el-select>
                </div>
              </el-form-item>
              <el-form-item label="编码">
                <el-input v-model="form.charset" placeholder="如 gbk，留空默认 utf-8" />
              </el-form-item>
              <el-form-item label="排序/权重">
                <el-input-number v-model="form.customOrder" :min="0" controls-position="right" />
                <el-input-number v-model="form.weight" :min="0" controls-position="right"
                                 style="margin-left: 8px" />
              </el-form-item>
            </el-form>
          </el-tab-pane>
          <el-tab-pane name="rules">
            <template #label>
              <span class="tab-label">规则配置<i class="dot" :class="tabDot('rules')"></i></span>
            </template>
            <el-form :label-width="isMobile ? 'auto' : '96px'"
                     :label-position="isMobile ? 'top' : 'right'" size="small">
              <el-tabs v-model="activeRuleTab" class="rule-tabs">
                <el-tab-pane name="search" label="搜索">
                  <el-form-item label="搜索 URL">
                    <el-input v-model="form.searchUrl" placeholder="/search?q={{key}}" />
                  </el-form-item>
                  <el-form-item label="bookList">
                    <el-input v-model="form.ruleSearch.bookList" placeholder="class.book-list@tag.li" />
                  </el-form-item>
                  <el-form-item label="name">
                    <el-input v-model="form.ruleSearch.name" placeholder="tag.a@text" />
                  </el-form-item>
                  <el-form-item label="bookUrl">
                    <el-input v-model="form.ruleSearch.bookUrl" placeholder="tag.a@href" />
                  </el-form-item>
                  <el-form-item label="coverUrl">
                    <el-input v-model="form.ruleSearch.coverUrl" placeholder="tag.img@data-original" />
                  </el-form-item>
                  <el-form-item label="作者">
                    <el-input v-model="form.ruleSearch.author" placeholder="class.author@text##作者：##" />
                  </el-form-item>
                  <el-form-item label="简介">
                    <el-input v-model="form.ruleSearch.intro" placeholder="class.intro@text" />
                  </el-form-item>
                </el-tab-pane>
                <el-tab-pane name="detail" label="详情">
                  <el-form-item label="name">
                    <el-input v-model="form.ruleBookInfo.name" placeholder="tag.h1@text" />
                  </el-form-item>
                  <el-form-item label="coverUrl">
                    <el-input v-model="form.ruleBookInfo.coverUrl" placeholder="class.cover@tag.img@src" />
                  </el-form-item>
                  <el-form-item label="author">
                    <el-input v-model="form.ruleBookInfo.author" placeholder="class.author@text" />
                  </el-form-item>
                  <el-form-item label="intro">
                    <el-input v-model="form.ruleBookInfo.intro" placeholder="class.intro@text" />
                  </el-form-item>
                  <el-form-item label="lastChapter">
                    <el-input v-model="form.ruleBookInfo.lastChapter" placeholder="class.last@tag.a@text" />
                  </el-form-item>
                  <el-form-item label="tocUrl">
                    <el-input v-model="form.ruleBookInfo.tocUrl" placeholder="可选，目录页 URL" />
                  </el-form-item>
                </el-tab-pane>
                <el-tab-pane name="toc" label="目录">
                  <el-form-item label="chapterList">
                    <el-input v-model="form.ruleToc.chapterList" placeholder="class.chapter@tag.a" />
                  </el-form-item>
                  <el-form-item label="chapterName">
                    <el-input v-model="form.ruleToc.chapterName" placeholder="tag.a@text" />
                  </el-form-item>
                  <el-form-item label="chapterUrl">
                    <el-input v-model="form.ruleToc.chapterUrl" placeholder="tag.a@href" />
                  </el-form-item>
                  <el-form-item label="nextTocUrl">
                    <el-input v-model="form.ruleToc.nextTocUrl" placeholder="class.next@tag.a@href" />
                  </el-form-item>
                </el-tab-pane>
                <el-tab-pane name="content" label="正文">
                  <el-form-item label="content">
                    <el-input v-model="form.ruleContent.content" placeholder="id.content@text" />
                  </el-form-item>
                  <el-form-item label="nextContentUrl">
                    <el-input v-model="form.ruleContent.nextContentUrl" placeholder="class.next@tag.a@href" />
                  </el-form-item>
                  <el-form-item label="imageStyle">
                    <el-select v-model="form.ruleContent.imageStyle" clearable style="width: 100%">
                      <el-option value="" label="默认" />
                      <el-option value="FULL" label="FULL" />
                      <el-option value="TEXT" label="TEXT" />
                    </el-select>
                  </el-form-item>
                  <!-- **这里原本有个 `webView` 开关，删掉了**：Legado 的 `ContentRule`
                       没有这个字段（`ContentRule.kt:12-25`），写进去 App 根本不读——
                       一个开了没用的开关比没有更糟。`webView` 是 **URL 规则**的选项，
                       写在 URL 后面（如 `url,{"webView":true}`，见 AnalyzeUrl.kt:254）。
                       实测库里 0 条源带过这个字段，所以删掉没有任何数据要迁 -->
                  <el-form-item label="webJs">
                    <el-input v-model="form.ruleContent.webJs" type="textarea" :rows="3"
                              placeholder="页面内执行的 ES5 JS，返回正文内容" />
                    <div class="muted" style="line-height: 1.5">
                      只在对应的 URL 规则开了 <code>webView</code> 时才生效。
                      要写成 <code>url,{"webView":true}</code> 挂在
                      <code>目录规则</code> 的 <code>chapterUrl</code> 后面。
                      没开的话这段 JS 在 App 里会被静默忽略。
                    </div>
                  </el-form-item>
                </el-tab-pane>
              </el-tabs>
            </el-form>
          </el-tab-pane>
          <el-tab-pane name="ext">
            <template #label>
              <span class="tab-label">扩展配置<i class="dot" :class="tabDot('ext')"></i></span>
            </template>
            <el-form :label-width="isMobile ? 'auto' : '96px'"
                     :label-position="isMobile ? 'top' : 'right'" size="small">
              <el-collapse v-model="extActive">
                <el-collapse-item name="request" title="请求配置">
                  <el-form-item label="header">
                    <el-input v-model="form.header" type="textarea" :rows="3"
                              placeholder='{"User-Agent":"...","Referer":"..."}' />
                  </el-form-item>
                  <el-form-item label="loginUrl">
                    <el-input v-model="form.loginUrl" placeholder="登录页 URL（可选）" />
                  </el-form-item>
                  <el-form-item label="loginCheckJs">
                    <el-input v-model="form.loginCheckJs" type="textarea" :rows="2"
                              placeholder="登录检测 JS（可选）" />
                  </el-form-item>
                  <el-form-item label="并发限制">
                    <el-input-number v-model="form.concurrentRate" :min="1" controls-position="right" />
                  </el-form-item>
                </el-collapse-item>
                <el-collapse-item name="discover" title="发现配置">
                  <!-- **不是一个中立的开关**：`enabledExplore` 由配置推导，
                       这里只表达「配了但不想显示」这一个例外。原来的开关会让用户
                       手动维护一个算得出来的状态——实测 739 条开着却没配置、
                       133 条有配置却关着，两边都是错的 -->
                  <el-form-item label="发现">
                    <span v-if="!hasExploreConfig" class="muted">
                      未配置发现规则，不会出现在 App 的发现页
                    </span>
                    <template v-else>
                      <el-checkbox v-model="hideExplore">在 App 的发现页隐藏</el-checkbox>
                      <span class="muted" style="margin-left: 8px">
                        留空则跟随配置（配了就显示）
                      </span>
                    </template>
                  </el-form-item>
                  <el-form-item label="exploreUrl">
                    <el-input v-model="form.exploreUrl" type="textarea" :rows="3"
                              placeholder='[{"title":"分类","url":"/list?page={{page}}"}]' />
                  </el-form-item>
                  <p class="muted" style="margin: 0 0 8px">
                    <b v-if="!form.exploreUrl" style="color: #e6a23c">
                      还没填 exploreUrl。开启发现也不会有发现页。
                    </b>
                    ruleExplore 结构较复杂，可在「原始 JSON」里编辑。
                  </p>
                </el-collapse-item>
                <el-collapse-item name="interaction" title="交互与事件">
                  <el-form-item label="customButton">
                    <el-switch v-model="form.customButton" />
                  </el-form-item>
                  <el-form-item label="eventListener">
                    <el-switch v-model="form.eventListener" />
                  </el-form-item>
                  <el-form-item label="variableComment">
                    <el-input v-model="form.variableComment" type="textarea" :rows="2"
                              placeholder="变量说明（可选）" />
                  </el-form-item>
                  <el-form-item label="jsLib">
                    <el-input v-model="form.jsLib" type="textarea" :rows="3"
                              placeholder="公共 JS 库（可选）" />
                  </el-form-item>
                </el-collapse-item>
              </el-collapse>
            </el-form>
          </el-tab-pane>

          <el-tab-pane name="raw">
            <template #label>
              <span class="tab-label">原始 JSON<i class="dot" :class="tabDot('raw')"></i></span>
            </template>
            <p class="muted" style="margin: 0 0 8px">
              原始兜底：可直接编辑整条书源 JSON。建议先点「从表单生成」再修改。
            </p>
            <el-alert v-if="rawDirty" type="warning" :closable="false" show-icon
                      style="margin-bottom: 8px"
                      title="你正在手改这段 JSON。点「从表单生成」会覆盖你的改动。" />
            <el-input v-model="rawJsonText" type="textarea" :rows="18" class="raw-json"
                      @input="rawDirty = true" />
            <p v-if="rawJsonError" style="color: #f56c6c; margin: 6px 0 0">{{ rawJsonError }}</p>
            <div class="toolbar" style="margin-top: 8px">
              <el-button size="small" @click="syncRawFromForm">从表单生成</el-button>
              <el-button size="small" type="primary" @click="applyRawJson">应用到表单</el-button>
            </div>
          </el-tab-pane>
        </el-tabs>
      </el-col>

      <el-col :xs="24" :sm="24" :md="9">
        <el-card shadow="never" header="调试" class="sticky-test">
          <!-- 通道：**默认本机引擎**——App 的源码跑在本机（Robolectric），不填 IP、
               不推送、不预检；连 App 那条留给「登录态/网络出口/WebView 都在手机上」
               的场合。两者跑的是**同一段 App 代码**，所以分段结果同形状、可直接对比 -->
          <el-radio-group v-model="debugChannel" size="small" class="debug-targets"
                          style="margin-bottom: 8px">
            <el-radio-button value="jvm">本机引擎</el-radio-button>
            <el-radio-button value="app">连 App</el-radio-button>
          </el-radio-group>
          <!-- 调试目标照 App 调试界面的 chip 行做。这排 chip 只负责改 placeholder
               和拼 key 前缀，**不改变 App 的分派**——它认的是 key 的形态，
               所以在这排选什么并不会「锁死」链路，理由见 buildDebugKey 上方 -->
          <el-radio-group v-model="debugTarget" size="small" class="debug-targets">
            <el-radio-button v-for="t in DEBUG_TARGETS" :key="t.value" :value="t.value">
              {{ t.label }}
            </el-radio-button>
          </el-radio-group>
          <div class="toolbar" style="margin-top: 8px">
            <el-input v-model="debugQuery" size="small" :placeholder="debugHint"
                      style="flex: 1 1 150px" @keyup.enter="debugRun()" />
          </div>
          <!-- 连 App 调试：我们离线回放不了 JS 规则（<js> / @js:），
               而 App 内建的调试 WebSocket 能跑完整链路。IP 填 App 通知栏里
               显示的那个（端口取 HTTP 端口 + 1，默认 1123），填过就记住了。
               失焦 / Enter 即预检（只读，不写 App），所以这一行不需要
               「测试连接」占位——见 appPreflightRun -->
          <div class="toolbar" style="margin-top: 8px">
            <!-- 只有连 App 才需要 IP：本机引擎就在这台机器上跑 -->
            <el-input v-if="debugChannel === 'app'" v-model="appHost" size="small"
                      placeholder="App 的 IP，如 192.168.1.5"
                      style="flex: 1 1 150px"
                      @blur="appPreflightRun" @keyup.enter="appPreflightRun" />
            <!-- 页面缓存策略：只管**我们补抓的那几页**（链路本身是 App 在跑）。
                 默认「用缓存」——调试的反馈环原来是重新联网（一条链 2.4 秒），
                 而解析本身是毫秒级 -->
            <el-select v-model="appCacheMode" size="small" style="width: 112px">
              <el-option v-for="m in DEBUG_CACHE_MODES" :key="m.value"
                         :value="m.value" :label="m.label" />
            </el-select>
            <!-- 唯一的按钮：该不该先推送由预检的三态决定（App 里没有 / 是旧版本 /
                 一致），用户不必知道这一层。推送走 App 的 HTTP 接口，幂等 -->
            <el-button type="primary" size="small" :loading="appDebugging"
                       @click="debugRun()">
              {{ debugChannel === "jvm" ? "开始调试" : "连 App 调试" }}
            </el-button>
          </div>
          <!-- 预检结果就地显示。以前只有一个「连」按钮：连不上或缺源都要干等
               60 秒超时，而且两者表现完全一样，没法对症下药。「检测中」得留一格：
               预检现在是失焦触发的，不给在途状态就成了「点完什么也没发生」 -->
          <!-- 本机引擎的登录态提示：登录墙的源要在**我们自己的浏览器 profile** 里
               登一次（A3），之后按源 URL 自动带上——不说的话用户只会看到「需登录」 -->
          <p v-if="debugChannel === 'jvm'" class="muted" style="margin: 6px 0 0">
            需登录的源：先用 <code>scripts/jvm_login.py</code> 打开浏览器登录一次，
            之后调试会自动带上登录态。
          </p>
          <p v-if="debugChannel === 'app' && (appPreflightState || appChecking)"
             style="margin: 8px 0 0">
            <template v-if="appChecking">
              <el-tag size="small" type="info">检测中…</el-tag>
            </template>
            <template v-else>
              <el-tag size="small" :type="preflightType">{{ preflightText }}</el-tag>
              <span class="muted" style="margin-left: 6px">{{ appPreflightState.error }}</span>
            </template>
          </p>
          <p v-if="debugChannel === 'app' && pushed" style="margin: 8px 0 0">
            <el-tag size="small" type="success">已推送到 App</el-tag>
            <span class="muted" style="margin-left: 6px">
              {{ pushed === "missing" ? "（新建）" : "（覆盖了 App 里的旧规则）" }}
            </span>
          </p>
          <!-- 空态只在没跑过时出现，正好承接「第一次用才知道」的事：
               IP 从哪来。跑过一次它就自己消失，不常驻占地方。
               「需要开 Web 服务、同一局域网」不再单说——连不上时后端那句
               error 已经说了，而且更全（还带端口）。
               本机引擎那侧不写空态：那个通道没有需要解释的东西 -->
          <div v-if="!testResult && debugChannel === 'app'" class="muted"
               style="padding: 22px; text-align: center">
            在 App 里打开「Web 服务」，把通知栏显示的 IP 填到上面的输入框。
          </div>
          <template v-else-if="testResult && !testResult.error">
            <div v-for="s in testResult.steps" :key="s.name" class="quick-step">
              <el-tag size="small" :type="tagTypeOf(s)">
                {{ STEP_LABELS[s.name] || s.name }}
              </el-tag>
              <span class="muted">{{ s.detail }}</span>
            </div>
            <el-alert v-if="testStale" type="info" :closable="false" show-icon
                      style="margin-top: 10px"
                      title="规则已改动，结果已过期，请重跑" />
            <p v-else style="margin: 10px 0 0">
              <b>{{ testSummary(testResult.steps).text }}</b>
              <span v-if="testResult.steps.some((s) => s.has_notes)"
                    class="muted">（有疑点，见附注）</span>
            </p>
            <div class="toolbar" style="margin-top: 8px">
              <el-button size="small" type="primary" plain @click="openDebug()">
                查看证据
              </el-button>
            </div>
          </template>
          <pre v-else-if="testResult" class="mono"
               style="margin-top: 10px">{{ testResult.error }}</pre>
        </el-card>

        <!-- grammar-card 这个 class 是给 styles.css 的桌面端布局用的：它要在右列
             的 flex 列里吃掉剩余高度并自己滚。不加 class 的话只能靠 :last-child
             去猜，将来中间插一张卡片就会选错。 -->
        <el-card shadow="never" header="语法速查" class="grammar-card"
                 style="margin-top: 12px">
          <!-- 上半组逐条对过 core/rules/replayer.py（RULE_PREFIXES / VALUE_ACTIONS /
               COMMON_ATTRS / parse_rule），都是本地回放得了的——照它写，「本地粗略
               验证」一定给得出结论。下半组是 Legado 支持、但我们回放不了的，写了
               就只剩「连 App 调试」一条路；提前标出来，免得在本地看到灰点「无法判定」
               时以为是自己写错了规则。 -->
          <ul class="muted" style="margin: 0; padding-left: 18px; line-height: 1.6">
            <li>简写：class.xxx → .xxx；tag.a → a</li>
            <li>链式：class.list@tag.li@tag.a@href</li>
            <li>索引：.0 第一个、.-1 最后一个</li>
            <li>取值：@text / @textNodes / @ownText / @html / @all</li>
            <li>属性：@href / @src / @data-original 等</li>
            <li>正则：规则##正则##替换（支持 $1）</li>
            <li>类型前缀：@css: / @@ / @xpath: / @json:（没有 @html: 前缀——@html 是取值动作）</li>
            <li>接口源：$.data.list[*].name</li>
            <li>取图：tag.img@src / @data-original</li>
          </ul>
        </el-card>
      </el-col>
    </el-row>

    <template #footer>
      <el-button @click="handleBeforeClose(() => (visible = false))">取消</el-button>
      <el-button type="primary" @click="save">保存</el-button>
    </template>

    <!-- 抽屉常驻挂载，靠 v-model 控制显隐。**不要改成 v-if**：抽屉里的 watch 没有
         immediate，initialStep 只在 modelValue 由 false → true 时生效，用 v-if 会让
         首帧落到 steps[0] 而忽略 :initial-step -->
    <RuleDebugDrawer v-model="debugVisible" :result="testResult"
                     :initial-step="debugStep" :rules="ruleByStep"
                     :source-type="Number(form.bookSourceType) || 0"
                     :enabled-cookie-jar="!!form.enabledCookieJar"
                     :rerunning="appDebugging"
                     @goto="onDebugGoto" @apply-rule="onApplyRule"
                     @rerun-from="rerunFromStep" />
  </el-dialog>
</template>

<style scoped>
.source-tabs { min-height: 520px; }
.source-tabs :deep(.el-tabs__header) { flex: 0 0 auto; }
.tab-label { display: inline-flex; align-items: center; gap: 6px; }
/* 5 个调试目标按钮：右列约 380px 宽，5 个两字按钮恰好放得下，这里只做换行兜底
   （字号或容器宽度一变就不会横向溢出、把卡片撑破） */
.debug-targets { display: flex; flex-wrap: wrap; }
.dialog-header { display: flex; align-items: center; gap: 12px; }
/* 圆点样式已上移到全局 styles.css（那里有完整的 .dot / .ok / .warn / .err / .unknown）。
   这里原先重复定义了一遍基础规则，而 scoped 版本的注入更晚、特异性同为 0,2,0，
   会把全局的 .dot.unknown 盖掉——导致「无法判定」的灰点落回基础灰，与徽章的灰不同色。
   删掉这份重复，统一从全局取。 */
.quick-verify { border-top: 1px solid #ebeef5; margin-top: 4px; padding-top: 8px; }
.quick-step { display: flex; align-items: center; gap: 8px; padding: 3px 0; }
.raw-json :deep(textarea) { font-family: Consolas, Monaco, monospace; font-size: 12px; }
/* 这里原本有一条 `@media (min-width: 993px) { .sticky-test { position: sticky } }`，
   已随右列布局改动删除：右列不再是滚动容器（见 styles.css 桌面端段——两张卡片
   各滚各的），sticky 失去可吸附的上下文，留着只会让人以为它还在起作用。
   .sticky-test 这个 class 仍在用，在那儿做右列的布局钩子。 */
</style>
