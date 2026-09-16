<script setup>
import { ref, reactive, computed, nextTick, watch, onMounted, onUnmounted } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { Search, Plus, Upload, Download, Delete, Filter, Refresh, Monitor } from "@element-plus/icons-vue";
import { listSources, listSourceUrls, listGroups, patchTags, deleteSources, listTags, getStats } from "../api/sources";
import { api, subscribeJob } from "../api/client";
import { ensureTagMeta, isQualityTag, splitTags, tagOfType, sourceTypes } from "../utils/tags";
import { HEALTH_LABELS, describeChanges, healthLabel, starBasisLabel } from "../utils/health";
import { useMobile } from "../composables/useMobile";
import SourceEditDialog from "../components/SourceEditDialog.vue";
import TrashDrawer from "../components/TrashDrawer.vue";
import ExportDrawer from "../components/ExportDrawer.vue";
import ImportDialog from "../components/ImportDialog.vue";
import GroupManagerDrawer from "../components/GroupManagerDrawer.vue";
import JobsDrawer from "../components/JobsDrawer.vue";
import CheckOverrideForm from "../components/CheckOverrideForm.vue";

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
const exportVisible = ref(false);
const importVisible = ref(false);
const filterVisible = ref(false);
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

//: 总数优先用后端报的（提交列表与"全量"的实际条数可能不同，比如回收站/禁用的源）
const checkStatusText = computed(() => {
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

// 批量校验的「本次覆盖」：只含与全局设置不同的键，空对象 = 全走全局设置。
//: 「忽略缓存，全部重校」。**不是覆盖项**（没有全局对应值可比），是纯粹的本次动作。
const refreshThisRun = ref(false);
const checkOverride = ref({});

// 批量校验的确认弹框。**选项就长在这个框里**。
//
// 以前「校验参数」是工具栏上一个独立按钮，而「全量校验」另弹一个只问"确定吗"的
// 空确认框：选项和动作分在两处，那个确认框纯粹是减速带。合起来之后工具栏少一个
// 按钮，确认框变成有用的一步，选项也不再是"长在全量校验旁边、却影响行按钮"的隐形状态
// ——它只在批量动作的弹框里出现，一一对应。
//
// 单条校验（表格行 / 卡片图标）**不弹框**，直接用全局设置：单条本来就没什么可调的。
const checkDialog = ref(false);
const checkDialogRef = ref(null);
//: 弹框确认后要校验的 URL 列表；空数组 = 全量
const pendingCheckUrls = ref([]);

// 打开时才去拉全局设置（拿到的是最新全局值），所以必须等容器渲染完再调
watch(checkDialog, (v) => {
  if (v) nextTick(() => { if (checkDialogRef.value) checkDialogRef.value.reload(); });
});

const checkDialogTitle = computed(
  () => (pendingCheckUrls.value.length ? "校验选中" : "全量校验"));
const checkDialogHint = computed(() => {
  if (pendingCheckUrls.value.length) {
    return "将校验选中的 " + pendingCheckUrls.value.length + " 条源。";
  }
  const n = stats.value ? stats.value.sources : "?";
  return "将校验全部未删除书源（共 " + n + " 条），可能耗时数分钟。";
});

/** 打开批量校验的弹框。批量入口都走这里，单条不走。 */
function openCheckDialog(urls = []) {
  pendingCheckUrls.value = urls;
  checkDialog.value = true;
}

function startPendingCheck() {
  checkDialog.value = false;
  checkSources(pendingCheckUrls.value);
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
  order: "-stars", limit: 50, offset: 0,
});

const healthType = { ok: "success", dead: "danger", auth: "warning", gfw: "info" };
const typeLabel = (v) => tagOfType(v) || ("类型" + v);

// 健康度的取值：统计条上的 chip（点一下直接改筛选条件）和两个下拉共用这一份。
//
// **9 个健康态一个都不能少**：原来这里有两份，各只列了 4 个（ok/dead/auth/gfw），
// 于是 timeout / no_search / error / skipped 的源在统计条上一个都数不到——各 chip
// 之和小于总数，看着像凭空少了一批源，而且没法按它们下钻、下钻不到就没法批量处理。
// 文案从 HEALTH_LABELS 取（那是 core/models.py HEALTH_NAMES 的显示层副本），
// 别在这儿再抄一份名字。
const HEALTH_OPTIONS = [
  "ok", "dead", "auth", "gfw", "no_search", "timeout", "error", "cert", "skipped",
].map((value) => ({ value, label: HEALTH_LABELS[value] }));

// stats.health 的键是 str(health)：没有校验记录时 health 为 NULL，键就是字符串 "None"
const healthCount = (key) => {
  const h = stats.value && stats.value.health;
  return (h && h[key]) || 0;
};

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

//: 列表 API 的 toc_complete/content_ok 是 0/1/null（SourceOut 里是 Optional[int]，
//: 由 SQLite 的三态整数来的），而校验结果里是 true/false/null（Python bool）。
//: **不转就静默坏掉**：模板里判的是 `=== 1`，赋个 true 进去两个分支都不成立，
//: 表现是那一格永远显示「未验证」——看着像没校验，其实是类型不对
const triToInt = (v) => (v === true ? 1 : v === false ? 0 : null);

//: URL 两侧必须同口径：列表里的 source_url 是库里归一化过的（去空白/尾斜杠/小写），
//: 后端下发 items 时也已归一。这里再兜一次——归一化是最容易漏在半路的那种约定
const urlKey = (u) => String(u || "").trim().replace(/\/+$/, "").toLowerCase();

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
  Object.assign(query, { q: "", type: null, health: "", group: "", tag: "", order: "-stars", offset: 0 });
  load();
}
function onPage(p) { query.offset = (p - 1) * query.limit; load(); }

