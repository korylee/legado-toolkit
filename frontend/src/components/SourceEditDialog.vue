<!-- 书源编辑视图：工作区「编辑」模式下的完整表单。只管编辑，不管保存——
     保存/另存/脏跟踪都在壳顶栏（SourceWorkspaceDrawer），本组件的每次改动
     经 commitDraft 即时写进 workspace 草稿。 -->
<script setup>
import { ref, computed, watch, nextTick, onMounted, onUnmounted, inject } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { getJobDetail, submitJob } from "../api/jobs";
import {
  canonicalTag, ensureTagMeta, isQualityTag, isStatusTag,
  mergeGroup, sourceTypes, splitSystemUser, statusTags, tagOfType, typeKeyOf,
} from "../utils/tags";
import { useMobile } from "../composables/useMobile";
import { useJobs } from "../composables/useJobs";
import SourceFields from "./SourceFields.vue";
import GrammarList from "./GrammarList.vue";
// 步骤名 → 中文的**唯一**一份（调试页共用），别再在本组件里写第二份
import { STEP_LABELS } from "../utils/steps";
// 调试会话（ux-debug-session 第二期）。key 的拼装在后端 core/debug_keys，
// 前端只发 target + query
import { useDebugSession, STEP_TARGETS } from "../composables/useDebugSession";
// 生成后验证的新鲜度判定（strengthen-src）：分步过期判据的唯一一份
import { ensureRuleSteps, snapshotRuleGroups, staleVerifySteps as computeStaleSteps } from "../utils/verifyFreshness";
import { firstNextDebugAction, nextDebugAction } from "../utils/debugNextAction";
import { SOURCE_WORKSPACE_KEY } from "../composables/useSourceWorkspace";

const workspace = inject(SOURCE_WORKSPACE_KEY);
if (!workspace)
  throw new Error("SourceEditDialog must be mounted under MainLayout");
const isMobile = useMobile();
const { watchJob, frameOf } = useJobs();

const {
  result: testResult,
  channel: debugChannel, target: debugTarget, query: debugQuery,
  host: appHost, env: jvmEnv, envLoading: jvmEnvLoading,
  startRun, confirmPush, setResult, resetDebugState, clearPreflight,
  invalidatePreflight,
} = useDebugSession();

const testStale = ref(false);      // 规则已改动，结果过期

const generationError = ref("");   // 自动生成失败原因，随调试材料一起展示
const systemTags = ref([]);
const manualStatus = ref("");
// 锁定状态归 workspace（它是保存参数），这里的 watch 只维护 manualStatus 显示态
const statusLocked = computed({
  get: () => workspace.state.statusLocked,
  set: (v) => workspace.setStatusLocked(v),
});
//: 用户标签清单归 workspace：保存动作在壳顶栏，标签必须跟草稿是同一份
const userTags = computed(() => workspace.state.userTags);
//: 新建 / 另存时源地址可填（写新地址）；编辑已存源时地址即主键，不许改
const isNew = computed(
  () => workspace.state.saveAsMode || !workspace.state.savedUrl,
);
//: 「真正的新建」。快速生成是**整份替换表单**的入口——另存模式下点它会把
//: 正在另存的那份规则冲掉，所以它只属于真新建（地址输入框可否改仍看 isNew）
const isFresh = computed(
  () => !workspace.state.savedUrl && !workspace.state.saveAsMode,
);

const quickUrl = ref("");
const quickDetailUrl = ref("");
const quickDiscover = ref(false);
const quickProbe = ref(true);
const quickLoading = ref(false);
const quickProgress = ref("");
//: 正在盯的那个生成任务（进度从它的观测帧来，见下面的 watch）
const quickJobId = ref("");
const quickVerify = ref(null);
//: 生成后的那次验证是谁给的（十-5 之后只有**真引擎**两个通道）：标签必须跟着来源走——
//: 跑真引擎的结果挂着「本地调试 · 仅供参考」是句假话（AGENTS #4 那一类）
const quickVerifyFrom = computed(() => {
  const v = quickVerify.value || {};
  if (v.source === "app") return { tag: "App 实测", type: "success" };
  if (v.source === "jvm") return { tag: "本机引擎", type: "primary" };
  return { tag: "", type: "info" };
});
//: 验证没跑成时的那句话（引擎不可用 / 零事件 / 另一个任务在跑）。
//: **不能吞**：源已经生成了，用户要知道「这份验证不是通过，是没跑」
const quickVerifyError = computed(() => String((quickVerify.value || {}).error || ""));

