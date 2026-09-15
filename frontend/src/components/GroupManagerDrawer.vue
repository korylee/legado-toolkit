<script setup>
import { ref, computed, watch } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import {
  deleteTag,
  listTags,
  mergeTags,
  normalizeTags,
  renameTag,
} from "../api/sources";

const props = defineProps({ modelValue: { type: Boolean, default: false } });
const emit = defineEmits(["update:modelValue", "changed"]);

const visible = computed({
  get: () => props.modelValue,
  set: (v) => emit("update:modelValue", v),
});

const loading = ref(false);
const tags = ref([]);
const renameVisible = ref(false);
const renameForm = ref({ old: "", new: "" });
const mergeVisible = ref(false);
const mergeForm = ref({ sources: "", target: "" });

async function load() {
  loading.value = true;
  try {
    tags.value = await listTags();
  } catch (e) {
    ElMessage.error("加载标签失败: " + e.message);
  } finally {
    loading.value = false;
  }
}

watch(() => props.modelValue, (v) => { if (v) load(); });

function openRename(row) {
  renameForm.value = { old: row.tag, new: row.tag };
  renameVisible.value = true;
}

async function doRename() {
  try {
    const n = await renameTag(renameForm.value.old, renameForm.value.new);
    ElMessage.success("已更新 " + n.updated + " 条");
    renameVisible.value = false;
    load();
    emit("changed");
  } catch (e) {
    ElMessage.error(e.message);
  }
}

function openMerge(row) {
  mergeForm.value = { sources: row ? row.tag : "", target: "" };
  mergeVisible.value = true;
}

async function doMerge() {
  const sources = mergeForm.value.sources
    .split(/[,，;；|]+/)
    .map((s) => s.trim())
    .filter(Boolean);
  try {
    const n = await mergeTags(sources, mergeForm.value.target);
    ElMessage.success("已更新 " + n.updated + " 条");
    mergeVisible.value = false;
    load();
    emit("changed");
  } catch (e) {
    ElMessage.error(e.message);
  }
}

async function doDelete(row) {
  try {
    await ElMessageBox.confirm("从所有源移除标签 " + row.tag + "？", "删除标签", { type: "warning" });
    const n = await deleteTag(row.tag);
    ElMessage.success("已移除 " + n.updated + " 条");
    load();
    emit("changed");
  } catch (e) {
    if (e !== "cancel" && e !== "close") ElMessage.error(e.message || String(e));
  }
}

async function doNormalize() {
  try {
    const n = await normalizeTags();
    ElMessage.success("已规范化 " + n.updated + " 条");
    load();
    emit("changed");
  } catch (e) {
    ElMessage.error(e.message);
  }
}
</script>

<template>
  <el-drawer v-model="visible" title="分组管理" direction="rtl" size="520px" destroy-on-close>
    <div class="bar">
      <el-button size="small" :loading="loading" @click="load">刷新</el-button>
      <el-button size="small" type="primary" @click="openMerge(null)">合并标签</el-button>
      <el-button size="small" @click="doNormalize">规范化全部</el-button>
    </div>
    <el-table :data="tags" v-loading="loading" border size="small" style="margin-top: 10px">
      <el-table-column prop="tag" label="标签" min-width="140" show-overflow-tooltip />
      <el-table-column label="类型" width="80" align="center">
        <template #default="{ row }">
          <el-tag size="small" :type="row.kind === 'system' ? 'info' : 'success'">
            {{ row.kind === "system" ? "系统" : "用户" }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="count" label="数量" width="70" align="center" />
      <el-table-column label="操作" width="190" align="right">
        <template #default="{ row }">
          <template v-if="row.editable">
            <el-button link size="small" @click="openRename(row)">重命名</el-button>
            <el-button link size="small" @click="openMerge(row)">合并</el-button>
            <el-button link size="small" type="danger" @click="doDelete(row)">删除</el-button>
          </template>
          <span v-else class="muted">系统只读</span>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog v-model="renameVisible" title="重命名用户标签" width="360px">
      <el-form label-width="60px" size="small">
        <el-form-item label="原标签"><el-input v-model="renameForm.old" disabled /></el-form-item>
        <el-form-item label="新标签"><el-input v-model="renameForm.new" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="renameVisible = false">取消</el-button>
        <el-button type="primary" @click="doRename">确定</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="mergeVisible" title="合并用户标签" width="420px">
      <el-form label-width="70px" size="small">
        <el-form-item label="来源标签">
          <el-input v-model="mergeForm.sources" placeholder="用逗号分隔，如 原创,自写" />
        </el-form-item>
        <el-form-item label="目标标签">
          <el-input v-model="mergeForm.target" placeholder="如 原创" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="mergeVisible = false">取消</el-button>
        <el-button type="primary" @click="doMerge">确定</el-button>
      </template>
    </el-dialog>
  </el-drawer>
</template>
