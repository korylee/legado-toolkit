<script setup>
import { ref, reactive, computed, nextTick, watch, onMounted, onUnmounted } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { Search, Plus, Upload, Download, Delete, Filter, Refresh, Monitor, MagicStick,
         Operation, Close } from "@element-plus/icons-vue";
import { listSources, listSourceUrls, listGroups, patchTags, deleteSources, listTags, getStats } from "../api/sources";
import { api, subscribeJob } from "../api/client";
import { jvmRun } from "../api/jvm.js";
import { jobFailReason } from "../utils/jobs";
import { ensureTagMeta } from "../utils/tags";
import { HEALTH_OPTIONS, describeChanges, engineLabel, healthLabel,
         starBasisLabel } from "../utils/health";
// 一行的显示事实（健康/深度/标签/逐段结论）都在 utils/sourceRow.js——移动卡片与
// 桌面表格共用同一份判断，这里只负责渲染
import { depthClass, depthText, hasAnyTag, healthCell, jvmStateLabel, jvmSteps,
         lockedStatus, tagCellsOf, triToInt, typeLabel, urlKey } from "../utils/sourceRow";
import { useMobile } from "../composables/useMobile";
import SourceEditDialog from "../components/SourceEditDialog.vue";
import SourceFilterForm from "../components/SourceFilterForm.vue";
import SourceCard from "../components/SourceCard.vue";
import TrashDrawer from "../components/TrashDrawer.vue";
import TidyDrawer from "../components/TidyDrawer.vue";
import ExportDrawer from "../components/ExportDrawer.vue";
import ImportDialog from "../components/ImportDialog.vue";
import GroupManagerDrawer from "../components/GroupManagerDrawer.vue";
import JobsDrawer from "../components/JobsDrawer.vue";
import CheckJvmForm from "../components/CheckJvmForm.vue";

const isMobile = useMobile();
const loading = ref(false);
const rows = ref([]);
const total = ref(0);
const groups = ref([]);
const selected = ref([]);
const tableRef = ref(null);

const dlgVisible = ref(false);
const dlgUrl = ref("");
const trashVisible = ref(false);
const tidyVisible = ref(false);
const exportVisible = ref(false);
const importVisible = ref(false);
const filterVisible = ref(false);
//: 移动端工具行是否展开。**不持久化**：它是一次性动作（点开→用一个→收起），
//: 不是偏好；记住了反而会在某次刷新后莫名挡住列表
const toolsOpen = ref(false);
const batchTags = ref([]);
const tags = ref([]);
const tagManagerVisible = ref(false);
const checking = ref(false);
let stopCheck = null;

// 「哪些源正在校验」。**不能用一个布尔代替**：点了第 7 行的校验，工具栏、批量条
// 和所有行的按钮一起转圈，用户会以为"选中的那些正在校验"，实际只有第 7 行在跑——
// 反馈没有指向性。checking 仍然要有（不能并发提交是真的），但它只管"有没有任务"，
// "测的是哪些"由这两个状态回答。
const checkingUrls = ref(new Set());
const checkingAll = ref(false);
const checkJobId = ref("");
const checkTotal = ref(0);
//: 后端**每完成一条**就更新一次的已完成数（见 checker.run 的 on_progress）
const checkProgress = ref(0);

//: 某一行是否正在校验。全量时所有行都在测，逐行订阅时只有命中的那些
function isRowChecking(url) {
  if (!checking.value) return false;
  if (checkingAll.value) return true;
  return checkingUrls.value.has(url);
}

/** 单条校验：**卡片与表格行共用这一处**（手机卡片是 emit 上来的、桌面是行内按钮）。
 *  不弹框，直接用全局设置——单条本来就没什么可调的。 */
function checkOne(row) { checkSources([row.source_url]); }

//: 总数优先用后端报的（提交列表与"全量"的实际条数可能不同，比如回收站/禁用的源）
const checkStatusText = computed(() => {
  // 本机引擎跑批：一次同步调用、没有逐条进度（后端跑完才返回），所以只报状态不报分数
  // ——编一个假进度比不报更糟（用户会盯着一个不动的数字）
  if (jvmRunning.value) return "正在用本机引擎跑批：" + jvmRunScope.value;
  const n = checkTotal.value || (checkingAll.value ? 0 : checkingUrls.value.size);
  // 全量校验是十几分钟的长任务，**带上跑动中的计数**：只有「正在校验全部 3861 条」
  // 这句时，跑与没跑、跑到哪了在界面上完全看不出来（原话是"进度没更新"）。
  // 逐条校验（选中 N 条）不加分数——那种几秒就完，分数只是噪音
  if (checkingAll.value) {
    if (!n) return "正在校验全部源";
    return "正在校验全部 " + n + " 条 · 已完成 " + Math.min(checkProgress.value, n);
  }
  return "正在校验 " + n + " 条";
});

//: 批量校验的**范围**（一台引擎之后，这里成了弹框里唯一的选择）：
//:   selected（勾选的那几条）/ filtered（当前筛选命中的）/ all（全部在用源）
//: 勾选优先于筛选：勾是明确意图，筛选是「这一屏里的」。
const checkScope = ref("all");

// 批量校验的确认弹框。**选项就长在这个框里**。
//
// 以前「校验参数」是工具栏上一个独立按钮，而「全量校验」另弹一个只问"确定吗"的
// 空确认框：选项和动作分在两处，那个确认框纯粹是减速带。合起来之后工具栏少一个
// 按钮，确认框变成有用的一步，选项也不再是"长在全量校验旁边、却影响行按钮"的隐形状态
// ——它只在批量动作的弹框里出现，一一对应。
//
// 单条校验（表格行 / 卡片图标）**不弹框**，直接用全局设置：单条本来就没什么可调的。
const checkDialog = ref(false);
//: 弹框确认后要校验的 URL 列表；空数组 = 不按勾选
const pendingCheckUrls = ref([]);

// 打开时才去拉全局设置（拿到的是最新全局值），所以必须等容器渲染完再调
// **表单要刷**：el-dialog 不销毁内容，上一次的参数会留在 DOM 里
watch(checkDialog, (v) => {
  if (!v) return;
  nextTick(() => {
    if (jvmFormRef.value) jvmFormRef.value.reload();
  });
});

//: 结果条上那一行字。**行内与 tooltip 共用同一份**——各写一遍必然漂成两句不同的话。
//: 顺序按「用户最关心的先看」：本次跑了多少 → 变化 → 首次有结论 → 过期提醒 → 异常。
const checkTipText = computed(() => {
  const c = checkResult.value;
  if (!c) return "";
  const bits = ["本次校验 " + c.checked + " 条：新校验 " + c.fetched
                + "、复用缓存 " + c.cached];
  if (c.changes.length) bits.push(c.changes.join("、"));
  else if (!c.firstChecked) bits.push("无状态变化");
  if (c.firstChecked) bits.push(c.firstChecked + " 条首次有结论");
  if (c.stale) bits.push("排序/筛选项可能已过期");
  if (c.saveFailures) bits.push(c.saveFailures + " 条结果没能写入管理库，列表状态不会更新");
  if (c.hitDowngrades) {
    bits.push(c.hitDowngrades + " 个源命中判定降级（规则无法回放，已按「命中」处理）");
  }
  return bits.join(" · ");
});

const checkDialogTitle = computed(() => (
  checkScope.value === "selected" ? "校验勾选的源"
    : checkScope.value === "filtered" ? "校验当前筛选" : "全量校验"));

//: 校验只有**一台引擎**（本机引擎 = 在 JVM 里跑「阅读」App 的真源码）：
//: 本地回放那条路 2026-09-20 撤了（判定口径见 TODO §一点九 / lessons §七十二），
//: 所以弹框里不再有引擎选择——**可调的是「跑哪些」**。
const jvmReady = ref(false);
const jvmFormRef = ref(null);

