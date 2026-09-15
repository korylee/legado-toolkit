<script setup>
import { ref, reactive, computed, nextTick, watch, onMounted, onUnmounted } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { Search, Plus, Upload, Download, Delete, Filter, Refresh, Monitor, Setting } from "@element-plus/icons-vue";
import { listSources, listGroups, patchTags, deleteSources, listTags, getStats } from "../api/sources";
import { api, subscribeJob } from "../api/client";
import { ensureTagMeta, isQualityTag, splitTags, tagOfType, sourceTypes } from "../utils/tags";
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

// 校验参数的「本次覆盖」：只含与全局设置不同的键，空对象 = 全走全局设置。
// 对所有校验入口生效（全量 / 选中 / 单行）——设了代理就是为了能校验被墙源，
// 而按行校验单个源恰恰是最常见的用法。生效时必须看得见，否则会变成
// 「上次覆盖了忘了」的静默差异，所以按钮上有计数徽标。
//: 「忽略缓存，全部重校」。**不是覆盖项**（没有全局对应值可比），是纯粹的本次
//: 动作。放在同一个容器里是为了让「这次怎么测」只有一个入口——它原本挂在
//: 全量校验的 split-button 下拉里，而那个下拉只有这一项，旁边又站着「校验参数」，
//: 两个入口并列反而不知道该点哪个。
const refreshThisRun = ref(false);
const checkOverride = ref({});
const overrideSummary = ref("");
const overrideVisible = ref(false);
const overrideDialog = ref(false);
const overrideRef = ref(null);
const overrideDialogRef = ref(null);
//: 徽标显示"本次有 N 项不是全局默认"，覆盖项与忽略缓存都算
const runOptionCount = computed(
  () => Object.keys(checkOverride.value).length + (refreshThisRun.value ? 1 : 0));

// 打开时才去拉全局设置（拿到的是最新全局值），所以必须等容器渲染完再调。
//
// **不能用 el-popover 的 @show**：ElPopover 声明的 emits 只有
// update:visible / before-enter / before-leave / after-enter / after-leave，
// 没有 show —— 写上去不报错、永远不触发，表单会一直空着（实测踩过）。
// 监听 visible 本身，两个容器共用一套机制，也不依赖过渡时序。
function reloadOverride(target) {
  nextTick(() => { if (target.value) target.value.reload(); });
}
watch(overrideVisible, (v) => { if (v) reloadOverride(overrideRef); });
watch(overrideDialog, (v) => { if (v) reloadOverride(overrideDialogRef); });

// 统计条（替代已删掉的「诊断」页）与「任务」抽屉
const stats = ref(null);
const jobsVisible = ref(false);
const jobBadge = ref(0);
const jobsRef = ref(null);

const query = reactive({
  q: "", type: null, health: "", group: "", tag: "",
  order: "-stars", limit: 50, offset: 0,
});

const HEALTH = [
  { value: "ok", label: "✅可用" }, { value: "dead", label: "❌失效" },
  { value: "auth", label: "🔒需验证" }, { value: "gfw", label: "🌐需翻墙" },
];
const healthType = { ok: "success", dead: "danger", auth: "warning", gfw: "info" };
const typeLabel = (v) => tagOfType(v) || ("类型" + v);

// 统计条上可下钻的健康度 chip。value 即 query.health 的取值，点一下直接改筛选条件
const HEALTH_CHIPS = [
  { value: "ok", label: "✅可用" },
  { value: "dead", label: "❌失效" },
  { value: "auth", label: "🔒需验证" },
  { value: "gfw", label: "🌐需翻墙" },
  { value: "none", label: "未校验" },      // 后端 _where 认这个值 → health IS NULL
];

// stats.health 的键是 str(health)：没有校验记录时 health 为 NULL，键就是字符串 "None"
const healthCount = (key) => {
  const h = stats.value && stats.value.health;
  return (h && h[key]) || 0;
};

async function loadStats() {
  try { stats.value = await getStats(); } catch (e) { stats.value = null; }
}

const selectedUrls = computed(() => new Set(selected.value.map((r) => r.source_url)));
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
    ? selected.value.filter((r) => r.source_url !== key)
    : [...selected.value, row];
}

function userTagsOf(row) {
  return splitTags(row.user_tags || "");
}

// group_name 是系统标签的逗号拼接串。类型和健康状态表格里已各自单独成列展示，
// 这里只取不重复的质量标签（规则完整），避免同一信息渲染两遍。
function qualityTagsOf(row) {
  return splitTags(row.group_name || "").filter((t) => isQualityTag(t));
}

async function removeSelected() {
  if (!selected.value.length) return ElMessage.warning("先勾选源");
  try {
    await ElMessageBox.confirm(
      "将把 " + selected.value.length + " 条源移入回收站。不会再导出到 App，可随时恢复。",
      "移入回收站", { type: "warning" });
    const res = await deleteSources(selected.value.map((r) => r.source_url));
    ElMessage.success("已移入回收站 " + res.deleted + " 条");
    clearSelection();
    load();
  } catch (e) { /* 取消 */ }
}

