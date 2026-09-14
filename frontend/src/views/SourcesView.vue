<script setup>
import { ref, reactive, computed, onMounted } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { Search, Plus, Upload, Download, Delete, Filter } from "@element-plus/icons-vue";
import { listSources, listGroups, patchGroup, deleteSources } from "../api/sources";
import { useMobile } from "../composables/useMobile";
import SourceEditDialog from "../components/SourceEditDialog.vue";
import TrashDrawer from "../components/TrashDrawer.vue";
import ExportDrawer from "../components/ExportDrawer.vue";
import ImportDialog from "../components/ImportDialog.vue";

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
const batchGroup = ref("");

const query = reactive({
  q: "", type: null, health: "", group: "",
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
  return [f.q, f.type !== null && f.type !== "", f.health, f.group].filter(Boolean).length;
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
  Object.assign(query, { q: "", type: null, health: "", group: "", order: "-stars", offset: 0 });
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

async function changeGroup(row) {
  try {
    await patchGroup(row.source_url, row.group_name);
    ElMessage.success("分组已更新");
  } catch (e) {
    ElMessage.error(e.message);
    load();
  }
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

async function applyBatchGroup() {
  if (!selected.value.length) return ElMessage.warning("先勾选源");
  if (!batchGroup.value) return ElMessage.warning("先填要设置的分组");
  try {
    const n = selected.value.length;
    for (const r of selected.value) await patchGroup(r.source_url, batchGroup.value);
    ElMessage.success("已更新 " + n + " 条分组");
    batchGroup.value = "";
    clearSelection();
    load();
    groups.value = await listGroups();
  } catch (e) {
    ElMessage.error(e.message);
  }
}

function openNew() { dlgUrl.value = ""; dlgVisible.value = true; }
function openEdit(row) { dlgUrl.value = row.source_url; dlgVisible.value = true; }
function onSaved() { dlgVisible.value = false; load(); }

onMounted(async () => {
  load();
  try { groups.value = await listGroups(); } catch (e) { /* 忽略 */ }
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
      <el-button size="small" :icon="Upload" @click="exportVisible = true">导出</el-button>
      <el-button size="small" :icon="Download" @click="importVisible = true">导入</el-button>
      <el-button size="small" :icon="Delete" @click="trashVisible = true">回收站</el-button>
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
        <el-input class="w-batch" v-model="batchGroup" placeholder="输入分组，如 原创"
                  size="small" @keyup.enter="applyBatchGroup" />
        <el-button size="small" :disabled="!batchGroup" @click="applyBatchGroup">应用分组</el-button>
      </template>
      <el-divider direction="vertical" />
      <el-button size="small" type="danger" plain @click="removeSelected">移入回收站</el-button>
      <el-button size="small" link @click="clearSelection">取消选择</el-button>
      <span v-if="isMobile" class="grow" />
      <el-button v-if="isMobile" size="small" type="primary" :icon="Upload"
                 @click="exportVisible = true">导出</el-button>
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
            <div class="grp">{{ row.group_name || "(无分组)" }}</div>
          </div>
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
        <el-table-column label="分组" min-width="170">
          <template #default="{ row }">
            <el-input v-model="row.group_name" size="small" @change="changeGroup(row)" />
          </template>
        </el-table-column>
        <el-table-column prop="source_url" label="域名" min-width="190" show-overflow-tooltip>
          <template #default="{ row }"><span class="mono">{{ row.source_url }}</span></template>
        </el-table-column>
        <el-table-column prop="checked_at" label="校验时间" width="146" />
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
          <el-button :icon="Delete" @click="trashVisible = true; filterVisible = false">
            回收站
          </el-button>
        </div>
      </div>
    </el-drawer>

    <SourceEditDialog v-model="dlgVisible" :source-url="dlgUrl" @saved="onSaved" />
    <TrashDrawer v-model="trashVisible" @changed="load" />
    <ExportDrawer v-model="exportVisible" :selected="selected"
                  :filter="query" :filtered-total="total" />
    <ImportDialog v-model="importVisible" @imported="load" />
  </div>
</template>