//: 当前有没有筛选条件（决定「当前筛选」这个范围出不出现）。
//: 与列表查询同一个对象，所以不可能会漂。
const hasFilter = computed(() => {
  const q = query;
  return !!(q.q || q.health || q.group || q.tag
            || (q.type !== null && q.type !== ""));
});
//: 当前筛选命中的条数（分页总数就是它）
const filterTotal = computed(() => total.value || 0);

//: 本次要跑多少条（给表单与提示语共用一份口径）。
const scopeCount = computed(() => {
  if (checkScope.value === "selected") return pendingCheckUrls.value.length;
  if (checkScope.value === "filtered") return filterTotal.value;
  return stats.value ? stats.value.sources : 0;
});


const checkDialogHint = computed(() => {
  if (checkScope.value === "selected") {
    return "将校验勾选的 " + pendingCheckUrls.value.length + " 条源。";
  }
  if (checkScope.value === "filtered") {
    return "将校验当前筛选命中的 " + filterTotal.value + " 条源（不受分页限制）。";
  }
  return "将校验全部在用书源。";
});

/** 打开批量校验的弹框。批量入口都走这里，单条不走。
 *
 * **弹框里不再有"范围"这个选择**：点开它的那一刻跑什么就已经定了——
 * 勾选了就是那几条，有筛选就是筛选命中的那批，都没有才是全量。用户知道自己选了
 * 哪些，不需要在弹框里再选一遍；而"全部在用源"更不该摆在勾选过的用户面前当选项
 * （全量一次十几分钟，站点还按 IP 认人——能少跑就少跑）。
 *
 * 于是范围只跟着入口走，`urls` 就是入口带来的范围。
 */
function openCheckDialog(urls = []) {
  pendingCheckUrls.value = urls;
  jvmReady.value = false;
  checkScope.value = urls.length ? "selected"
    : (hasFilter.value && filterTotal.value > 0 ? "filtered" : "all");
  checkDialog.value = true;
}

function startPendingCheck() {
  checkDialog.value = false;
  runJvmBatch();
}

//: 本机引擎跑批：一次同步调用（后端起 appservice 子进程，跑完才返回），没有逐条进度。
//: **不做成 job**：它是「一条命令跑完一批」，与本地那套 SSE 进度不是一回事；
//: 界面上只给一句「正在跑」+ 完成的落库条数。
const jvmRunning = ref(false);
//: 跑动中的状态条要写清**这次跑的是哪些、多少条**——同一句话糊过去的话，
//: 「我明明只选了 20 条」这种疑问在跑动期间无法自证
const jvmRunScope = ref("");
//: 跑批任务的 SSE 句柄（关掉订阅用；任务本身在后端跑，关页面也不会丢）
let stopJvm = null;

async function runJvmBatch() {
  if (jvmRunning.value) return ElMessage.warning("已有本机引擎跑批在运行");
  if (checking.value) return ElMessage.warning("已有校验任务在运行");
  const scope = checkScope.value;
  // 三种范围各自的入参：勾选给 urls、筛选给 filter、全量什么都不给
  // （「条数上限」只对全量生效——范围由勾选/筛选决定，否则会出现看不出来的截断）
  // 本次参数（关键词 / 超时 / 并发 / 挡位 / 条数上限）跟着请求走、**不写回设置**：
  // 形状同本地校验的 `check: {...}`，表单那边已经把它们做成可编辑的
  const params = jvmFormRef.value ? jvmFormRef.value.params() : {};
  const payload = scope === "selected" ? { urls: pendingCheckUrls.value, params }
    : scope === "filtered" ? { filter: { q: query.q, type: query.type, health: query.health,
                                        group: query.group, tag: query.tag }, params }
      : { params };
  jvmRunning.value = true;
  try {
    const r = await jvmRun(payload);
    if (!r.started) {
      // 没起来的几种原因要分开说：另一个 JVM 任务在跑（reason）、自检没过（selftest）、
      // 选中的源一条都没匹配上 / 筛选没命中（reason）——都说成「自检未通过」会把原因指反
      ElMessage.warning(r.reason || "环境自检未通过，不能跑批");
      jvmRunning.value = false;
      return;
    }
    // 任务的**条数**用预检算好的那个（已经过了「条数上限」）：界面上说"全量"而实际
    // 跑了 3 条，是设置页时代最难发现的一处截断
    jvmRunScope.value = (scope === "selected" ? "勾选的" : scope === "filtered" ? "当前筛选的" : "全部在用源")
      + "（" + (r.count || 0) + " 条）";
    // 跑批是分钟级的：**提交任务 + 订阅**（与本地校验同一条链路）。原来是一个挂到
    // 跑完的长请求——关掉页面/刷新就白等；结论照落库，但界面不知道它跑完了
    if (stopJvm) stopJvm();
    stopJvm = subscribeJob(
      r.job_id,
      // 跑批没有逐条进度（一次 Gradle 调用跑一批），**不编假进度**：只报状态
      () => {},
      async (data) => {
        jvmRunning.value = false;
        let res = {};
        try { res = JSON.parse((data && data.result_json) || "{}"); } catch (e) { /* 非 JSON 就当空 */ }
        if (data.status === "done") {
          if (res.ok === false) {
            ElMessage.warning(res.reason || "跑批没跑成");
          } else {
            // 结论写进 checks（健康档位 / 星级 / 深度）与 meta——跑完必须重新拉列表
            ElMessage.success("跑批完成：" + (res.count || 0) + " 条结论已入库"
                              + (res.checks ? "（健康档位 " + res.checks + " 条）" : ""));
          }
          await load();
        } else if (data.status === "failed") {
          ElMessage.error("跑批失败：" + (res.error || "看任务详情"));
        } else if (data.status === "cancelled") {
          ElMessage.info("跑批已取消");
        }
      });
  } catch (e) {
    jvmRunning.value = false;
    ElMessage.error("跑批失败：" + (e?.message || e));
  }
}

// 统计条（替代已删掉的「诊断」页）与「任务」抽屉
const stats = ref(null);
const jobsVisible = ref(false);
const jobBadge = ref(0);
const jobsRef = ref(null);
//: 结果条上点「查看」时，要让抽屉直接展开哪一条任务
const focusJobId = ref("");

/** 结果条上的「查看」：打开任务抽屉，并展开刚跑完的那条（明细在里面） */
function openJobDetail() {
  focusJobId.value = (checkResult.value && checkResult.value.jobId) || "";
  jobsVisible.value = true;
}

const query = reactive({
  q: "", type: null, health: "", group: "", tag: "",
  order: "-verified", limit: 50, offset: 0,
});

// stats.health 的键是 str(health)：没有校验记录时 health 为 NULL，键就是字符串 "None"
// （后端**筛选**用的取值才是 "none"，别拿它当键——读错这一格永远是 0）
const healthCount = (key) => {
  const h = stats.value && stats.value.health;
  return (h && h[key]) || 0;
};

// 统计条上**只渲染有数的档**：0 条的状态在这里没有信息量（「这档存在、当前没有」），
// 却把这一行越摊越长——用户的原话是「太多太杂」。空档位在**筛选下拉里照旧全给**
// （那是能力清单，不是现状概览），所以按档下钻、确认「确实一条都没有」仍然做得到。
const activeHealthOptions = computed(
  () => HEALTH_OPTIONS.filter((h) => healthCount(h.value) > 0));

async function loadStats() {
  try { stats.value = await getStats(); } catch (e) { stats.value = null; }
}

