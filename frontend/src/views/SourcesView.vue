<script setup>
import { ref, reactive, computed, onMounted, onUnmounted } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { Search, Plus, Upload, Download, Delete, Filter, Refresh } from "@element-plus/icons-vue";
import { listSources, listGroups, patchTags, deleteSources, listTags } from "../api/sources";
import { api, subscribeJob } from "../api/client";
import { splitTags } from "../utils/tags";
import { useMobile } from "../composables/useMobile";
import SourceEditDialog from "../components/SourceEditDialog.vue";
import TrashDrawer from "../components/TrashDrawer.vue";
import ExportDrawer from "../components/ExportDrawer.vue";
import ImportDialog from "../components/ImportDialog.vue";
import GroupManagerDrawer from "../components/GroupManagerDrawer.vue";

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

const query = reactive({
  q: "", type: null, health: "", group: "", tag: "",
  order: "-stars", limit: 50, offset: 0,
});

const TYPES = [
  { value: 0, label: "📖小说" }, { value: 1, label: "🎧听书" },
  { value: 2, label: "🎨漫画" }, { value: 3, label: "🎬视频" },
];
const HEALTH = [
  { value: "ok", label: "✅可用" }, { value: "dead", label: "❌失效" },
  { value: "auth", label: "🔒需验证" }, { value: "gfw", label: "🌐需翻墙" },
];
const healthType = { ok: "success", dead: "danger", auth: "warning", gfw: "info" };
const typeLabel = (v) => (TYPES.find((t) => t.value === v) || {}).label || ("类型" + v);

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

async function checkSources(urls = []) {
  if (checking.value) return ElMessage.warning("已有校验任务在运行");
  checking.value = true;
  try {
    const r = await api.post("/jobs", {
      kind: "check",
      payload: { urls, probe_depth: 1 },
    });
    ElMessage.success("已提交校验任务 " + r.job_id);
    if (stopCheck) stopCheck();
    stopCheck = subscribeJob(
      r.job_id,
      () => {},
      async (data) => {
        checking.value = false;
        stopCheck = null;
        if (data.status === "done") {
          ElMessage.success("校验完成");
          await load();
          try { tags.value = await listTags(); } catch (e) { /* 忽略 */ }
        } else {
          ElMessage.error("校验任务失败: " + (data.status || "unknown"));
        }
      },
    );
  } catch (e) {
    checking.value = false;
    ElMessage.error("提交校验失败: " + e.message);
  }
}

async function checkAll() {
  try {
    await ElMessageBox.confirm(
      "将校验全部未删除书源，可能耗时数分钟，确认继续？",
      "全量校验", { type: "warning" });
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
    <!-- 桌面：完整筛选栏 -->
    <div class="bar page-toolbar desktop-only" v-if="!isMobile">
      <el-input class="w-search" v-model="query.q" placeholder="搜名称 / 域名" clearable
                size="small" :prefix-icon="Search" @keyup.enter="search" />
      <el-select class="w-type" v-model="query.type" placeholder="类型" clearable size="small">
        <el-option v-for="t in TYPES" :key="t.value" :value="t.value" :label="t.label" />
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
      <el-button size="small" :icon="Refresh" :loading="checking" @click="checkAll">全量校验</el-button>
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
              <el-tag v-if="row.group_name" size="small" type="info">{{ row.group_name }}</el-tag>
              <el-tag v-if="row.system_tags_locked" size="small" type="warning">手动</el-tag>
              <el-tag v-for="t in userTagsOf(row)" :key="t" size="small" type="success">
                {{ t }}
              </el-tag>
              <span v-if="!row.group_name && !userTagsOf(row).length" class="muted">(无标签)</span>
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
            <el-tag v-if="row.group_name" size="small" type="info">{{ row.group_name }}</el-tag>
            <el-tag v-if="row.system_tags_locked" size="small" type="warning">手动</el-tag>
            <el-tag v-for="t in userTagsOf(row)" :key="t" size="small" type="success">
              {{ t }}
            </el-tag>
            <span v-if="!row.group_name && !userTagsOf(row).length" class="muted">(无标签)</span>
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
            <el-option v-for="t in TYPES" :key="t.value" :value="t.value" :label="t.label" />
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
  </div>
</template>