//: strengthen-src：生成后验证是「生成时那一版规则」的结论——改完规则要重验才作数。
//: 快照与分步过期判据都在 utils/verifyFreshness（唯一一份，有 node 测试钉着）；
//: 没改的步骤结论继续可用（分步粒度，比整份作废的 testStale 更细）。
const verifyRuleSnapshot = ref(null);   // {ruleSearch: json, ...} 验证时刻的规则
const refreshedSteps = ref(new Set());  // 点过「重新调试本步」、拿到新结论的步骤

async function captureVerifyRuleSnapshot() {
  // 映射（规则组→步骤）从后端下发（唯一一份在 core/verify.py）；没拿到就不定格
  // 快照——staleVerifySteps 对 null 不标过期，同「没有快照」语义，绝不猜一份本地映射
  try {
    await ensureRuleSteps();
  } catch (e) {
    console.error("[verifyFreshness] 规则组映射加载失败，本次不做过期判定:", e);
    return;
  }
  verifyRuleSnapshot.value = snapshotRuleGroups(form.value);
}

const staleVerifySteps = computed(
  () => computeStaleSteps(verifyRuleSnapshot.value, form.value,
                          [...refreshedSteps.value]));

function rerunStaleStep(stepName) {
  // 调试页里马上会出现这一步的新结论（成功或失败都如实显示），
  // 验证条上的过期标记随之撤下
  const next = new Set(refreshedSteps.value);
  next.add(stepName);
  refreshedSteps.value = next;
  return rerunFromStep(stepName);
}
let quickStop = null;
//: 进度取那条观测流的帧（每个任务一条，与明细弹窗共用）：生成也是分钟级的，
//: 只在收尾才更新的话，按钮旁那句话会一直停在「任务 xxx」不动。
watch(() => {
  const frame = frameOf(quickJobId.value);
  return (frame && frame.job) || null;
}, (job) => {
  if (job && job.total) quickProgress.value = `${job.progress || 0}/${job.total}`;
});
onUnmounted(() => {
  if (quickStop) { quickStop(); quickStop = null; }
});

const activeTab = ref("quick");
const activeRuleTab = ref("search");
const extActive = ref(["request"]);
const rawJsonText = ref("");
const rawJsonError = ref("");
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

function clone(value) {
  return value == null ? value : JSON.parse(JSON.stringify(value));
}

//: 当前表单写回 workspace 草稿。group 在这里合成（类型 + 健康状态 + 质量标签 +
//: 用户标签），壳顶栏保存时拿到的就是完整的一份。updateDraft 对内容做等值判断，
//: 深度 watch 的重复触发到这里是无害空操作
function commitDraft() {
  form.value.bookSourceGroup = mergeGroup(displaySystemTags.value, userTags.value);
  workspace.updateDraft(form.value);
}

/** 源当前的健康状态标签（未锁定时就是校验结果给的）。 */
const currentStatus = computed(
  () => systemTags.value.find((t) => isStatusTag(t)) || "",
);

/** 质量标签（如「规则完整」）：由校验判定，用户改不了，只能看。 */
const qualityTags = computed(
  () => systemTags.value.filter((t) => isQualityTag(t)),
);

// 写回 group 的完整系统标签：类型 + 健康状态 + 质量。
// 界面上三者分散在各自的控件里，不把这串整个渲染成 chips——那会让类型和
// 健康状态各出现两遍。
const displaySystemTags = computed(() => {
  if (!systemTags.value.length && isNew.value) return [];
  // 4 之类的脏值在 Legado 里没有对应类型名，留空并被 filter 丢掉
  const typeTag = tagOfType(form.value.bookSourceType);
  const status = manualStatus.value || currentStatus.value;
  return [typeTag, status, ...qualityTags.value].filter(Boolean);
});

