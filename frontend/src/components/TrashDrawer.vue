<script setup>
// 回收站：软删除的源在这里，可恢复。
// 彻底删除不提供 UI —— 快照已写到 data/backups/deleted_<时间戳>.json，
// 需要真正清掉时由使用者在该文件层面处理。
import { ref, computed, watch } from "vue";
import { ElMessage } from "element-plus";
import { listDeleted, restoreSources } from "../api/sources";
import { useMobile } from "../composables/useMobile";

const props = defineProps({ modelValue: { type: Boolean, default: false } });
const emit = defineEmits(["update:modelValue", "changed"]);

const isMobile = useMobile();
const visible = computed({
  get: () => props.modelValue,
  set: (v) => emit("update:modelValue", v),
});
const rows = ref([]);
const total = ref(0);
const loading = ref(false);
const selected = ref([]);
const showHeight = computed(() => (isMobile.value ? "58vh" : "calc(100vh - 260px)"));

async function load() {
  loading.value = true;
  try {
    const res = await listDeleted(500, 0);
    rows.value = res.items;
    total.value = res.total;
  } catch (e) {
    ElMessage.error("加载回收站失败: " + e.message);
  } finally {
    loading.value = false;
  }
}

watch(() => props.modelValue, (v) => { if (v) load(); });

async function restoreOne(row) {
  try {
    const res = await restoreSources([row.source_url]);
    ElMessage.success("已恢复 " + res.restored + " 条");
    await load();
    emit("changed");
  } catch (e) {
    ElMessage.error(e.message);
  }
}

async function restoreSelected() {
  if (!selected.value.length) return ElMessage.warning("先勾选要恢复的源");
  try {
    const res = await restoreSources(selected.value.map((r) => r.source_url));
    ElMessage.success("已恢复 " + res.restored + " 条");
    selected.value = [];
    await load();
    emit("changed");
  } catch (e) {
    ElMessage.error(e.message);
  }
}
</script>

<template>
  <el-drawer v-model="visible" title="回收站" size="760px" destroy-on-close>
    <el-alert type="warning" :closable="false" show-icon style="margin-bottom: 12px">
      <template #title>
        回收站里的源<b>不会被导出到 App</b>。删除时的完整快照已写入
        <code>data/backups/deleted_&lt;时间戳&gt;.json</code>——
        需要彻底清掉时请在该文件层面处理，UI 不提供硬删除。
      </template>
    </el-alert>

    <div class="toolbar" style="margin-bottom: 10px">
      <el-button @click="load">刷新</el-button>
      <el-button type="success" :disabled="!selected.length" @click="restoreSelected">
        恢复选中 ({{ selected.length }})
      </el-button>
      <span class="grow" />
      <span class="muted">共 {{ total }} 条</span>
    </div>

    <el-table :data="rows" v-loading="loading" border size="small"
              :height="showHeight"
              @selection-change="(v) => (selected = v)">
      <el-table-column type="selection" width="42" />
      <el-table-column prop="name" label="名称" min-width="150" show-overflow-tooltip>
        <template #default="{ row }">{{ row.name || "(无名)" }}</template>
      </el-table-column>
      <el-table-column label="类型" width="88" align="center">
        <template #default="{ row }">
          {{ row.source_type === 2 ? "🎨漫画" : row.source_type === 1 ? "🎧听书"
             : row.source_type === 3 ? "🎬视频" : "📖小说" }}
        </template>
      </el-table-column>
      <el-table-column prop="group_name" label="原分组" min-width="150" show-overflow-tooltip />
      <el-table-column prop="deleted_at" label="删除时间" width="150" />
      <el-table-column label="操作" width="90" align="center">
        <template #default="{ row }">
          <el-button link type="success" size="small" @click="restoreOne(row)">恢复</el-button>
        </template>
      </el-table-column>
      <template #empty>
        <el-empty description="回收站是空的" :image-size="70" />
      </template>
    </el-table>
  </el-drawer>
</template>