//: 选中的是 **URL 字符串数组**，不是行对象。
//: 「选中全部 N 条筛选结果」拿回来的那批源根本不在当前页、也不在 DOM 里，
//: 行对象既拿不到也用不上；统一成 URL 之后，批量删除/加标签/校验/导出都只认它，
//: 判断某一行是否选中走这个 Set
const selectedUrls = computed(() => new Set(selected.value));
const filterCount = computed(() => {
  const f = query;
  return [f.q, f.type !== null && f.type !== "", f.health, f.group, f.tag].filter(Boolean).length;
});

async function load() {
  loading.value = true;
  try {
    const res = await listSources(query);
    rows.value = res.items;
    total.value = res.total;
  } catch (e) {
    ElMessage.error("加载失败: " + e.message);
  } finally {
    loading.value = false;
  }
  // 统计条和列表同屏，跟着一起刷，免得出现「条上说 12 条失效、列表却不是」的错位
  loadStats();
  // 翻页/刷新后把 selected 的状态同步回表格勾选（表格只渲染当前页，翻页会忘掉）
  await nextTick();
  syncTableSelection();
}

//: 上一次校验的结果摘要。**做成持久条而不是 toast**：校验是长时任务，
//: 「新校验几条 / 复用几条 / 几条变了」是要看第二眼的东西，而 ElMessage 三秒就没了——
//: 错过之后列表莫名其妙不一样了，用户没有任何线索。
//: 值为 null 表示没有可展示的结果（没校验过 / 任务状态获取失败）。
//: `stale` 是就地回填的副产品：行没动，所以排序和筛选项都可能已经不再成立。
const checkResult = ref(null);

/**
 * 用校验结果**就地回填**列表，不是整表重拉。
 *
 * 原来这里是 `await load()`：只校验了 7 条，整张表却重载——滚动位置会跳、
 * 正在看的行会移位。`result_json.items` 已带每条源的新状态，替换即可。
 *
 * 两种情况返回 false，调用方退回全量刷新：
 *   1. items 被后端截断（全量校验 3850 条，而 payload 是 `items[:500]`）
 *   2. 一条都没匹配上——URL 口径不一致时就是这个表现，宁可重拉也别静默不做事
 *
 * **回填不重算筛选**：筛了「可用」的列表里会留着一行刚变成失效的源。这是有意的
 * ——替用户决定「悄悄把它移走」比让他看见更糟。所以另给一条提示条，把选择权还回去。
 */
function applyCheckResults(resultJson) {
  const items = (resultJson && resultJson.items) || [];
  const checked = Number((resultJson && resultJson.checked) || 0);
  if (!items.length || checked > items.length) return false;
  const byUrl = new Map(items.map((it) => [urlKey(it.url), it]));
  let hit = 0;
  rows.value.forEach((row) => {
    const it = byUrl.get(urlKey(row.source_url));
    if (!it) return;
    row.health = it.health;
    row.stars = it.stars;
    row.star_basis = it.star_basis;
    row.toc_complete = triToInt(it.toc_complete);
    row.content_ok = triToInt(it.content_ok);
    row.search_hit = it.search_hit;
    row.checked_at = it.checked_at;
    hit += 1;
  });
  return hit > 0;
}

// 点健康度 chip：再点一次同一个就取消筛选，切回全部
function onHealthChip(value) {
  query.health = query.health === value ? "" : value;
  search();   // 换筛选条件必须回到第一页，否则会停在越界的 offset 上
}

function search() { query.offset = 0; load(); }
function reset() {
  Object.assign(query, { q: "", type: null, health: "", group: "", tag: "", order: "-verified", offset: 0 });
  load();
}
function onPage(p) { query.offset = (p - 1) * query.limit; load(); }

//: 程序化改表格勾选时置位。
//:
//: `toggleRowSelection` / `clearSelection` 都会触发 `selection-change`，而那个事件
//: 处理**拿"当前表格状态"重算 selected** —— 不挡掉的话，同步过程会被自己的事件
//: 反过来覆盖：点「选中全部 N 条」后表格一格都不勾、翻页回来也全丢，而批量条上的
//: 数字还停在 N（selected 里有 URL，表格里没有）。
let syncingSelection = false;

function clearSelection() {
  selected.value = [];
  if (!tableRef.value) return;
  syncingSelection = true;
  try { tableRef.value.clearSelection(); } finally { syncingSelection = false; }
}
function isSelected(row) { return selectedUrls.value.has(row.source_url); }
function toggleCard(row) {
  const key = row.source_url;
  selected.value = isSelected(row)
    ? selected.value.filter((u) => u !== key)
    : [...selected.value, key];
}

//: 表格勾选 → selected。**当前页的行以表格为准，不在当前页的保持原样**。
//: 写成 `selected = v.map((r) => r.source_url)` 的话，「选中全部 800 条」之后
//: 取消勾选一行，会变成「已选 49 条」——其余 750 条无声消失，界面上看不出丢过东西
function onTableSelect(picked) {
  if (syncingSelection) return;      // 这是程序设的，不是用户勾的
  const onPage = new Set(rows.value.map((r) => r.source_url));
  const kept = selected.value.filter((u) => !onPage.has(u));
  selected.value = [...kept, ...picked.map((r) => r.source_url)];
}

//: 把 selected 的状态同步回表格勾选。表格只渲染当前页，翻页后它自己会忘掉勾选
//: 状态——不同步的话，翻回来看见的是「批量条说选了 800 条、表格上一个都没勾」
function syncTableSelection() {
  const t = tableRef.value;
  if (!t || !selected.value.length) return;   // 常态（没选任何行）直接跳过
  syncingSelection = true;
  try {
    rows.value.forEach((row) => {
      t.toggleRowSelection(row, selectedUrls.value.has(row.source_url));
    });
  } finally {
    syncingSelection = false;
  }
}

//: 「选中全部 N 条筛选结果」，N 取 total（后端与列表同一套筛选口径）。
//: 传的是**显式 URL 列表**而不是筛选条件：批量条上写的是「已选 N 条」，用户的心智是
//: "我选中了这 N 条"；反过来让删除接口吃筛选条件的话，将来 _where 的语义一变，
//: 这个按钮的含义会跟着静默改变——而它的下一步是不可逆操作
async function selectAllFiltered() {
  try {
    const res = await listSourceUrls(query);
    selected.value = res.urls;
    syncTableSelection();
    ElMessage.success("已选中全部 " + selected.value.length + " 条");
  } catch (e) {
    ElMessage.error("获取全部筛选结果失败: " + e.message);
  }
}

//: 移入回收站的确认框，返回**原因**（取消时抛出）。
//:
//: 原因走的是可选输入：不填也能删，但填了会写进 `data/backups/deleted.jsonl`
//: 的那条记录 —— 那是「为什么删」**唯一**的存放处（库里没有这一列），而之前界面
//: 从不传它，于是从界面删的全部是空原因，备份里那份审计信息形同虚设。
//:
//: 单条与批量共用这一处：各写一遍的话，同一件事会慢慢变成两种说法。
async function askTrashReason(n) {
  const res = await ElMessageBox.prompt(
    "将把 " + n + " 条源移入回收站。不会再导出到 App，可随时恢复。",
    "移入回收站",
    {
      type: "warning",
      confirmButtonText: "移入回收站",
      cancelButtonText: "取消",
      inputPlaceholder: "可选：为什么删（会记进备份，如「非书源：影视站」）",
      inputValue: "",
    });
  return String(res.value || "").trim();
}