// 锁定时默认锁在当前状态上，省得再选一次；关掉即交回校验结果。
// 播种窗口内不动：manualStatus 由 seedFrom 按 workspace 的锁定态落好
watch(statusLocked, (on) => {
  if (seeding || draftMismatched()) return;
  manualStatus.value = on ? currentStatus.value : "";
  commitDraft();
});

// 用户标签改动同步进草稿（group 要跟着重新合成）；播种窗口内由基线覆盖
watch(userTags, () => {
  if (seeding || draftMismatched()) return;
  commitDraft();
});

function onUserTagsInput(list) {
  // 输入别名立即归一（如「精品排版」→「精排」），别等保存后被后端改名——
  // 那时用户会以为标签被动了手脚。canonicalTag 幂等
  const source = list || [];
  const fixed = source.map((t) => canonicalTag(t));
  const renamed = source.filter((t, i) => fixed[i] !== t);
  workspace.setUserTags(fixed);
  if (renamed.length) {
    ElMessage.info("标签已归一：" + renamed.map((t) => `${t} → ${canonicalTag(t)}`).join("，"));
  }
}

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
  // 且 watch(form) 会一直拒绝刷新快照
  rawDirty = false;
}

//: 外部替换了草稿（壳加载完成 / 保存后的规范化回写 / 另存取消还原）→ 重新播种。
//: 自己写进去的内容（commitDraft → updateDraft → state.source 换引用）形态一致，
//: 被等值判断拦住，不会触发多余的播种
watch(() => workspace.state.source, (next) => {
  const seeded = JSON.stringify({ ...blank(), ...clone(next || {}) });
  if (seeded === JSON.stringify(form.value)) return;
  seedFrom(next);
});

//: 播种窗口：seedFrom 及同一轮 flush 里的关联 watch（表单深度、用户标签、锁定）
//: 都不该算作用户修改——壳加载完成后编辑器才补种是常态，把规范化的表单当成
//: 「有未保存修改」会让守卫凭空拦人
let seeding = false;

//: 表单与草稿是否脱节：外部（壳加载 / 保存回写 / 另存还原）刚替换了
//  workspace.state.source，而镜像 watch 还没把新内容播种进表单。这个窗口里
//  一律不回写草稿——回写的会是旧表单
function draftMismatched() {
  return JSON.stringify({ ...blank(), ...clone(workspace.state.source || {}) })
    !== JSON.stringify(form.value);
}

function seedFrom(next) {
  seeding = true;
  form.value = { ...blank(), ...clone(next || {}) };
  const parsed = splitSystemUser(form.value.bookSourceGroup || "");
  systemTags.value = parsed.system;
  manualStatus.value = workspace.state.statusLocked
    ? (parsed.system.find((t) => isStatusTag(t)) || "")
    : "";
  // 只在空表单（刚打开）时重置子页签落点；保存后的回写、另存取消的还原
  // 都不该把用户正在看的页签掰走
  const fresh = !String(form.value.bookSourceName || "").trim()
    && !String(form.value.bookSourceUrl || "").trim();
  if (fresh) {
    activeRuleTab.value = "search";
    extActive.value = ["request"];
  }
  // 落点页签必须**落在存在的 pane 上**：编辑器常先于壳的详情加载挂载，
  // 那时 savedUrl 还是空、快速生成页签还在；加载完成 isNew 翻假，
  // 若 activeTab 停在 quick 上就会指着一片空白
  const panes = ["basic", "rules", "ext", "raw"];
  if (isFresh.value) panes.unshift("quick");
  if (!panes.includes(activeTab.value)) {
    activeTab.value = isFresh.value ? "quick" : "basic";
  }
  syncRawFromForm();
  // 把播种后的表单（补齐了 blank 默认值、group 重新合成）定为干净基线：
  // updateDraft 的等值比较认 JSON 字符串，外部原文与规范化表单必然不同，
  // 不落基线的话每次打开都会被误判成「有未保存修改」
  commitDraft();
  workspace.settleDraft();
  nextTick(() => { seeding = false; });
}

onMounted(() => seedFrom(workspace.state.source));

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
      workspace.setStatusLocked(false);  // 原始 JSON 不说锁定与否，按「自动」处理
      workspace.setUserTags(parsed.user);
      rawJsonError.value = "";
      rawDirty = false;               // 已应用到表单，文本域与表单一致了
      ElMessage.success("已应用到表单");
      commitDraft();
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

