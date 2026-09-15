<script setup>
import { ref, computed, watch } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import {
  activateLLMProfile,
  createLLMProfile,
  deleteLLMProfile,
  getLLMStatus,
  listLLMPresets,
  listLLMProfiles,
  testLLMProfile,
  updateLLMProfile,
} from "../api/llm";

const props = defineProps({ modelValue: { type: Boolean, default: false } });
const emit = defineEmits(["update:modelValue", "changed"]);

const visible = computed({
  get: () => props.modelValue,
  set: (v) => emit("update:modelValue", v),
});

const profiles = ref([]);
const presets = ref([]);
const status = ref(null);
const loading = ref(false);
const saving = ref(false);
const editingId = ref("");
const editVisible = ref(false);
const testingId = ref("");
const lastTest = ref(null);

function blankProfile() {
  return {
    id: "", name: "", provider: "openai-compatible", adapter: "openai-compatible",
    base_url: "", api_key: "", api_key_env: "", model: "",
    temperature: 0.2, timeout: 90, max_tokens: 0,
    extra_headers: {}, extra_body: {}, enabled: true, sort_order: 0,
  };
}
const form = ref(blankProfile());

async function load() {
  loading.value = true;
  try {
    const [p, ps, s] = await Promise.all([
      listLLMProfiles(), listLLMPresets(), getLLMStatus(),
    ]);
    profiles.value = p || [];
    presets.value = ps || [];
    status.value = s || null;
  } catch (e) {
    ElMessage.error("加载模型配置失败: " + e.message);
  } finally {
    loading.value = false;
  }
}

watch(() => props.modelValue, (v) => { if (v) load(); });

function openNew() {
  editingId.value = "";
  form.value = blankProfile();
  editVisible.value = true;
}

function openEdit(row) {
  editingId.value = row.id;
  form.value = { ...blankProfile(), ...row, api_key: "" };
  editVisible.value = true;
}

function applyPreset(name) {
  const preset = presets.value.find((p) => p.name === name || p.provider === name);
  if (!preset) return;
  form.value.provider = preset.provider;
  form.value.adapter = "openai-compatible";
  form.value.base_url = preset.base_url;
  form.value.model = preset.model;
  if (!form.value.name) form.value.name = preset.name;
}

async function save() {
  const payload = { ...form.value };
  if (!payload.name) return ElMessage.warning("请填名称");
  if (!payload.base_url) return ElMessage.warning("请填 Base URL");
  if (!payload.model) return ElMessage.warning("请填模型名");
  saving.value = true;
  try {
    if (editingId.value) {
      await updateLLMProfile(editingId.value, payload);
    } else {
      await createLLMProfile(payload);
    }
    ElMessage.success("已保存");
    editVisible.value = false;
    await load();
    emit("changed");
  } catch (e) {
    ElMessage.error("保存失败: " + e.message);
  } finally {
    saving.value = false;
  }
}

async function activate(row) {
  try {
    await activateLLMProfile(row.id);
    ElMessage.success("已设为默认: " + row.name);
    await load();
    emit("changed");
  } catch (e) {
    ElMessage.error(e.message);
  }
}

async function doTest(row) {
  testingId.value = row.id;
  lastTest.value = null;
  try {
    const r = await testLLMProfile(row.id);
    lastTest.value = { ...r, name: row.name };
    if (r.ok) ElMessage.success("连接成功: " + r.latency_ms + "ms");
    else ElMessage.error("连接失败: " + (r.error || "unknown"));
  } catch (e) {
    ElMessage.error("测试失败: " + e.message);
  } finally {
    testingId.value = "";
  }
}

async function remove(row) {
  try {
    await ElMessageBox.confirm("删除模型配置 " + row.name + "？", "删除配置",
                               { type: "warning" });
    await deleteLLMProfile(row.id);
    ElMessage.success("已删除");
    await load();
    emit("changed");
  } catch (e) { /* 取消 */ }
}
</script>