/** 行内删除一条。走的是**软删除**（与批量同一个接口、同一套后果）。 */
async function removeOne(row) {
  try {
    const reason = await askTrashReason(1);
    const res = await deleteSources([row.source_url], reason);
    ElMessage.success("已移入回收站 " + res.deleted + " 条");
    // 这一行如果被勾选过，得把它从 selected 里摘掉：否则批量条上的「已选 N 条」
    // 会算上一条已经不在列表里的源，后续批量动作还会拿它去发请求
    selected.value = selected.value.filter((u) => u !== row.source_url);
    load();
  } catch (e) { /* 取消 */ }
}

async function removeSelected() {
  if (!selected.value.length) return ElMessage.warning("先勾选源");
  try {
    const reason = await askTrashReason(selected.value.length);
    const res = await deleteSources(selected.value, reason);
    ElMessage.success("已移入回收站 " + res.deleted + " 条");
    clearSelection();
    load();
  } catch (e) { /* 取消 */ }
}

async function applyBatchTags(mode) {
  if (!selected.value.length) return ElMessage.warning("先勾选源");
  if (!batchTags.value.length) return ElMessage.warning("先选择或输入标签");
  const urls = selected.value;
  try {
    await patchTags(
      urls,
      mode === "add" ? batchTags.value : [],
      mode === "remove" ? batchTags.value : [],
    );
    ElMessage.success((mode === "add" ? "已加标签 " : "已移除标签 ") + urls.length + " 条");
    batchTags.value = [];
    clearSelection();
    load();
    tags.value = await listTags();
  } catch (e) {
    ElMessage.error(e.message);
  }
}

/**
 * 把校验结果解析成结果条要的那几个数字。**不再弹 toast**——见 `checkResult` 的注释。
 *
 * **必须报出「复用了几条」**：有效期内的缓存不会重新请求，所以要是不说，
 * 「点校验 → 完成」和「一条请求都没发」在界面上长得一模一样。
 *
 * 「首次有结论」与「变成 X」**分开**：库里绝大多数源从未校验过，第一次全量之后
 * 「新增可用 2000 条」不是「比上次好」。混在一起这个数字就失去意义。
 *
 * 口径与任务抽屉里的摘要同源（都读 result_json），不另算一份。
 */
function parseCheckResult(resultJson) {
  let r = null;
  try { r = resultJson ? JSON.parse(resultJson) : null; } catch (e) { return null; }
  if (!r || typeof r.checked !== "number") return null;
  const cached = r.cached || 0;
  const t = r.transitions || {};
  return {
    checked: r.checked,
    cached,
    fetched: typeof r.fetched === "number" ? r.fetched : r.checked - cached,
    firstChecked: t.first_checked || 0,
    changes: describeChanges(t.changed),
    // 写库失败要单独报：结果没落库时列表状态不会变，而列表上完全看不出来
    saveFailures: r.save_failures || 0,
    hitDowngrades: r.hit_downgrades || 0,
  };
}

async function checkSources(urls = []) {
  if (checking.value) return ElMessage.warning("已有校验任务在运行");
  // **单条走本机引擎**（十-2）：结论按 checks 口径落库、结果体与本地那条**同形状**
  // （`backend/api/check_summary.py` 一处给形状，下面这段订阅/回填/摘要两条路共用）。
  // 多条与全量暂时仍走本地引擎——本地执行体整体退役是十-4 的事。
  const viaEngine = urls.length === 1;
  if (viaEngine && jvmRunning.value) return ElMessage.warning("已有本机引擎跑批在运行");
  checking.value = true;
  // 提交时就要把"测哪些"记下来：等 SSE 回来才更新的话，点完到第一次事件之间
  // 界面上什么都不会变
  checkingAll.value = !urls.length;
  checkingUrls.value = new Set(urls);
  checkJobId.value = "";
  checkTotal.value = 0;
  checkProgress.value = 0;
  try {
    // refresh_cache：忽略有效期内的缓存，全部重新请求。
    // 校验参数（并发/超时/深度/代理等）不再写死在这里——不传就由后端取全局设置，
    // 只有「本次覆盖」的那几项才进 payload
    // 参数全走全局设置：单条校验本来就不弹框、也没有「本次覆盖」这一层
    // （覆盖项原来长在全量弹框里，却会顺手影响行按钮——那一层随弹框里的本地分支一起撤了）
    let jobId = "";
    if (viaEngine) {
      const r = await jvmRun({ urls });
      if (!r.started) {
        // 引擎没起来的几种原因分开说（reason = 忙 / 空范围，selftest = 环境没过）——
        // 都说成「自检未通过」会把原因指反（与跑批那条同一套说法）
        checking.value = false;
        checkingUrls.value = new Set();
        ElMessage.warning(r.reason
          || "本机引擎不可用：先在「设置 → JVM 校验」里填 App 源码目录并自检");
        return;
      }
      jobId = r.job_id;
      checkTotal.value = r.count || urls.length;
    } else {
      const r = await api.post("/jobs", { kind: "check", payload: { urls } });
      jobId = r.job_id;
    }
    checkJobId.value = jobId;
    // **不弹「已提交」的 toast**：上面那条状态条已经在说「正在校验 N 条」了，
    // 再来一条浮层只是噪音——而且它挡在统计条旁边，反而盖住了真正的进度
    if (stopCheck) stopCheck();
    stopCheck = subscribeJob(
      jobId,
      // 每帧带整个 job（status/total/progress）。**progress 是每完成一条就更新一次**
      // 的（checker.run 的 on_progress），所以状态条上那个计数是跑动中的，不是跳变的
      (data) => {
        if (!data) return;
        if (data.total) checkTotal.value = data.total;
        if (typeof data.progress === "number") checkProgress.value = data.progress;
      },
      async (data) => {
        resetCheckState();
        if (data.status === "done") {
          const summary = parseCheckResult(data.result_json);
          // 就地回填优先：整表重拉会让滚动位置跳、正在看的行移位。
          // 回填不了（结果被截断 / 一条都没匹配上）才退回全量刷新
          const backfilled = applyCheckResults(data.result_json);
          if (!backfilled) await load();
          if (summary) {
            // 行没动 → 排序和筛选项都可能已经不再成立。**只有就地回填时才谈得上
            // 过期**：退回全量刷新的话列表就是刚查的，没有过期问题。
            // 排序过期只在按星级排时才成立（别的排序键不会因校验而变）
            summary.stale = backfilled
              && (filterCount.value > 0 || /verified/.test(query.order));
            // **把 job_id 存进摘要**：resetCheckState() 刚把 checkJobId 清空了，
            // 不存的话结果条上的「查看」点开抽屉不知道要看哪一条
            summary.jobId = jobId;
            checkResult.value = summary;
          }
          try { tags.value = await listTags(); } catch (e) { /* 忽略 */ }
        } else if (data.status === "cancelled") {
          ElMessage.info("校验已取消");
        } else if (data.status === "unknown") {
          // 兜底出口：SSE 重连到上限仍没拿到终态。**话要说准**——不能说
          // 「任务失败」，任务很可能早就跑完了，只是我们没收到
          ElMessage.error(data.error || "任务状态获取失败，请刷新页面");
        } else {
          ElMessage.error("校验任务失败：" + (jobFailReason(data.result_json)
                                            || data.status || "unknown"));
        }
        // 任务收尾后让抽屉那份列表/徽标跟上
        jobsRef.value?.refresh();
      },
    );
    // 新任务立刻反映到「任务」按钮的徽标上
    jobsRef.value?.refresh();
  } catch (e) {
    resetCheckState();
    ElMessage.error("提交校验失败: " + e.message);
  }
}

function resetCheckState() {
  checking.value = false;
  checkingAll.value = false;
  checkingUrls.value = new Set();
  checkJobId.value = "";
  checkTotal.value = 0;
  checkProgress.value = 0;
  stopCheck = null;
}