async function applyBatchTags(mode) {
  if (!selected.value.length) return ElMessage.warning("先勾选源");
  if (!batchTags.value.length) return ElMessage.warning("先选择或输入标签");
  const urls = selected.value.map((r) => r.source_url);
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

// 校验结果提示。**必须报出「复用了几条」**：有效期内的缓存不会重新请求，
// 所以要是不说，「点校验 → 完成」和「一条请求都没发」在界面上长得一模一样
function reportCheckResult(resultJson) {
  let r = null;
  try { r = resultJson ? JSON.parse(resultJson) : null; } catch (e) { return false; }
  if (!r || typeof r.checked !== "number") return false;
  const cached = r.cached || 0;
  const fetched = typeof r.fetched === "number" ? r.fetched : r.checked - cached;
  if (fetched) {
    ElMessage.success("校验完成：新校验 " + fetched + " 条"
                      + (cached ? "，复用缓存 " + cached + " 条" : ""));
  } else if (cached) {
    ElMessage.success("校验完成：全部 " + cached + " 条命中缓存，未发起请求");
  } else {
    ElMessage.success("校验完成");
  }
  // 写库失败要单独报：结果没落库时列表状态不会变，而列表上完全看不出来
  if (r.save_failures) {
    ElMessage.error(r.save_failures + " 条结果没能写入管理库，列表状态不会更新");
  }
  return true;
}

async function checkSources(urls = []) {
  if (checking.value) return ElMessage.warning("已有校验任务在运行");
  checking.value = true;
  try {
    // refresh_cache：忽略有效期内的缓存，全部重新请求。
    // 校验参数（并发/超时/深度/代理等）不再写死在这里——不传就由后端取全局设置，
    // 只有「本次覆盖」的那几项才进 payload
    const payload = { urls, refresh_cache: refreshThisRun.value };
    if (Object.keys(checkOverride.value).length) payload.check = checkOverride.value;
    const r = await api.post("/jobs", { kind: "check", payload });
    ElMessage.success("已提交校验任务 " + r.job_id);
    if (stopCheck) stopCheck();
    stopCheck = subscribeJob(
      r.job_id,
      () => {},
      async (data) => {
        checking.value = false;
        stopCheck = null;
        if (data.status === "done") {
          if (!reportCheckResult(data.result_json)) ElMessage.success("校验完成");
          await load();
          try { tags.value = await listTags(); } catch (e) { /* 忽略 */ }
        } else {
          ElMessage.error("校验任务失败: " + (data.status || "unknown"));
        }
        // 任务收尾后让抽屉那份列表/徽标跟上
        jobsRef.value?.refresh();
      },
    );
    // 新任务立刻反映到「任务」按钮的徽标上
    jobsRef.value?.refresh();
  } catch (e) {
    checking.value = false;
    ElMessage.error("提交校验失败: " + e.message);
  }
}

async function checkAll() {
  const refresh = refreshThisRun.value;
  try {
    // 确认文案里写清楚这次会不会用缓存、以及有没有别的本次设置——不然用户点完
    // 只看到「完成」，既不知道是刚查了一遍还是全走了缓存，也想不起上次改过什么
    await ElMessageBox.confirm(
      (refresh
        ? "忽略缓存，对全部未删除书源重新发起校验。"
        : "校验全部未删除书源；有效期内的缓存会直接复用、不重新请求。")
      + (overrideSummary.value ? overrideSummary.value + "。" : "")
      + "可能耗时数分钟，确认继续？",
      refresh ? "全量重校" : "全量校验", { type: "warning" });
  } catch (e) { return; }
  checkSources([]);
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
        <button v-for="h in HEALTH_CHIPS" :key="h.value" type="button" class="chip"
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
      <el-badge :value="jobBadge" :hidden="!jobBadge" type="primary">
        <el-button size="small" :icon="Monitor" @click="jobsVisible = true">任务</el-button>
      </el-badge>
    </div>

    <!-- 桌面：完整筛选栏 -->
    <div class="bar page-toolbar desktop-only" v-if="!isMobile">
      <el-input class="w-search" v-model="query.q" placeholder="搜名称 / 域名" clearable
                size="small" :prefix-icon="Search" @keyup.enter="search" />
      <el-select class="w-type" v-model="query.type" placeholder="类型" clearable size="small">
        <el-option v-for="t in sourceTypes" :key="t.value" :value="t.value" :label="t.tag" />
      </el-select>
      <el-select class="w-health" v-model="query.health" placeholder="健康度" clearable size="small">
        <el-option v-for="h in HEALTH" :key="h.value" :value="h.value" :label="h.label" />
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
      <span class="grow" />
      <el-button size="small" :icon="Plus" @click="openNew">新建源</el-button>
      <!-- 「这次怎么测」的唯一入口：参数覆盖 + 忽略缓存。生效时按钮上有计数徽标——
           这些只存在内存里，不显示出来就成了「看不见的生效参数」。
           原来「忽略缓存」挂在全量校验的 split-button 下拉里，那下拉只有这一项，
           旁边又站着这个按钮，两个入口并列反而不知道该点哪个 -->
      <el-popover v-model:visible="overrideVisible" trigger="click" :width="330"
                  placement="bottom-end">
        <template #reference>
          <el-button size="small" :type="runOptionCount ? 'primary' : ''">
            <el-icon style="margin-right: 4px; vertical-align: -2px"><Setting /></el-icon>
            校验参数<span v-if="runOptionCount">（{{ runOptionCount }}）</span>
          </el-button>
        </template>
        <CheckOverrideForm ref="overrideRef" v-model="checkOverride"
                           v-model:refresh="refreshThisRun"
                           @summary="overrideSummary = $event" />
      </el-popover>
      <el-button size="small" :icon="Refresh" :disabled="checking" @click="checkAll()">
        全量校验
      </el-button>
      <el-button size="small" :icon="Upload" @click="exportVisible = true">导出/订阅</el-button>
      <el-button size="small" :icon="Download" @click="importVisible = true">导入</el-button>
      <el-button size="small" :icon="Delete" @click="trashVisible = true">回收站</el-button>
      <el-button size="small" @click="tagManagerVisible = true">标签管理</el-button>
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
                 @click="checkSources(selected.map((r) => r.source_url))">
        校验选中
      </el-button>
      <el-button size="small" type="danger" plain @click="removeSelected">移入回收站</el-button>
      <el-button size="small" link @click="clearSelection">取消选择</el-button>
      <span v-if="isMobile" class="grow" />
      <el-button v-if="isMobile" size="small" type="primary" :icon="Upload"
                 @click="exportVisible = true">导出/订阅</el-button>
    </div>

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
            </div>
            <div class="host mono">{{ row.source_url }}</div>
            <div class="meta">
              <el-tag size="small">{{ typeLabel(row.source_type) }}</el-tag>
              <el-tag v-if="row.health" size="small" :type="healthType[row.health] || 'info'">
                {{ row.health }}
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
          <el-button link :icon="Refresh" :loading="checking"
                     @click.stop="checkSources([row.source_url])" />
          <el-button link :icon="Filter" @click.stop="openEdit(row)" />
        </div>
        <el-empty v-if="!loading && !rows.length" description="没有匹配的书源" :image-size="80" />
      </div>

      <el-table v-else ref="tableRef" :data="rows" v-loading="loading" border stripe size="small"
                height="100%" @selection-change="(v) => (selected = v)">
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
              {{ row.health }}
            </el-tag>
            <span v-else class="muted">未校验</span>
          </template>
        </el-table-column>
        <el-table-column prop="stars" label="★" width="56" align="center" />
        <el-table-column label="目录/正文" width="106" align="center">
          <template #default="{ row }">
            <span class="muted nowrap">
              {{ row.toc_complete === 1 ? "目录✓" : row.toc_complete === 0 ? "目录✗" : "—" }}
              {{ row.content_ok === 1 ? " 正文✓" : row.content_ok === 0 ? " 正文✗" : "" }}
            </span>
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
        <el-table-column label="操作" width="90" align="center">
          <template #default="{ row }">
            <el-button link size="small" :loading="checking"
                       @click="checkSources([row.source_url])">校验</el-button>
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
            <el-option v-for="h in HEALTH" :key="h.value" :value="h.value" :label="h.label" />
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
          <el-button :icon="Refresh" :loading="checking" @click="checkAll(); filterVisible = false">
            全量校验<span v-if="runOptionCount">（{{ runOptionCount }}）</span>
          </el-button>
          <el-button @click="overrideDialog = true">
            校验参数<span v-if="runOptionCount">（{{ runOptionCount }}）</span>
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
    <JobsDrawer ref="jobsRef" v-model="jobsVisible" @running-change="jobBadge = $event" />

    <!-- 移动端的校验参数覆盖。用 dialog 而不是在 btt 抽屉里展开一块：那个抽屉是
         size="auto"，高度在打开时就定好了，往里插一块会伸缩的表单是个不必要的赌注 -->
    <el-dialog v-model="overrideDialog" title="校验参数（本次）" width="90%" append-to-body>
      <CheckOverrideForm ref="overrideDialogRef" v-model="checkOverride"
                         v-model:refresh="refreshThisRun"
                         @summary="overrideSummary = $event" />
      <div class="muted" style="margin-top: 8px">仅对本次校验生效，不改全局设置</div>
      <template #footer>
        <el-button @click="overrideDialog = false">关闭</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
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