<template>
  <el-drawer v-model="visible" title="模型设置" direction="rtl" size="640px" destroy-on-close>
    <div class="toolbar">
      <el-button size="small" :loading="loading" @click="load">刷新</el-button>
      <el-button size="small" type="primary" @click="openNew">新增配置</el-button>
      <span class="grow" />
      <el-tag v-if="status && status.active" size="small" type="success">
        默认: {{ status.active.name }}
      </el-tag>
    </div>

    <el-alert v-if="lastTest" :closable="false" show-icon style="margin-top: 10px"
              :type="lastTest.ok ? 'success' : 'error'"
              :title="(lastTest.name || '') + (lastTest.ok ? ' 连接成功' : ' 连接失败')"
              :description="lastTest.ok ? (lastTest.latency_ms + 'ms · ' + lastTest.reply) : lastTest.error" />

    <el-table :data="profiles" v-loading="loading" border size="small" style="margin-top: 12px">
      <el-table-column prop="name" label="名称" min-width="120">
        <template #default="{ row }">
          <b>{{ row.name }}</b>
          <el-tag v-if="status && status.active && status.active.id === row.id"
                  size="small" type="success" style="margin-left: 6px">默认</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="模型 / 地址" min-width="220">
        <template #default="{ row }">
          <div>{{ row.model }}</div>
          <div class="muted mono">{{ row.base_url }}</div>
        </template>
      </el-table-column>
      <el-table-column label="Key" width="90" align="center">
        <template #default="{ row }">
          <el-tag size="small" :type="row.api_key_set ? 'success' : 'info'">
            {{ row.api_key_set ? row.api_key_masked : '未设置' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="220" align="right">
        <template #default="{ row }">
          <el-button link size="small" :loading="testingId === row.id"
                     @click="doTest(row)">测试</el-button>
          <el-button link size="small" @click="activate(row)">设为默认</el-button>
          <el-button link size="small" @click="openEdit(row)">编辑</el-button>
          <el-button link size="small" type="danger" @click="remove(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-empty v-if="!loading && !profiles.length" description="还没有模型配置" :image-size="70" />

    <el-dialog v-model="editVisible" :title="editingId ? '编辑模型配置' : '新增模型配置'"
               width="520px" append-to-body>
      <el-form label-width="96px" size="small">
        <el-form-item label="平台预设">
          <el-select placeholder="选择后自动填 Base URL / Model" clearable style="width: 100%"
                     @change="applyPreset">
            <el-option v-for="p in presets" :key="p.provider" :value="p.name"
                       :label="p.name + ' · ' + p.model" />
          </el-select>
        </el-form-item>
        <el-form-item label="名称"><el-input v-model="form.name" /></el-form-item>
        <el-form-item label="Base URL"><el-input v-model="form.base_url" /></el-form-item>
        <el-form-item label="API Key">
          <el-input v-model="form.api_key" type="password" show-password
                    :placeholder="editingId ? '留空表示不修改' : 'sk-...'" />
        </el-form-item>
        <el-form-item label="Key 环境变量">
          <el-input v-model="form.api_key_env" placeholder="可选，如 DEEPSEEK_API_KEY" />
        </el-form-item>
        <el-form-item label="模型"><el-input v-model="form.model" /></el-form-item>
        <el-form-item label="温度">
          <el-input-number v-model="form.temperature" :min="0" :max="2" :step="0.1"
                           controls-position="right" />
        </el-form-item>
        <el-form-item label="超时秒">
          <el-input-number v-model="form.timeout" :min="5" :max="600" controls-position="right" />
        </el-form-item>
        <el-form-item label="max_tokens">
          <el-input-number v-model="form.max_tokens" :min="0" controls-position="right" />
        </el-form-item>
        <el-form-item label="启用"><el-switch v-model="form.enabled" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="editVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="save">保存</el-button>
      </template>
    </el-dialog>
  </el-drawer>
</template>

<style scoped>
.toolbar { display: flex; align-items: center; gap: 8px; }
.grow { flex: 1 1 auto; }
</style>
