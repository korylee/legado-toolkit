<script setup>
import { ref, reactive, onMounted } from "vue";
import { useRouter } from "vue-router";
import { ElMessage, ElMessageBox } from "element-plus";
import { listSources, listGroups, patchGroup, deleteSources } from "../api/sources";

const router = useRouter();
const loading = ref(false);
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
      "将删除 " + selected.value.length + " 条源。注意：目前是硬删除，不可撤销。",
      "确认删除", { type: "warning" });
    const res = await deleteSources(selected.value.map((r) => r.source_url));
    ElMessage.success("已删除 " + res.deleted + " 条");
    selected.value = [];
    load();
  } catch (e) { /* 取消 */ }
}

onMounted(async () => {
  load();
  try { groups.value = await listGroups(); } catch (e) { /* 忽略 */ }
});
</script>

<template>
  <div class="page page-flex">
    <div class="toolbar page-toolbar">
      <el-input v-model="query.q" placeholder="搜名称 / 域名" style="width: 200px"
                clearable @keyup.enter="search" />
      <el-select v-model="query.type" placeholder="类型" clearable style="width: 116px">
        <el-option v-for="t in TYPES" :key="t.value" :value="t.value" :label="t.label" />
      </el-select>
      <el-select v-model="query.health" placeholder="健康度" clearable style="width: 124px">
        <el-option v-for="h in HEALTH" :key="h.value" :value="h.value" :label="h.label" />
      </el-select>
      <el-select v-model="query.group" placeholder="分组" clearable filterable style="width: 190px">
        <el-option v-for="g in groups" :key="g.group" :value="g.group"
                   :label="g.group + ' (' + g.count + ')'" />
      </el-select>
      <el-select v-model="query.order" style="width: 130px">
        <el-option value="-stars" label="星级 ↓" />
        <el-option value="stars" label="星级 ↑" />
        <el-option value="-checked_at" label="校验时间 ↓" />
        <el-option value="name" label="名称 ↑" />
      </el-select>
      <el-button type="primary" @click="search">查询</el-button>
      <el-button @click="reset">重置</el-button>
      <span class="grow" />
      <el-button @click="router.push('/source/new')">新建源</el-button>
      <el-button type="danger" plain :disabled="!selected.length" @click="removeSelected">
        删除 ({{ selected.length }})
      </el-button>
    </div>

    <div class="page-fill">
      <el-table :data="rows" v-loading="loading" border stripe size="small" height="100%"
                @selection-change="(v) => (selected = v)">
        <el-table-column type="selection" width="42" />
        <el-table-column prop="name" label="名称" min-width="170" show-overflow-tooltip>
          <template #default="{ row }">
            <a href="#" @click.prevent="router.push({ name: 'source-edit', query: { url: row.source_url } })">
              {{ row.name || "(无名)" }}
            </a>
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
      </el-table>
    </div>

    <el-pagination class="page-footer" background
                   layout="total, sizes, prev, pager, next, jumper"
                   :total="total" :page-size="query.limit"
                   :page-sizes="[20, 50, 100, 200]"
                   @current-change="onPage"
                   @size-change="(s) => { query.limit = s; search(); }" />
  </div>
</template>