//: 取消正在跑的校验。后端 runner.cancel 会 cancel 掉 asyncio task，
//: 任务状态转 cancelled 后由上面的 SSE 收尾回调统一复位
async function cancelCheck() {
  if (!checkJobId.value) return;
  try {
    await api.post("/jobs/" + checkJobId.value + "/cancel", {});
  } catch (e) {
    ElMessage.error("取消失败: " + e.message);
  }
}


async function onTagsChanged() {
  await load();
  try { tags.value = await listTags(); } catch (e) { /* 忽略 */ }
}

function openNew() { dlgUrl.value = ""; dlgVisible.value = true; }
function openEdit(row) { dlgUrl.value = row.source_url; dlgVisible.value = true; }
async function onSaved() {
  dlgVisible.value = false;
  await load();
  try { tags.value = await listTags(); } catch (e) { /* 忽略 */ }
}

onMounted(async () => {
  // 拆分系统/用户标签（utils/sourceRow 的质量标签、用户标签）用的是后端下发的枚举，
  // 必须等它到位再拉数据，否则首屏会把系统标签当成用户标签渲染
  try { await ensureTagMeta(); } catch (e) {
    ElMessage.warning("系统标签枚举加载失败，标签归类可能不准: " + e.message);
  }
  load();
  try { groups.value = await listGroups(); } catch (e) { /* 忽略 */ }
  try { tags.value = await listTags(); } catch (e) { /* 忽略 */ }
});

onUnmounted(() => {
  if (stopCheck) stopCheck();
  if (stopJvm) stopJvm();   // 只关订阅：任务在后端继续跑完并落库
});
</script>