// 单步 → 圆点级别。**顺序必须与 tagTypeOf / 调试页的 dotClass 一致**
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
    // 名称与源地址是保存的前置条件（壳顶栏 save()），两者齐全才算填好
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

// 整体汇总。**三态，不是二态**——只有「全部通过 / 未全部通过」会把
// `unknown`（我们的工具回放不了）算进「未通过」而误报：仅发现模式下
// search 步是预期内的 skip（unknown），每个真实执行的步都通过。
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
  if (summary.level === "fail") return "，先查看失败步骤的诊断。";
  if (summary.level === "unknown") return "，有步骤需要进一步调试。";
  return "";
}

function quickStepAction(step) {
  return nextDebugAction(step, {
    channel: String((quickVerify.value || {}).source || ""),
    stale: staleVerifySteps.value.has(step.name),
  });
}

const quickNextAction = computed(() => firstNextDebugAction(
  (quickVerify.value || {}).steps || [],
  { channel: String((quickVerify.value || {}).source || "") },
));

function openQuickStepAction(step) {
  const action = quickStepAction(step);
  if (!action) return;
  if (action.kind === "rerun") return rerunStaleStep(step.name);
  // workbench / diagnosis 这些动作给的是「去看已有证据」：切到调试页并定位到
  // 该步，跑不跑由那里的「重新调试本步」决定——在这里重跑与文案是两件事
  return workspace.enterDebug(step.name, String(step.url || ""));
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

// 规则一改，上一轮试跑结论就作废了。不标过期的话，用户改了规则
// 还看到绿色的「全部通过」，会据此保存——这正是本次要消除的误导。
// 播种（seedFrom）触发的这轮变化不算用户修改。draftMismatched 守卫**不放在这里**：
// 本 watch 触发的那一刻表单必然带着用户新输入，与草稿必然不同，挂了它等于拦死全部编辑
watch(form, () => {
  if (seeding) return;
  if (testResult.value) testStale.value = true;
  // 预检里的「已连接」= App 里那份与本地规则一致，规则一改这句话就不成立了。
  // 其余三态（连不上 / App 里没有 / 是旧版本）与本地规则无关，留着仍然成立
  invalidatePreflight();
  commitDraft();
  // 用户没动过文本域时保持快照新鲜——否则「应用到表单」会把表单改动全部回滚
  if (!rawDirty) rawJsonText.value = JSON.stringify(form.value, null, 2);
}, { deep: true });

// 源地址变了以后，missing/stale 也不再属于当前 App 源；不能沿用旧标签。
watch(() => String(form.value.bookSourceUrl || "").trim(), (url, oldUrl) => {
  if (oldUrl !== undefined && url !== oldUrl) clearPreflight();
});

//: 「配了发现，但不想让它在 App 里出现」。
//
// `enabledExplore` 由配置推导，这里只表达「配了但想隐藏」这一个例外，默认跟随
// 配置；清洗在壳顶栏保存时做（utils/sourceSave，唯一一份）
const hideExplore = computed({
  get: () => !form.value.enabledExplore,
  set: (v) => { form.value.enabledExplore = !v; },
});

const hasExploreConfig = computed(
  () => !!(String(form.value.exploreUrl || "").trim()
           || Object.keys(form.value.ruleExplore || {}).length));

async function applyGenerated(result) {
  const src = result.source || {};
  form.value = { ...blank(), ...src };
  const parsed = splitSystemUser(src.bookSourceGroup || "");
  systemTags.value = parsed.system;
  manualStatus.value = "";
  workspace.setStatusLocked(false);
  workspace.setUserTags(parsed.user);
  quickVerify.value = result.verify || null;
  generationError.value = "";
  captureVerifyRuleSnapshot();        // 验证是「这一版规则」的结论，快照定格
  refreshedSteps.value = new Set();
  // 自动生成得到的是草稿。生成时的 JVM 验证直接作为调试页的首屏证据，
  // 不再让用户再点一次；没有可用步骤时才补跑一次本机调试。
  const generatedVerify = result.verify && Array.isArray(result.verify.steps)
    && result.verify.steps.length ? result.verify : null;
  resetDebugState();
  commitDraft();   // 先落草稿：首屏证据对应的版次就是当前草稿
  if (generatedVerify) setResult(generatedVerify, workspace.state.draftRevision);
  testStale.value = false;
  debugChannel.value = "jvm";
  debugTarget.value = "search";
  debugQuery.value = String(result.keyword || "").trim();
  expandTestFailures(result.verify || null);
  workspace.enterDebug("search", String(result.keyword || "").trim());
  ElMessage.success("已生成候选源，已打开调试工作台；确认规则后再保存");
  await nextTick();
  if (!testResult.value) await debugRun();
}

function keywordFromSearchUrl(url) {
  try {
    const parsed = new URL(url);
    for (const key of ["q", "keyword", "kw", "wd", "query", "search", "key"]) {
      const value = parsed.searchParams.get(key);
      if (value) return value.trim();
    }
  } catch (e) { /* 让调试入口继续用默认关键词 */ }
  return "";
}

async function openDebugForGenerationFailure(reason) {
  const url = quickUrl.value.trim();
  generationError.value = String(reason || "自动生成未完成");
  if (!url) return;
  // 只写当前草稿，失败分支也不落 SQLite；调试需要源 URL 才能让 App 引擎取页。
  form.value.bookSourceUrl = url;
  if (!String(form.value.bookSourceName || "").trim()) {
    try { form.value.bookSourceName = new URL(url).hostname; } catch (e) { /* ignore */ }
  }
  resetDebugState();
  testStale.value = false;
  debugChannel.value = "jvm";
  debugTarget.value = "search";
  const keyword = keywordFromSearchUrl(url);
  debugQuery.value = keyword;
  commitDraft();
  workspace.enterDebug("search", keyword);
  await nextTick();
  await debugRun();
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
    const r = await submitJob("add", {
      url,
      name: form.value.bookSourceName || "",
      type: typeKey,
      detail_url: quickDetailUrl.value.trim(),
      probe: quickProbe.value,
      discover: quickDiscover.value,
      verify: true,
    });
    quickProgress.value = "任务 " + r.job_id;
    if (quickStop) quickStop();
    quickJobId.value = r.job_id;
    quickStop = watchJob(r.job_id, async (state) => {
      quickLoading.value = false;
      quickStop = null;
      quickJobId.value = "";
      const status = (state && state.job && state.job.status) || "";
      if (status !== "done") {
        quickProgress.value = "";
        // 失败原因在后端的 `error`（连「进程重启没写终态」那种也有，见
        // Store.fail_orphan_jobs）。只显示 status 字面量等于把原因丢了
        const why = (state && state.job && state.job.error) || "";
        const message = why || status || "未知错误";
        ElMessage.error("生成失败，已打开调试工作台：" + message);
        void openDebugForGenerationFailure(message);
        return;
      }
      // 生成类任务的结果体走明细（列表/流都只给轻量行）：用户主动等到的这一次
      // 收尾，拉一次完整结果
      let result = {};
      try {
        const detail = await getJobDetail(r.job_id);
        result = detail.result || {};
      } catch (e) {
        result = {};
      }
      if (!result.ok) {
        quickProgress.value = "";
        const message = result.error || "生成失败";
        ElMessage.error("生成失败，已打开调试工作台：" + message);
        void openDebugForGenerationFailure(message);
        return;
      }
      quickProgress.value = "";
      applyGenerated(result);
    });
  } catch (e) {
    quickLoading.value = false;
    quickProgress.value = "";
    ElMessage.error("提交生成任务失败：" + e.message);
  }
}