function clearSelection() {
  selected.value = [];
  if (tableRef.value) tableRef.value.clearSelection();
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
  const onPage = new Set(rows.value.map((r) => r.source_url));
  const kept = selected.value.filter((u) => !onPage.has(u));
  selected.value = [...kept, ...picked.map((r) => r.source_url)];
}

//: 把 selected 的状态同步回表格勾选。表格只渲染当前页，翻页后它自己会忘掉勾选
//: 状态——不同步的话，翻回来看见的是「批量条说选了 800 条、表格上一个都没勾」
function syncTableSelection() {
  const t = tableRef.value;
  if (!t || !selected.value.length) return;   // 常态（没选任何行）直接跳过
  rows.value.forEach((row) => {
    t.toggleRowSelection(row, selectedUrls.value.has(row.source_url));
  });
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

function userTagsOf(row) {
  return splitTags(row.user_tags || "");
}

// group_name 是系统标签的逗号拼接串。类型和健康状态表格里已各自单独成列展示，
// 这里只取不重复的质量标签（规则完整），避免同一信息渲染两遍。
function qualityTagsOf(row) {
  return splitTags(row.group_name || "").filter((t) => isQualityTag(t));
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
/**
 * 任务失败的原因（后端写进 `result_json` 的 `error`）。
 *
 * **失败原因不能只显示状态名**：后端为失败任务写的是 `{"error","trace"}`，
 * 其中「进程重启，任务没写终态（崩溃或被强杀）」这类原因（见 Store.fail_orphan_jobs）
 * 是用户唯一能看到的解释——直接显示 "failed" 等于把原因丢了。
 */
function jobFailReason(resultJson) {
  try { return JSON.parse(resultJson || "{}").error || ""; } catch (e) { return ""; }
}

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
    const payload = { urls, refresh_cache: refreshThisRun.value };
    if (Object.keys(checkOverride.value).length) payload.check = checkOverride.value;
    const r = await api.post("/jobs", { kind: "check", payload });
    checkJobId.value = r.job_id;
    // **不弹「已提交」的 toast**：上面那条状态条已经在说「正在校验 N 条」了，
    // 再来一条浮层只是噪音——而且它挡在统计条旁边，反而盖住了真正的进度
    if (stopCheck) stopCheck();
    stopCheck = subscribeJob(
      r.job_id,
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
              && (filterCount.value > 0 || /stars/.test(query.order));
            // **把 job_id 存进摘要**：resetCheckState() 刚把 checkJobId 清空了，
            // 不存的话结果条上的「查看」点开抽屉不知道要看哪一条
            summary.jobId = r.job_id;
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
  // 列表要按系统标签拆分组内容（qualityTagsOf），必须等枚举到位再拉数据，
  // 否则首屏会把系统标签当成用户标签渲染
  try { await ensureTagMeta(); } catch (e) {
    ElMessage.warning("系统标签枚举加载失败，标签归类可能不准: " + e.message);
  }
  load();
  try { groups.value = await listGroups(); } catch (e) { /* 忽略 */ }
  try { tags.value = await listTags(); } catch (e) { /* 忽略 */ }
});

onUnmounted(() => {
  if (stopCheck) stopCheck();
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
        <button v-for="h in HEALTH_OPTIONS" :key="h.value" type="button" class="chip"
                :class="{ active: query.health === h.value }" @click="onHealthChip(h.value)">
          {{ h.label }} <b>{{ healthCount(h.value) }}</b>
        </button>
        <!-- 「未校验」= 没有校验记录（health IS NULL），后端 _where 已支持值 "none" -->
        <button type="button" class="chip" :class="{ active: query.health === 'none' }"
                @click="onHealthChip('none')">
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
        <el-input class="w-search" v-model="query.q" placeholder="搜名称 / 域名" clearable
                  size="small" :prefix-icon="Search" @keyup.enter="search" />
        <el-select class="w-type" v-model="query.type" placeholder="类型" clearable size="small">
          <el-option v-for="t in sourceTypes" :key="t.value" :value="t.value" :label="t.tag" />
        </el-select>
        <el-select class="w-health" v-model="query.health" placeholder="健康度" clearable size="small">
          <el-option v-for="h in HEALTH_OPTIONS" :key="h.value" :value="h.value" :label="h.label" />
        </el-select>
        <el-select class="w-group" v-model="query.group" placeholder="分组" clearable filterable
                   size="small">
          <el-option v-for="g in groups" :key="g.group" :value="g.group"
                     :label="g.group + ' (' + g.count + ')'" />
        </el-select>
        <el-select class="w-group" v-model="query.tag" placeholder="用户标签" clearable filterable
                   size="small">
          <el-option v-for="t in tags.filter((x) => x.kind === 'user')" :key="t.tag" :value="t.tag"
                     :label="t.tag + ' (' + t.count + ')'" />
        </el-select>
        <el-select class="w-order" v-model="query.order" size="small">
          <el-option value="-stars" label="星级 ↓" />
          <el-option value="stars" label="星级 ↑" />
          <el-option value="-checked_at" label="校验时间 ↓" />
          <el-option value="name" label="名称 ↑" />
        </el-select>
        <el-button type="primary" size="small" @click="search">查询</el-button>
        <el-button size="small" @click="reset">重置</el-button>
      </div>

      <div class="bar-row">
        <el-button size="small" :icon="Plus" @click="openNew">新建源</el-button>
        <!-- 本次的选项（参数覆盖 + 忽略缓存）在点开后的弹框里，不再单独占一个按钮 -->
        <el-button size="small" :icon="Refresh" :disabled="checking"
                   @click="openCheckDialog([])">
          全量校验
        </el-button>
        <span class="grow" />
        <el-button size="small" :icon="Upload" @click="exportVisible = true">导出/订阅</el-button>
        <el-button size="small" :icon="Download" @click="importVisible = true">导入</el-button>
        <el-button size="small" :icon="Delete" @click="trashVisible = true">回收站</el-button>
        <el-button size="small" @click="tagManagerVisible = true">标签管理</el-button>
      </div>
    </div>

    <!-- 移动端：搜索 + 筛选 + 新建 -->
    <div class="bar page-toolbar mobile-only" v-if="isMobile">
      <el-input style="flex: 1 1 140px; min-width: 0" v-model="query.q" placeholder="搜索书源"
                clearable :prefix-icon="Search" @keyup.enter="search" />
      <el-badge :value="filterCount" :hidden="!filterCount" type="primary">
        <el-button :icon="Filter" @click="filterVisible = true">筛选</el-button>
      </el-badge>
      <el-button type="primary" :icon="Plus" @click="openNew" />
    </div>

    <!-- 批量操作条 -->
    <div class="batch-bar" v-if="selected.length">
      <span class="batch-text">已选 <b>{{ selected.length }}</b> 条</span>
      <!-- 入口是「先在表头（或移动端卡片）上勾一条」——批量条本身只在有勾选时出现。
           跨页勾选做不了（表格只渲染当前页），所以这一步走显式 URL 列表 -->
      <el-button v-if="total > selected.length" size="small" link type="primary"
                 @click="selectAllFiltered">
        选中全部 {{ total }} 条筛选结果
      </el-button>
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
      </template>
      <el-divider direction="vertical" />
      <el-button size="small" :icon="Refresh" :loading="checking" :disabled="!selected.length"
                 @click="openCheckDialog([...selected])">
        校验选中
      </el-button>
      <el-button size="small" type="danger" plain @click="removeSelected">移入回收站</el-button>
      <el-button size="small" link @click="clearSelection">取消选择</el-button>
      <span v-if="isMobile" class="grow" />
      <el-button v-if="isMobile" size="small" type="primary" :icon="Upload"
                 @click="exportVisible = true">导出/订阅</el-button>
    </div>

    <!-- 上次校验的结果条。**持久**，不是 toast——「新校验几条 / 复用几条 / 几条变了」
         是要看第二眼的数字，而 ElMessage 三秒就没了；错过之后列表莫名其妙不一样了，
         用户没有任何线索。「排序/筛选项过期」并进同一条：它是同一次校验的副产品，
         分成两条只是在加噪音 -->
    <el-alert v-if="checkResult" class="result-tip" show-icon
              :type="checkResult.changes.length ? 'warning' : 'success'"
              @close="checkResult = null">
      <template #title>
        <span>本次校验 {{ checkResult.checked }} 条：新校验 {{ checkResult.fetched }}、复用缓存 {{ checkResult.cached }}</span>
        <span v-if="checkResult.changes.length"> · {{ checkResult.changes.join("、") }}</span>
        <span v-else-if="!checkResult.firstChecked"> · 无状态变化</span>
        <span v-if="checkResult.firstChecked"> · {{ checkResult.firstChecked }} 条首次有结论</span>
        <span v-if="checkResult.stale" class="stale"> · 排序/筛选项可能已过期</span>
      </template>
      <template #default>
        <el-button link type="primary" size="small" @click="openJobDetail">查看</el-button>
        <el-button v-if="checkResult.stale" link type="primary" size="small"
                   @click="checkResult = null; load()">刷新列表</el-button>
        <span v-if="checkResult.saveFailures" class="warn">
          {{ checkResult.saveFailures }} 条结果没能写入管理库，列表状态不会更新
        </span>
        <span v-if="checkResult.hitDowngrades" class="warn">
          {{ checkResult.hitDowngrades }} 个源的命中判定降级（规则无法回放，已按「命中」处理）
        </span>
      </template>
    </el-alert>

    <!-- 列表区：移动端卡片 / 桌面表格 -->
    <div class="page-fill">
      <div v-if="isMobile" class="card-list" v-loading="loading">
        <div v-for="row in rows" :key="row.source_url" class="src-card"
             :class="{ sel: isSelected(row) }" @click="toggleCard(row)">
          <div class="chk" @click.stop>
            <el-checkbox :model-value="isSelected(row)" @change="toggleCard(row)" />
          </div>
          <div class="main">
            <div class="row1">
              <span class="nm">{{ row.name || "(无名)" }}</span>
              <span class="stars" v-if="row.stars">{{ "★".repeat(row.stars) }}</span>
              <!-- 这一级是实测来的还是按规则推的。空串（0★）不渲染 -->
              <span v-if="starBasisLabel(row.star_basis)" class="basis"
                    :class="row.star_basis">{{ starBasisLabel(row.star_basis) }}</span>
            </div>
            <div class="host mono">{{ row.source_url }}</div>
            <div class="meta">
              <el-tag size="small">{{ typeLabel(row.source_type) }}</el-tag>
              <el-tag v-if="row.health" size="small" :type="healthType[row.health] || 'info'">
                {{ healthLabel(row.health) }}
              </el-tag>
              <span class="muted nowrap" v-if="row.toc_complete !== null || row.content_ok !== null">
                {{ row.toc_complete === 1 ? "目录✓" : row.toc_complete === 0 ? "目录✗" : "" }}
                {{ row.content_ok === 1 ? " 正文✓" : row.content_ok === 0 ? " 正文✗" : "" }}
              </span>
            </div>
            <div class="grp">
              <el-tag v-for="t in qualityTagsOf(row)" :key="t" size="small" type="info">
                {{ t }}
              </el-tag>
              <el-tag v-if="row.system_tags_locked" size="small" type="warning">手动</el-tag>
              <el-tag v-for="t in userTagsOf(row)" :key="t" size="small" type="success">
                {{ t }}
              </el-tag>
              <span v-if="!qualityTagsOf(row).length && !userTagsOf(row).length"
                    class="muted">(无标签)</span>
            </div>
          </div>
          <el-button link :icon="Refresh" :loading="isRowChecking(row.source_url)"
                     @click.stop="checkSources([row.source_url])" />
          <el-button link :icon="Filter" @click.stop="openEdit(row)" />
          <!-- 与表格操作栏同一组动作：卡片是移动端的等价物，少一个就会
               「手机上没有删除入口、只能先勾选再走批量条」 -->
          <el-button link type="danger" :icon="Delete" @click.stop="removeOne(row)" />
        </div>
        <el-empty v-if="!loading && !rows.length" description="没有匹配的书源" :image-size="80" />
      </div>

      <el-table v-else ref="tableRef" :data="rows" v-loading="loading" border stripe size="small"
                height="100%" @selection-change="onTableSelect">
        <el-table-column type="selection" width="42" />
        <el-table-column prop="name" label="名称" min-width="170" show-overflow-tooltip>
          <template #default="{ row }">
            <a href="#" @click.prevent="openEdit(row)">{{ row.name || "(无名)" }}</a>
          </template>
        </el-table-column>
        <el-table-column label="类型" width="88" align="center">
          <template #default="{ row }">{{ typeLabel(row.source_type) }}</template>
        </el-table-column>
        <el-table-column label="健康" width="92" align="center">
          <template #default="{ row }">
            <el-tag v-if="row.health" size="small" :type="healthType[row.health] || 'info'">
              {{ healthLabel(row.health) }}
            </el-tag>
            <span v-else class="muted">未校验</span>
          </template>
        </el-table-column>
        <el-table-column label="★" width="92" align="center">
          <template #default="{ row }">
            <!-- 悬停看明细：原来有一列「目录/正文」专门显示这些，但它和
                 「实测 / 仅规则」是同一件事的明细与摘要，两列并排是重复的。
                 留下摘要（一眼看可信度），明细收进 tooltip -->
            <el-tooltip placement="top" :show-after="200">
              <template #content>
                <div>域名：{{ row.health ? healthLabel(row.health) : "未校验" }}</div>
                <div>搜索：{{ row.search_hit ? "命中《" + row.search_hit + "》" : "未命中" }}</div>
                <div>目录：{{ row.toc_complete === 1 ? "完整 ✓" : row.toc_complete === 0 ? "不完整 ✗" : "未验证" }}</div>
                <div>正文：{{ row.content_ok === 1 ? "可用 ✓" : row.content_ok === 0 ? "不可用 ✗" : "未验证" }}</div>
              </template>
              <span>
                <span>{{ row.stars }}★</span>
                <span v-if="starBasisLabel(row.star_basis)" class="basis"
                      :class="row.star_basis">{{ starBasisLabel(row.star_basis) }}</span>
              </span>
            </el-tooltip>
          </template>
        </el-table-column>
        <el-table-column label="标签" min-width="210">
          <template #default="{ row }">
            <el-tag v-for="t in qualityTagsOf(row)" :key="t" size="small" type="info">
              {{ t }}
            </el-tag>
            <el-tag v-if="row.system_tags_locked" size="small" type="warning">手动</el-tag>
            <el-tag v-for="t in userTagsOf(row)" :key="t" size="small" type="success">
              {{ t }}
            </el-tag>
            <span v-if="!qualityTagsOf(row).length && !userTagsOf(row).length"
                  class="muted">(无标签)</span>
          </template>
        </el-table-column>
        <el-table-column prop="source_url" label="域名" min-width="190" show-overflow-tooltip>
          <template #default="{ row }"><span class="mono">{{ row.source_url }}</span></template>
        </el-table-column>
        <el-table-column prop="checked_at" label="校验时间" width="146" />
        <el-table-column label="操作" width="140" align="center">
          <template #default="{ row }">
            <el-button link size="small" :loading="isRowChecking(row.source_url)"
                       @click="checkSources([row.source_url])">校验</el-button>
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

    <!-- 移动端筛选底部抽屉 -->
    <el-drawer v-model="filterVisible" title="筛选" direction="btt" size="auto" :with-header="true">
      <div class="sheet">
        <div class="fld">
          <label>关键词</label>
          <el-input v-model="query.q" placeholder="名称 / 域名" clearable />
        </div>
        <div class="fld">
          <label>类型</label>
          <el-select v-model="query.type" placeholder="全部" clearable style="width: 100%">
            <el-option v-for="t in sourceTypes" :key="t.value" :value="t.value" :label="t.tag" />
          </el-select>
        </div>
        <div class="fld">
          <label>健康度</label>
          <el-select v-model="query.health" placeholder="全部" clearable style="width: 100%">
            <el-option v-for="h in HEALTH_OPTIONS" :key="h.value" :value="h.value" :label="h.label" />
          </el-select>
        </div>
        <div class="fld">
          <label>分组</label>
          <el-select v-model="query.group" placeholder="全部" clearable filterable
                     style="width: 100%">
            <el-option v-for="g in groups" :key="g.group" :value="g.group"
                       :label="g.group + ' (' + g.count + ')'" />
          </el-select>
        </div>
        <div class="fld">
          <label>用户标签</label>
          <el-select v-model="query.tag" placeholder="全部" clearable filterable
                     style="width: 100%">
            <el-option v-for="t in tags.filter((x) => x.kind === 'user')" :key="t.tag" :value="t.tag"
                       :label="t.tag + ' (' + t.count + ')'" />
          </el-select>
        </div>
        <div class="fld">
          <label>排序</label>
          <el-select v-model="query.order" style="width: 100%">
            <el-option value="-stars" label="星级 ↓" />
            <el-option value="stars" label="星级 ↑" />
            <el-option value="-checked_at" label="校验时间 ↓" />
            <el-option value="name" label="名称 ↑" />
          </el-select>
        </div>
        <div class="btns">
          <el-button @click="reset(); filterVisible = false">重置</el-button>
          <el-button type="primary" @click="search(); filterVisible = false">查询</el-button>
        </div>
        <div class="btns">
          <el-button :icon="Download" @click="importVisible = true; filterVisible = false">
            导入书源
          </el-button>
          <el-button :icon="Refresh" :loading="checking"
                     @click="openCheckDialog([]); filterVisible = false">
            全量校验
          </el-button>
          <el-button :icon="Delete" @click="trashVisible = true; filterVisible = false">
            回收站
          </el-button>
          <el-button @click="tagManagerVisible = true; filterVisible = false">标签管理</el-button>
        </div>
      </div>
    </el-drawer>

    <GroupManagerDrawer v-model="tagManagerVisible" @changed="onTagsChanged" />
    <SourceEditDialog v-model="dlgVisible" :source-url="dlgUrl" @saved="onSaved" />
    <TrashDrawer v-model="trashVisible" @changed="load" />
    <ExportDrawer v-model="exportVisible" :selected="selected"
                  :filter="query" :filtered-total="total" />
    <ImportDialog v-model="importVisible" @imported="load" />
    <JobsDrawer ref="jobsRef" v-model="jobsVisible" :focus-job-id="focusJobId"
                @running-change="jobBadge = $event" />

    <!-- 批量校验的确认弹框：选项 + 开始。桌面与移动端共用（移动端宽度由
         styles.css 的媒体查询压到 94vw）。单条校验不弹框，直接用全局设置。 -->
    <el-dialog v-model="checkDialog" :title="checkDialogTitle" width="420px" append-to-body>
      <div class="muted" style="margin-bottom: 12px">{{ checkDialogHint }}</div>
      <CheckOverrideForm ref="checkDialogRef" v-model="checkOverride"
                         v-model:refresh="refreshThisRun" />
      <template #footer>
        <el-button @click="checkDialog = false">取消</el-button>
        <el-button type="primary" :disabled="checking" @click="startPendingCheck">
          开始校验
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
/* 星级旁边那个「实测 / 仅规则」。
   光看星级分不出「验出来的」和「看规则推的」——库里 489 条 5★ 全是后者。
   颜色用 element-plus 的 success / warning 色值（本文件其余部分用的也是字面色） */
.basis {
  font-size: 11px;
  line-height: 16px;
  padding: 0 4px;
  margin-left: 4px;
  border-radius: 3px;
  white-space: nowrap;
}
.basis.measured { color: #67c23a; background: #f0f9eb; }
.basis.static { color: #e6a23c; background: #fdf6ec; }

/* 校验结果条：不挤占列表高度，只在需要时出现 */
.result-tip { margin: 0 0 8px; }
.result-tip .stale { color: #e6a23c; }
.result-tip .warn { color: var(--el-color-danger); margin-left: 8px; }

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
/* 不可点的 chip（未校验）：去掉手型与 hover 反馈，避免看着像能筛 */
.stats-bar .chip.readonly { cursor: default; color: #909399; }
.stats-bar .chip.readonly:hover { border-color: #e4e7ed; color: #909399; }

@media (max-width: 900px) {
  /* 移动端：chips 单行横滑，「任务」按钮钉在右侧不被挤出去 */
  .stats-bar { gap: 6px; padding: 6px 10px; }
  .stats-bar .chips { flex-wrap: nowrap; overflow-x: auto; -webkit-overflow-scrolling: touch; }
  .stats-bar .chip { min-height: 32px; }
}
</style>