<template>
  <div class="page page-flex">
    <!-- 统计条（替代已删掉的「诊断」页）：点 chip 直接下钻筛选，再点一次取消 -->
    <div class="stats-bar">
      <div class="chips">
        <button type="button" class="chip" :class="{ active: !filterCount }" @click="reset">
          源 <b>{{ stats ? stats.sources : "—" }}</b>
        </button>
        <button v-for="h in activeHealthOptions" :key="h.value" type="button" class="chip"
                :class="{ active: query.health === h.value }" @click="onHealthChip(h.value)">
          {{ h.label }} <b>{{ healthCount(h.value) }}</b>
        </button>
        <!-- 「未校验」= 没有校验记录（health IS NULL），后端 _where 已支持值 "none"。
             与上面同一条规矩：0 条时不占位置（新导入一批源之后它会自己冒出来） -->
        <button v-if="healthCount('None') > 0" type="button" class="chip"
                :class="{ active: query.health === 'none' }" @click="onHealthChip('none')">
          未校验 <b>{{ healthCount("None") }}</b>
        </button>
      </div>
      <span class="grow" />
      <!-- 校验中的状态放在统计条：工具栏那行已经会换行，再加控制项只会更挤。
           这里本来就有一块撑开的空档 -->
      <template v-if="checking">
        <span class="muted">{{ checkStatusText }}</span>
        <el-button link size="small" type="warning" @click="cancelCheck">取消</el-button>
      </template>
      <el-badge :value="jobBadge" :hidden="!jobBadge" type="primary">
        <el-button size="small" :icon="Monitor" @click="jobsVisible = true">任务</el-button>
      </el-badge>
    </div>

    <!-- 桌面：筛选一行、操作一行。显式分行，不靠 flex-wrap 决定断点 -->
    <div class="bar bar-rows page-toolbar desktop-only" v-if="!isMobile">
      <div class="bar-row">
        <!-- 六个筛选字段 + 查询/重置：与移动端底部抽屉**同一个组件**（那一处是 sheet） -->
        <SourceFilterForm variant="bar" :query="query" :groups="groups" :tags="tags"
                          @search="search" @reset="reset" />
      </div>

      <div class="bar-row">
        <el-button size="small" :icon="Plus" @click="openNew">新建源</el-button>
        <!-- 动作行这一个是**全量口径**（全部 / 当前筛选），文案固定不变脸；
             「校验这批勾选的」住在下面的批量条里——那里才有勾选这个前提。
             原来它按 selected 变脸并偷读 selected，一个按钮担两种范围，
             勾选状态一旦不在视野里（比如手机把它收进菜单）就说不清在测哪批 -->
        <el-button size="small" :icon="Refresh" :disabled="checking || jvmRunning"
                   @click="openCheckDialog([])">
          全量校验
        </el-button>
        <span class="grow" />
        <el-button size="small" :icon="Upload" @click="exportVisible = true">导出/订阅</el-button>
        <el-button size="small" :icon="Download" @click="importVisible = true">导入</el-button>
        <el-button size="small" :icon="Delete" @click="trashVisible = true">回收站</el-button>
        <el-button size="small" @click="tagManagerVisible = true">标签管理</el-button>
        <el-button size="small" :icon="MagicStick" @click="tidyVisible = true">整理源</el-button>
      </div>
    </div>

    <!-- 移动端：搜索 + 筛选 + 工具，**一行**。工具行默认收起，点开才铺开 -->
    <div class="bar bar-rows page-toolbar" v-if="isMobile">
      <div class="bar-row">
        <el-input class="q-input" v-model="query.q" placeholder="搜索书源"
                  clearable :prefix-icon="Search" @keyup.enter="search" />
        <el-badge :value="filterCount" :hidden="!filterCount" type="primary">
          <el-button :icon="Filter" @click="filterVisible = true">筛选</el-button>
        </el-badge>
        <!-- 工具**不是筛选**：它们跟「这批源」无关（新建/校验/导入/导出/回收站/标签/
             整理）。以前全塞在筛选抽屉里，是因为那时手机端没有别的地方可挂——抽屉因此
             成了「什么都往里放」的筐。现在给它们一行自己的位置，抽屉回到单一职责。
             校验也在这儿（不在抽屉里）：它的语义**跟着勾选变**（校验选中 / 全量校验），
             那是动作，不是筛选条件。用一次就收起——它是菜单，不是常驻面板 -->
        <el-button :icon="Operation" :type="toolsOpen ? 'primary' : 'default'"
                   aria-label="工具" @click="toolsOpen = !toolsOpen">工具</el-button>
      </div>
      <!-- 用 v-show 而不是 el-collapse-transition：实测**刷新后第一次展开不渲染**——
           按钮高亮成 primary 了、整行却还是 display:none（宽度量出来全是 0），
           再点一次才正常。疑与 EP 那个过渡组件首次 enter 取到的 scrollHeight 为 0 有关
           （行还是 none 时量高就是 0）。这一行是折叠菜单，展开/收起瞬时切换即可，
           不值得为了滑动动画赌一个「点了没反应」 -->
      <div v-show="toolsOpen" class="bar-row tools-row">
        <!-- 与桌面那条动作行**同一组、同一顺序**（新建源 / 校验 / 导出订阅 / 导入 /
             回收站 / 标签管理 / 整理源）；也同尺寸（size="small"）——手机上 7 个按钮
             本来就放不下，用 small 的一档内边距能把行数收少一档
             （高度仍是 36px：全局那条「触摸目标不用 small」的规则管着，别在这儿掀） -->
        <el-button size="small" :icon="Plus" @click="toolsOpen = false; openNew()">
          新建源
        </el-button>
        <el-button size="small" :icon="Refresh" :loading="checking"
                   @click="toolsOpen = false; openCheckDialog([])">
          全量校验
        </el-button>
        <el-button size="small" :icon="Upload" @click="toolsOpen = false; exportVisible = true">
          导出/订阅
        </el-button>
        <el-button size="small" :icon="Download" @click="toolsOpen = false; importVisible = true">
          导入
        </el-button>
        <el-button size="small" :icon="Delete" @click="toolsOpen = false; trashVisible = true">
          回收站
        </el-button>
        <el-button size="small" @click="toolsOpen = false; tagManagerVisible = true">
          标签管理
        </el-button>
        <el-button size="small" :icon="MagicStick" @click="toolsOpen = false; tidyVisible = true">
          整理源
        </el-button>
      </div>
    </div>

    <!-- 批量操作条：**勾选之后要干什么**都在这儿（校验选中 / 移入回收站 / 取消选择），
         桌面上还多一排「加/去标签」。这条 bar 的前提是「有勾选」，它只在有勾选时出现、
         同屏还写着「已选 N 条」——所以它的动作**不必变脸**，按钮只说这一种范围。
         手机上动作只留图标（文字在窄屏藏掉）：一行放得下五个元素，而且图标与桌面同款、
         aria-label 保命名不变，两种形态不会各写一套 -->
    <div class="batch-bar" v-if="selected.length">
      <span class="batch-text">已选 <b>{{ selected.length }}</b> 条</span>
      <!-- 入口是「先在表头（或移动端卡片）上勾一条」——批量条本身只在有勾选时出现。
           跨页勾选做不了（表格只渲染当前页），所以这一步走显式 URL 列表。
           文案短：手机上一行要放五个元素，长文案（「选中全部 N 条筛选结果」）
           单独就占 147px，只这一条就把整行挤成两行 -->
      <el-button v-if="total > selected.length" size="small" link type="primary"
                 @click="selectAllFiltered">
        全选 {{ total }} 条
      </el-button>
      <div class="flex-1"></div>
      <template v-if="!isMobile">
        <el-divider direction="vertical" />
        <el-select class="w-batch" v-model="batchTags" multiple filterable allow-create
                   default-first-option :reserve-keyword="false" placeholder="选择或输入标签"
                   size="small">
          <el-option v-for="t in tags.filter((x) => x.kind === 'user')" :key="t.tag" :value="t.tag"
                     :label="t.tag + ' (' + t.count + ')'" />
        </el-select>
        <el-button size="small" :disabled="!batchTags.length" @click="applyBatchTags('add')">
          加标签
        </el-button>
        <el-button size="small" :disabled="!batchTags.length" @click="applyBatchTags('remove')">
          去标签
        </el-button>
        <el-divider direction="vertical" />
      </template>
      <!-- 手机上**不给 default 插槽**，el-button 就只剩图标；桌面才给文字。
          （不再叠 `.desktop-only`：那是同一条 900px 断点的另一条链路，两套一起用
          只会在有人改断点时留下一个静默的第二种行为。）命名由 aria-label 保住——
          两种形态同名，读屏与将来的测试都认它 -->
      <el-button size="small" :icon="Refresh" :loading="checking" aria-label="校验选中"
                 @click="openCheckDialog([...selected])">
        <template v-if="!isMobile" #default>校验选中</template>
      </el-button>
      <el-button size="small" type="danger" plain :icon="Delete" aria-label="移入回收站"
                 @click="removeSelected">
        <template v-if="!isMobile" #default>移入回收站</template>
      </el-button>
      <el-button size="small" link :icon="Close" aria-label="取消选择" @click="clearSelection">
        <template v-if="!isMobile" #default>取消选择</template>
      </el-button>
    </div>

    <!-- 上次校验的结果条。**持久**，不是 toast——「新校验几条 / 复用几条 / 几条变了」
         是要看第二眼的数字，而 ElMessage 三秒就没了；错过之后列表莫名其妙不一样了，
         用户没有任何线索。「排序/筛选项过期」并进同一条：它是同一次校验的副产品，
         分成两条只是在加噪音 -->
    <el-alert v-if="checkResult" class="result-tip" show-icon
              :type="checkResult.changes.length ? 'warning' : 'success'"
              @close="checkResult = null">
      <!-- **整条只有一行**：数字多的时候会很长，交给省略号 + tooltip 兜住。
           换行的话高度会随内容变，把下面的列表挤来挤去 -->
      <template #title>
        <div class="tip-line">
          <el-tooltip :content="checkTipText" placement="top" :show-after="300">
            <span class="tip-text">{{ checkTipText }}</span>
          </el-tooltip>
          <span class="grow" />
          <el-button link type="primary" size="small" @click="openJobDetail">查看</el-button>
          <el-button v-if="checkResult.stale" link type="primary" size="small"
                     @click="checkResult = null; load()">刷新列表</el-button>
        </div>
      </template>
    </el-alert>

    <!-- 列表区：移动端卡片 / 桌面表格 -->
    <div class="page-fill">
      <div v-if="isMobile" class="card-list" v-loading="loading">
        <!-- 卡片是纯展示组件（`components/SourceCard.vue`）：勾选与「正在校验」从这儿进，
             四个动作从它出来——手机上的布局调整都落在那个文件里，改它不必读这个 1200 行的 -->
        <SourceCard v-for="row in rows" :key="row.source_url" :row="row"
                    :selected="isSelected(row)" :checking="isRowChecking(row.source_url)"
                    @toggle="toggleCard" @check="checkOne"
                    @edit="openEdit" @remove="removeOne" />
        <el-empty v-if="!loading && !rows.length" description="没有匹配的书源" :image-size="80" />
      </div>

      <el-table v-else ref="tableRef" :data="rows" v-loading="loading" border stripe size="small"
                height="100%" @selection-change="onTableSelect">
        <el-table-column type="selection" width="42" />
        <el-table-column prop="name" label="名称" min-width="170" show-overflow-tooltip>
          <template #default="{ row }">
            <a href="#" @click.prevent="openEdit(row)">{{ row.name || "（无名）" }}</a>
          </template>
        </el-table-column>
        <el-table-column label="类型" width="88" align="center">
          <template #default="{ row }">{{ typeLabel(row.source_type) }}</template>
        </el-table-column>
        <el-table-column label="健康" width="112" align="center">
          <template #default="{ row }">
            <el-tooltip v-if="lockedStatus(row)" placement="top" :show-after="200">
              <template #content>
                <div>手动锁定为「{{ lockedStatus(row) }}」</div>
                <div>实测：{{ row.health ? healthLabel(row.health) : "未校验" }}{{ row.checked_at ? " · " + row.checked_at : "" }}</div>
              </template>
              <el-tag size="small" :type="healthCell(row).type">{{ healthCell(row).label }}</el-tag>
            </el-tooltip>
            <el-tag v-else-if="healthCell(row)" size="small" :type="healthCell(row).type">
              {{ healthCell(row).label }}
            </el-tag>
            <span v-else class="muted">未校验</span>
          </template>
        </el-table-column>
        <!-- 「验证」是**一列一结论**：引擎只剩一台，所以不再并排一列「JVM」。
             单元格仍是「验到哪一步 + 过没过」，明细进 tooltip；tooltip 第一行写清
             **结论来自谁**（checks.engine）——藏的是"选择"，不是"证据来源"。
             原来还有一列「目录/正文」专门显示明细，那与「实测 / 仅规则」是同一件事的
             摘要与明细，两列并排是重复的：留下摘要，明细收进这里。 -->
        <el-table-column label="验证" width="112" align="center">
          <template #default="{ row }">
            <el-tooltip placement="top" :show-after="200">
              <template #content>
                <div class="muted">结论来自：{{ engineLabel(row.engine) }}</div>
                <div>健康：{{ lockedStatus(row) ? lockedStatus(row) + "（手动）" : (row.health ? healthLabel(row.health) : "未校验") }}</div>
                <div>搜索：{{ row.search_hit ? "命中《" + row.search_hit + "》" : "未命中" }}</div>
                <div>目录：{{ row.toc_complete === 1 ? "完整 ✓" : row.toc_complete === 0 ? "不完整 ✗" : "未验证" }}</div>
                <div>正文：{{ row.content_ok === 1 ? "可用 ✓" : row.content_ok === 0 ? "不可用 ✗" : "未验证" }}</div>
                <div v-if="row.stars">星级：{{ row.stars }}★ {{ starBasisLabel(row.star_basis) }}</div>
                <!-- 本机引擎那一层（证据阶梯的第二层）：同一台引擎的逐段明细，
                     跑到的段才显示（没跑的不写"未验证"——那是**没跑**，不是**跑了没过**） -->
                <template v-if="row.jvm_state">
                <div>本机引擎：{{ jvmStateLabel(row.jvm_state) }}</div>
                <!-- 逐段结果：跑到哪一段就显示哪几行（没跑的不显示"未验证"） -->
                <div v-for="s in jvmSteps(row)" :key="s.label" class="jvm-step">
                  {{ s.label }}：{{ s.text }}
                  <span v-if="s.ok === true">✓</span>
                  <span v-else-if="s.ok === false">✗</span>
                </div>
                <!-- 正文偏短的附注（判据与措辞都在服务端 core.quality）：
                     它**不改结论**——✓ 还是 ✓，只是把「这条通过可疑」说出来 -->
                <div v-if="row.jvm_content_note" class="muted">{{ row.jvm_content_note }}</div>
                <!-- 浏览器渲染状态（S3-4）：只在走过浏览器时显示——
                     「没渲染过」和「渲染过」是两件事，摆在一起会让人以为源有问题 -->
                <div v-if="row.jvm_rendered === true" class="muted">浏览器渲染：已渲染</div>
                <div v-else-if="row.jvm_rendered === false" class="muted">
                  浏览器渲染：失败（{{ row.jvm_render_reason || "原因未记录" }}）
                </div>
                <!-- 登录态（A3）：**只在有意义时显示**——带了就说带了（说明「需登录」
                     不是没试过），这行是「需登录」而没带时才提示下一步。平时不显示：
                     没登录过的源太多，人人一行会把 tooltip 淹没 -->
                <div v-if="row.jvm_cookie_len > 0" class="muted">
                  登录态：本次带了 {{ row.jvm_cookie_len }} 字符的 cookie
                </div>
                <div v-else-if="row.jvm_state === 'login_wall'" class="muted">
                  登录态：本次未带 cookie——先在浏览器里登录一次，之后按源自动复用
                </div>
                <div v-if="row.jvm_batch" class="muted">批次：{{ row.jvm_batch }}</div>
                </template>
              </template>
              <!-- 显示**验到哪一步**而不是星级：星级里大部分是「按规则推的」
                   （实测 static 占多数），而「验到哪一步」是用户真正能据此判断的东西；
                   星级与「实测/仅规则」收进同一个 tooltip，信息不丢 -->
              <span v-if="row.probe_depth" :class="depthClass(row)">{{ depthText(row) }}</span>
              <span v-else class="muted">未校验</span>
            </el-tooltip>
          </template>
        </el-table-column>
        <el-table-column label="标签" min-width="210">
          <template #default="{ row }">
            <el-tag v-for="t in tagCellsOf(row)" :key="t.key" size="small" :type="t.type">
              {{ t.label }}
            </el-tag>
            <span v-if="!hasAnyTag(row)" class="muted">（无标签）</span>
          </template>
        </el-table-column>
        <el-table-column prop="source_url" label="域名" min-width="190" show-overflow-tooltip>
          <template #default="{ row }">
            <!-- 空 URL 不给 <a>：href="" 会重载当前页。点开的是显示的那个地址
                 （库里的 source_url 已归一化），不是只取域名 -->
            <a v-if="row.source_url" class="mono" :href="row.source_url"
               target="_blank" rel="noopener noreferrer">{{ row.source_url }}</a>
          </template>
        </el-table-column>
        <el-table-column prop="checked_at" label="校验时间" width="146" />
        <el-table-column label="操作" width="140" fixed="right" align="center">
          <template #default="{ row }">
            <el-button link size="small" :loading="isRowChecking(row.source_url)"
                       @click="checkOne(row)">校验</el-button>
            <el-button link type="danger" size="small"
                       @click="removeOne(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <el-pagination class="page-footer" background
                   :layout="isMobile ? 'prev, pager, next' : 'total, sizes, prev, pager, next, jumper'"
                   :total="total" :page-size="query.limit" :pager-count="isMobile ? 5 : 7"
                   :page-sizes="[20, 50, 100, 200]"
                   @current-change="onPage"
                   @size-change="(s) => { query.limit = s; search(); }" />

    <!-- 移动端筛选底部抽屉。**只管筛选**：条件 + 查询/重置（校验与其他工具都在
         工具行——校验的语义跟着勾选变，是动作不是筛选条件） -->
    <el-drawer v-model="filterVisible" title="筛选" direction="btt" size="auto" :with-header="true">
      <div class="sheet">
        <!-- 同一份筛选表单（桌面工具条那处是 bar）。两处入口各写一遍时，加一个筛选项
             只改一处**不会报错**——表现是两个入口能筛出来的东西不一样 -->
        <SourceFilterForm variant="sheet" :query="query" :groups="groups" :tags="tags"
                          @search="search(); filterVisible = false"
                          @reset="reset(); filterVisible = false" />
      </div>
    </el-drawer>

    <GroupManagerDrawer v-model="tagManagerVisible" @changed="onTagsChanged" />
    <SourceEditDialog v-model="dlgVisible" :source-url="dlgUrl" @saved="onSaved" />
    <TrashDrawer v-model="trashVisible" @changed="load" />
    <!-- 第 3 步的「去跑全量校验」直接复用批量校验那条链路：关掉抽屉、打开确认框。
         参数（含「忽略缓存」）都在那个框里选，不在这里再摆一套 -->
    <TidyDrawer v-model="tidyVisible" @changed="load"
                @request-check="tidyVisible = false; openCheckDialog([])" />
    <ExportDrawer v-model="exportVisible" :selected="selected"
                  :filter="query" :filtered-total="total" />
    <ImportDialog v-model="importVisible" @imported="load" />
    <JobsDrawer ref="jobsRef" v-model="jobsVisible" :focus-job-id="focusJobId"
                @running-change="jobBadge = $event" />

    <!-- 批量校验的确认弹框：**一台引擎**（在 JVM 里跑「阅读」App 的真源码）。
         **跑哪些由入口决定**——点开它的那一刻就定了（勾选的 / 当前筛选的 / 全量），
         框里不再有范围选择；可调的是**本次参数**（关键词 / 超时 / 并发 / 挡位 / 条数上限）
         与「忽略缓存」，它们只作用于这一次、不写回设置。
         桌面与移动端共用（移动端宽度由 styles.css 的媒体查询压到 94vw）；
         单条校验不弹框，直接用全局设置。 -->
    <el-dialog v-model="checkDialog" :title="checkDialogTitle" width="460px" top="4vh"
               append-to-body class="check-dialog" modal-class="check-dialog-overlay">
      <div class="muted" style="margin-bottom: 12px">{{ checkDialogHint }}</div>


      <CheckJvmForm ref="jvmFormRef" :scope="checkScope" :scope-count="scopeCount"
                    @ready="jvmReady = $event" />

      <template #footer>
        <el-button @click="checkDialog = false">取消</el-button>
        <el-button type="primary" :disabled="checking || jvmRunning || !jvmReady"
                   @click="startPendingCheck">
          开始校验
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
/* ── 本页自己的样式 ────────────────────────────────────────────────
   这一族规则作用到的元素**全是本组件写的**（工具条两行、抽屉的插槽内容、卡片列表），
   所以放 scoped 就够：不必靠 class 前缀自律，也不会外泄到别的组件。
   （够不到的是 el-drawer 自己的 `.el-drawer__body` 那一层——真需要动它时才单开一个
   不带 scoped 的块，本组件末尾那个 `.check-dialog` 块就是这种。）

   留在 styles.css 的只有两类：跨组件复用的原语（骨架的 `.app-` / `.page-` 系列、
   `.bar`、`.toolbar`、`.muted`、`.dot` 系列），以及打向第三方组件内部结构的规则
   （`.el-table .cell`、`.el-dialog__body` 那类——它们身上没有 scope id，
   只能不带 scoped）。 */