// 「重新调试本步」等动作共用的一次启动。本组件不再有自己的调试卡片——
// 结果与证据都长在调试页里，跑完 enterDebug 把人送过去
async function debugRun(rerun = null, rerunStep = "") {
  const entry = rerun || { target: debugTarget.value, query: debugQuery.value };
  testStale.value = false;
  if (debugChannel.value === "jvm") {
    if (jvmEnvLoading.value || !jvmEnv.value?.ok) {
      const first = (jvmEnv.value?.checks || []).find((item) => !item.ok);
      return ElMessage.warning(first
        ? `${first.name}：${first.hint || "环境自检未通过"}`
        : "正在检查本机引擎环境，请稍候");
    }
  } else if (!appHost.value.trim()) {
    return ElMessage.warning("请先填 App 的 IP（App 通知栏里有）");
  }
  // 重跑期间调试页照常显示上一份结果（按钮在途禁用），
  // 新结果到来时由会话滚动对比基线
  const r = await startRun({ source: form.value, ...entry, confirmPush,
                             draftRevision: workspace.state.draftRevision });
  expandTestFailures(r);
  if (r) workspace.enterDebug(rerunStep || "");
}

function rerunFromStep(stepName) {
  const step = ((testResult.value || {}).steps || [])
    .find((s) => s.name === stepName) || {};
  if (stepName === "search") return debugRun(null, stepName);   // 搜索段重跑 = 整链入口
  if (!String(step.url || "").trim()) {
    return ElMessage.warning(
      "上一轮结果里没有这一步的链接。先跑一次完整调试，再重试这一步");
  }
  return debugRun({ target: STEP_TARGETS[stepName], query: step.url }, stepName);
}
</script>

