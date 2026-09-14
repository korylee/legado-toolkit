<script setup>
import { ref, reactive, onMounted } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { Search, Plus, Upload, Download, Delete } from "@element-plus/icons-vue";
import { listSources, listGroups, patchGroup, deleteSources } from "../api/sources";
import SourceEditDialog from "../components/SourceEditDialog.vue";
import TrashDrawer from "../components/TrashDrawer.vue";
import ExportDrawer from "../components/ExportDrawer.vue";
import ImportDialog from "../components/ImportDialog.vue";

const loading = ref(false);
const dlgVisible = ref(false);
const dlgUrl = ref("");
const batchGroup = ref("");
const trashVisible = ref(false);
const exportVisible = ref(false);
const importVisible = ref(false);
const tableRef = ref(null);
const rows = ref([]);
const total = ref(0);
const groups = ref([]);
const selected = ref([]);

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

async function changeGroup(row) {
  try {
    await patchGroup(row.source_url, row.group_name);
    ElMessage.success("分组已更新（同步写回 raw_json，导出不失真）");
  } catch (e) {
    ElMessage.error(e.message);
    load();
  }
}

async function removeSelected() {
  if (!selected.value.length) return ElMessage.warning("先勾选要删除的源");
  try {
    await ElMessageBox.confirm(
      "将把 " + selected.value.length + " 条源移入回收站。它们不会再被导出到 App，可随时恢复。",
      "移入回收站", { type: "warning" });
    const res = await deleteSources(selected.value.map((r) => r.source_url));
    ElMessage.success("已移入回收站 " + res.deleted + " 条，可在回收站恢复");
    selected.value = [];
    load();
  } catch (e) { /* 取消 */ }
}

function clearSelection() {
  selected.value = [];
  if (tableRef.value) tableRef.value.clearSelection();
}

function openNew() { dlgUrl.value = ""; dlgVisible.value = true; }
function openEdit(row) { dlgUrl.value = row.source_url; dlgVisible.value = true; }
function onSaved() { dlgVisible.value = false; load(); }

async function applyBatchGroup() {
  if (!selected.value.length) return ElMessage.warning("先勾选源");
  if (!batchGroup.value) return ElMessage.warning("先填要设置的分组");
  try {
    for (const r of selected.value) await patchGroup(r.source_url, batchGroup.value);
    const n = selected.value.length;
    ElMessage.success("已更新 " + n + " 条分组");
    clearSelection();
    load();
    groups.value = await listGroups();
  } catch (e) {
    ElMessage.error(e.message);
  }
}

onMounted(async () => {
  load();
  try { groups.value = await listGroups(); } catch (e) { /* 忽略 */ }
});
</script>

<template>
  <div class="page page-flex">
    <!-- 筛选栏：查询条件 + 全局操作 -->
    <div class="bar page-toolbar">
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

    <!-- 批量操作条：仅在有勾选时出现 -->
    <div class="batch-bar" v-if="selected.length">
      <span class="batch-text">已选 <b>{{ selected.length }}</b> 条</span>
      <el-divider direction="vertical" />
      <el-input class="w-batch" v-model="batchGroup" placeholder="输入分组，如 原创"
                size="small" @keyup.enter="applyBatchGroup" />
      <el-button size="small" :disabled="!batchGroup" @click="applyBatchGroup">应用分组</el-button>
      <el-divider direction="vertical" />
      <el-button size="small" type="danger" plain @click="removeSelected">移入回收站</el-button>
      <el-button size="small" link @click="clearSelection">取消选择</el-button>
    </div>

    <div class="page-fill">
      <el-table ref="tableRef" :data="rows" v-loading="loading" border stripe size="small" height="100%"
                @selection-change="(v) => (selected = v)">
        <el-table-column type="selection" width="42" />
        <el-table-column prop="name" label="名称" min-width="170" show-overflow-tooltip>
          <template #default="{ row }">
            <a href="#" @click.prevent="openEdit(row)">{{ row.name || "(无名)" }}</a>
          </template>
        </el-table-column>
        <el-table-column label="类型" width="88" align="center">
          <template #default="{ row }">
            {{ (TYPES.find((t) => t.value === row.source_type) || {}).label || row.source_type }}
          </template>
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
        <template #empty>
          <el-empty description="没有匹配的书源，试试放宽筛选条件" :image-size="80" />
        </template>
      </el-table>
    </div>

    <SourceEditDialog v-model="dlgVisible" :source-url="dlgUrl" @saved="onSaved" />
    <TrashDrawer v-model="trashVisible" @changed="load" />
    <ExportDrawer v-model="exportVisible" :selected="selected"
                  :filter="query" :filtered-total="total" />
    <ImportDialog v-model="importVisible" @imported="load" />

    <el-pagination class="page-footer" background
                   layout="total, sizes, prev, pager, next, jumper"
                   :total="total" :page-size="query.limit"
                   :page-sizes="[20, 50, 100, 200]"
                   @current-change="onPage"
                   @size-change="(s) => { query.limit = s; search(); }" />
  </div>
</template>