.bar-rows { flex-direction: column; align-items: stretch; }
/* 工具条显式分两行（筛选 / 操作）与移动端的两行（搜索行 / 工具行）共用这一条。
   **不靠 flex-wrap 偶然决定断点**：1440px 下桌面那行本来就会换行，而 .grow 撑开的
   空档会自己占掉一行，把操作按钮挤到第三行——「校验参数」和「全量校验」因此分处
   第一行和第三行，等于白挨着。分行是结构，不该是布局的副产物。 */
.bar-row {
  display: flex; flex-wrap: wrap; gap: 8px; align-items: center;
  width: 100%;
}
/* 手机那一行（搜索 + 筛选 + 工具）**不写死宽度**：输入框 `flex: 1 1 0` 吃掉余量，
   按钮各按内容宽，整行自然落成一行。
   这里原来写的是 `flex: 1 1 140px`——那是在"猜一个基准宽"，加了「工具」之后差 2px
   就把「新建」挤到第二行，只能再去调数字。基准改成 0 之后与机型无关：basis 为 0 的项
   不参与"这行要不要换行"的决定，只有按钮的宽度会，而按钮在任何手机宽度下都放得下 */
.q-input { flex: 1 1 0; min-width: 0; }
/* 手机工具行：7 个按钮在 390px 下放不下，靠上面那条 `.bar-row` 的 flex-wrap 换行；
   这里只需让按钮按内容排布，并去掉 Element Plus 相邻按钮的 margin——间距交给 gap，
   否则换行后的第一个按钮会多缩进 12px、两排左边缘对不齐 */