<template>
  <div class="source-editor">
    <div class="editor-toolbar">
      <el-popover placement="bottom-end" :width="360" trigger="click">
        <template #reference>
          <el-button size="small" plain>语法速查</el-button>
        </template>
        <div class="syntax-popover-body"><GrammarList /></div>
      </el-popover>
    </div>
    <el-tabs v-model="activeTab" :tab-position="isMobile ? 'top' : 'left'"
             class="source-tabs">
      <!-- 快速生成是「整份替换表单」的入口：只在新建 / 另存时出现，
           编辑已存源时替换整份规则既危险也无从撤销 -->
      <el-tab-pane v-if="isFresh" name="quick">
        <template #label>
          <span class="tab-label">快速生成<i class="dot" :class="tabDot('quick')"></i></span>
        </template>
        <el-form size="small" label-width="76px">
          <el-form-item label="搜索 URL">
            <el-input v-model="quickUrl"
                      placeholder="https://site/search?q=关键词（必须带真实关键词）" />
          </el-form-item>
          <el-form-item label="类型">
            <!-- **正文规则是按它挑的**：媒体类先看图片、文本类先看文字，同一个正文页上
                 两种东西都可能存在。所以它必须在生成之前就能看见、能改——放在
                 「基本信息」页签里等于默认值静默生效（生成出来才发现是小说源）。
                 与那边是**同一个字段**（`form.bookSourceType`），不另存一份状态。 -->
            <el-radio-group v-model="form.bookSourceType">
              <el-radio-button v-for="t in sourceTypes" :key="t.value" :value="t.value">
                {{ t.tag }}
              </el-radio-button>
            </el-radio-group>
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
          <!-- **谁给的结论要标出来**：十-5 之后这里默认是本机引擎（同一段 App 代码），
               与调试页的结果用的是同一套三态视觉，不标就分不清哪份是什么 -->
          <p class="muted" style="margin: 0 0 8px">
            <el-tag v-if="quickVerifyFrom.tag" size="small" :type="quickVerifyFrom.type">
              {{ quickVerifyFrom.tag }}
            </el-tag>
            <span v-else style="margin-left: 6px">
              生成后的验证跑的是「阅读」App 的真源码（本机引擎）；差在环境——
              登录态要预热、网络出口是本机。要连真机确认请切到「调试」。
            </span>
          </p>
          <el-alert v-if="quickVerifyError" type="warning" :closable="false" show-icon
                    style="margin: 0 0 8px"
                    :title="'这次没验成：' + quickVerifyError" />
          <!-- 与调试页同一套三态渲染：同一个 as_step_dict 产出的数据，
               这里若还用 ok 两态，「pass + 附注」会显示成绿色，两边矛盾 -->
          <div v-for="s in quickVerify.steps" :key="s.name" class="quick-step">
            <el-tag size="small" :type="tagTypeOf(s)">{{ s.name }}</el-tag>
            <span class="muted">{{ s.detail }}</span>
            <!-- strengthen-src：规则改了，这一步的结论就是旧规则的——过期要说，
                 下一步动作（重新调试本步）要给，别让人拿旧结论当现状保存 -->
            <template v-if="staleVerifySteps.has(s.name)">
              <el-tag size="small" type="warning">规则已改 · 结论过期</el-tag>
              <el-button size="small" link type="primary"
                         @click="rerunStaleStep(s.name)">重新调试本步</el-button>
            </template>
            <el-button v-else-if="quickStepAction(s)" size="small" link type="primary"
                       @click="openQuickStepAction(s)">
              {{ quickStepAction(s).label }}
            </el-button>
          </div>
          <p v-if="quickVerify.steps && quickVerify.steps.length"
             class="muted" style="margin: 6px 0 0">
            <b>{{ testSummary(quickVerify.steps).text }}</b>
            <span v-if="quickVerify.steps.some((s) => s.has_notes)">（有疑点，见调试详情）</span>
            <span>{{ summaryHint(testSummary(quickVerify.steps)) }}</span>
            <el-button v-if="quickNextAction" size="small" link type="primary"
                       @click="openQuickStepAction(quickNextAction.step)">
              {{ quickNextAction.action.label }}：{{ STEP_LABELS[quickNextAction.step.name] || quickNextAction.step.name }}
            </el-button>
          </p>
        </div>
      </el-tab-pane>
      <el-tab-pane name="basic">
        <template #label>
          <span class="tab-label">基本信息<i class="dot" :class="tabDot('basic')"></i></span>
        </template>
        <SourceFields v-model:source="form" :user-tags="userTags"
                      :is-new="isNew"
                      @update:user-tags="onUserTagsInput">
           <template #editor-fields>
             <el-form-item label="启用"><el-switch v-model="form.enabled" /></el-form-item>
             <el-form-item label="备注"><el-input v-model="form.bookSourceComment" type="textarea" :rows="2" /></el-form-item>
             <el-form-item label="健康状态"><el-switch v-model="statusLocked" :disabled="!currentStatus" active-text="锁定" inactive-text="自动" inline-prompt /><el-select v-if="statusLocked" v-model="manualStatus" style="width: 170px; margin-left: 10px" @change="commitDraft"><el-option v-for="t in statusTags" :key="t" :label="t" :value="t" /></el-select><span v-else class="muted">跟随校验结果{{ currentStatus ? "：" + currentStatus : "（尚无校验结果）" }}</span></el-form-item>
             <el-form-item label="编码"><el-input v-model="form.charset" placeholder="如 gbk，留空默认 utf-8" /></el-form-item>
             <el-form-item label="排序/权重"><el-input-number v-model="form.customOrder" :min="0" controls-position="right" /><el-input-number v-model="form.weight" :min="0" controls-position="right" style="margin-left: 8px" /></el-form-item>
           </template>
         </SourceFields>
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
              <!-- Legado 的 `ContentRule` 没有 `webView` 字段（`ContentRule.kt:12-25`），
                   写进去 App 根本不读——一个开了没用的开关比没有更糟。`webView` 是
                   **URL 规则**的选项，写在 URL 后面（如 `url,{"webView":true}`，
                   见 AnalyzeUrl.kt:254）。 -->
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
  </div>
</template>

<style scoped>
.source-editor {
  height: 100%;
  display: flex;
  flex-direction: column;
  min-height: 0;
}
.editor-toolbar {
  flex: 0 0 auto;
  display: flex;
  justify-content: flex-end;
  padding: 6px 12px 0;
}
.source-tabs {
  flex: 1 1 auto;
  min-height: 0;
  padding: 0 12px 12px;
}
/* el-tabs 的内部结构不是本组件渲染的，scoped 直写不生效，走 :deep()
   （el-tabs 在本组件子树内，data-v 在根上，:deep 能命中） */
.source-tabs :deep(.el-tabs__header) { flex: 0 0 auto; }
.source-tabs :deep(.el-tabs__content) {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  overflow-x: hidden;
}
.tab-label { display: inline-flex; align-items: center; gap: 6px; }
.quick-verify { border-top: 1px solid #ebeef5; margin-top: 4px; padding-top: 8px; }
.quick-step { display: flex; align-items: center; gap: 8px; padding: 3px 0; }
.raw-json :deep(textarea) { font-family: Consolas, Monaco, monospace; font-size: 12px; }
</style>
