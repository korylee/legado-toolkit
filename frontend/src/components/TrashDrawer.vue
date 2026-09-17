<script setup>
// 回收站：软删除的源在这里，可恢复。
//
// 恢复的粒度是**行 id**：同一个 URL 可以有多份历史版本（「删了再导入」时旧版就
// 留在这儿），按 URL 恢复会含糊。已经有在用版本的 URL 会被拒绝并单独报出来。
//
// 清空是**唯一**的硬删除路径，后端先落快照再删。
import { ref, computed, watch } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { listDeleted, purgeTrash, restoreSources } from "../api/sources";
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

/** 统一的恢复反馈。`blocked` 必须说出来：那是「这个 URL 已经有在用的版本」，
 *  用户得先删掉在用的那条才能恢复——静默跳过会让人以为恢复了。 */
function reportRestore(res) {
  if (res.restored) ElMessage.success("已恢复 " + res.restored + " 条");
  const blocked = res.blocked || [];
  if (blocked.length) {
    ElMessage.warning(blocked.length + " 条没能恢复：它们的 URL 已经有在用的版本"
                      + "（先删掉在用的那条，再回来恢复）");
  } else if (!res.restored) {
    ElMessage.info("没有可恢复的行");
  }
}

async function restoreOne(row) {
  try {
    reportRestore(await restoreSources([row.id]));
    await load();
    emit("changed");
  } catch (e) {
    ElMessage.error(e.message);
  }
}

async function restoreSelected() {
  if (!selected.value.length) return ElMessage.warning("先勾选要恢复的源");
  try {
    reportRestore(await restoreSources(selected.value.map((r) => r.id)));
    selected.value = [];
    await load();
    emit("changed");
  } catch (e) {
    ElMessage.error(e.message);
  }
}

/** 清空回收站：**不可逆**，所以要说清两件事——删多少条、快照落在哪。
 *  后端先落快照再删（写失败就不删），路径随返回体回来。 */
async function purgeAll() {
  if (!total.value) return ElMessage.warning("回收站是空的");
  try {
    await ElMessageBox.confirm(
      "将把回收站里的 " + total.value + " 条源彻底删掉，这一步不可撤销。"
      + "删除前会自动落一份快照到 data/backups/，需要时可以从那里捞回来。",
      "清空回收站",
      { type: "warning", confirmButtonText: "确认清空" });
  } catch (e) {
    return;   // 取消
  }
  try {
    const res = await purgeTrash();
    ElMessage.success("已清空 " + res.purged + " 条；快照 " + res.snapshot);
    selected.value = [];
    await load();
    emit("changed");
  } catch (e) {
    ElMessage.error("清空失败: " + e.message);
  }
}
</script>

<template>
  <el-drawer v-model="visible" title="回收站" size="760px" destroy-on-close>
    <el-alert type="warning" :closable="false" show-icon style="margin-bottom: 12px">
      <template #title>
        回收站里的源<b>不会被导出到 App</b>。同一个 URL 可以有多份历史版本
        （删了再导入新版时，旧版会留在这儿）。「清空回收站」是彻底删除，
        删之前会自动落一份快照到 <code>data/backups/purged_*.json</code>。
      </template>
    </el-alert>

    <div class="toolbar" style="margin-bottom: 10px">
      <el-button @click="load">刷新</el-button>
      <el-button type="success" :disabled="!selected.length" @click="restoreSelected">
        恢复选中 ({{ selected.length }})
      </el-button>
      <span class="grow" />
      <span class="muted">共 {{ total }} 条</span>
      <el-button type="danger" plain :disabled="!total" @click="purgeAll">清空回收站</el-button>
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
             : row.source_type === 3 ? "📥下载" : "📖小说" }}
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