.tools-row .el-button { flex: 1 1 auto; margin-left: 0; }

/* 移动端筛选抽屉：撤走工具之后只剩筛选条件 + 查询/重置 + 校验 */
.sheet { display: flex; flex-direction: column; gap: 14px; padding: 4px 2px 8px; }
.sheet .btns { display: flex; gap: 8px; padding-top: 4px; }
.sheet .btns .el-button { flex: 1 1 0; }

/* 批量操作条：仅勾选时出现 */
.batch-bar {
  /* flex: 0 0 auto 与 .result-tip 同理：它是 `.page-flex` 的直接子项，不排除在收缩
     之外的话，列表一高就被压扁、内容被自己的 overflow 裁掉 */
  flex: 0 0 auto;
  /* **不换行**。这条里的元素宽度都是内容定的（不像工具条那行有个输入框能吸收余量），
     空间不够时 flex 只能把它们折到下一行——而它是一条动作条，折行既难看又把列表
     顶下去。所以定成 nowrap，并拿 overflow-x: auto 当安全阀：放不下时**横滑**而不是
     折行（统计条 chips 在手机端就是这个做法） */
  display: flex; flex-wrap: nowrap; gap: 8px; align-items: center;
  overflow-x: auto;
  padding: 8px 12px;
  background: #ecf5ff;
  border: 1px solid #d9ecff;
  border-radius: 6px;
}
.batch-bar .batch-text { font-size: 13px; color: #409eff; }
.batch-bar .batch-text b { font-size: 15px; margin: 0 2px; }
.w-batch { width: 240px; }
.flex-1 {
  flex: 1;
}

/* ================= 卡片列表（移动端主视图） =================
   只有**容器**留在这儿；卡片自己的样式跟着卡片走了（components/SourceCard.vue） */
.card-list {
  flex: 1 1 auto; min-height: 0;
  overflow-y: auto;
  -webkit-overflow-scrolling: touch;
  display: flex; flex-direction: column; gap: 8px;
  padding-bottom: 4px;
}

/* 校验结果条。
   **flex: 0 0 auto 是必须的**：`.page-flex` 是 `height:100% + overflow:hidden` 的纵向
   flex，而它的子项里只有 .page-toolbar / .page-footer 被排除在收缩之外——这条两样都
   不是，于是列表一高它就被压扁，内容被自身的 overflow:hidden 裁掉，表现就是
   「结果条上的字被遮挡」。
   高度固定成一行：数字多了靠省略号 + tooltip，不让它换行把列表高度挤来挤去 */
.result-tip { flex: 0 0 auto; margin: 0 0 8px; }
/* 给 el-alert 绝对定位的关闭按钮留出位置，免得它压住右边的按钮 */
.result-tip :deep(.el-alert__title) { padding-right: 6px; }
.tip-line { display: flex; align-items: center; gap: 8px; min-width: 0; }
/* 验证深度列的结果着色：绿=验过且通过、红=验了没过、不着色=还没验到那一步 */
.v-ok { color: var(--el-color-success); }
.v-bad { color: var(--el-color-danger); }
.tip-text { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.tip-line .grow { flex: 1 1 auto; }

/* 统计条：与工具栏同一套底色/描边，夹在页面顶部 */
.stats-bar {
  flex: 0 0 auto;
  display: flex; align-items: center; gap: 8px;
  padding: 8px 12px;
  background: #fff;
  border: 1px solid #e4e7ed;
  border-radius: 6px;
}
.stats-bar .grow { flex: 1 1 auto; }
.stats-bar .chips {
  display: flex; flex-wrap: wrap; gap: 8px; align-items: center;
  min-width: 0;
}
.stats-bar .chip {
  flex: 0 0 auto;
  display: inline-flex; align-items: center; gap: 5px;
  padding: 3px 10px;
  font: inherit; font-size: 12px; line-height: 20px;
  color: #606266;
  background: #f4f6f9;
  border: 1px solid #e4e7ed;
  border-radius: 12px;
  cursor: pointer;
  white-space: nowrap;
}
.stats-bar .chip:hover { border-color: #409eff; color: #409eff; }
/* 当前生效的 chip：与 query.health 同步高亮 */
.stats-bar .chip.active {
  background: #ecf5ff; border-color: #409eff; color: #409eff; font-weight: 600;
}

@media (max-width: 900px) {
  /* 移动端：chips 单行横滑，「任务」按钮钉在右侧不被挤出去 */
  .stats-bar { gap: 6px; padding: 6px 10px; }
  .stats-bar .chips { flex-wrap: nowrap; overflow-x: auto; -webkit-overflow-scrolling: touch; }
}
</style>
<!-- 全量校验弹框：只允许 body 内部滚动，弹窗自己不滚。弹窗内部是 teleport 到 body 的，
     scoped 样式够不到，所以这块不带 scoped、用 .check-dialog 前缀限定（AGENTS #15）；
     .el-dialog.check-dialog 多一级是刻意的——与 Element Plus 的 .el-dialog 同特异性时
     要赌样式注入顺序，而那条赌不起（轻则没有内滚动，重则底部被裁掉） -->
<style>
.check-dialog-overlay .el-overlay-dialog { overflow: hidden; }
.el-dialog.check-dialog {
  display: flex;
  flex-direction: column;
  max-height: 90vh;
  margin-bottom: 0;
  overflow: hidden;
}
.check-dialog .el-dialog__header,
.check-dialog .el-dialog__footer { flex: 0 0 auto; }
.check-dialog .el-dialog__body {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  overflow-x: hidden;
}
</style>
