<script setup>
// 「设置」抽屉的「模型」页签。原 LLMSettingsDrawer.vue 的内容原样搬过来，
// 只去掉外层 el-drawer 与 modelValue 桥接——挂载时机交给 SettingsDrawer 的
// 页签 lazy 控制，不再需要 watch(modelValue)。
//
// 表单只有五个参数：协议 / Base URL / API Key / 模型 / 超时。其余字段
// （温度、max_tokens、启用、Key 环境变量）没有消费方，后端也不再存。
import { ref, onMounted } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import {
  activateLLMProfile,
  createLLMProfile,
  deleteLLMProfile,
  getLLMStatus,
  listLLMProfiles,
  testLLMProfile,
  updateLLMProfile,
} from "../api/llm";

const PROTOCOLS = [
  { value: "openai", label: "OpenAI 兼容（/chat/completions）" },
  { value: "responses", label: "OpenAI Responses（/responses）" },
  { value: "anthropic", label: "Anthropic（/messages）" },
];
const PROTOCOL_LABEL = Object.fromEntries(PROTOCOLS.map((p) => [p.value, p.label]));

const profiles = ref([]);
const status = ref(null);
const loading = ref(false);
const saving = ref(false);
const editingId = ref("");
const editVisible = ref(false);
const testingId = ref("");
const lastTest = ref(null);

function blankProfile() {
  return { id: "", protocol: "openai", base_url: "", api_key: "", model: "", timeout: 90 };
}
const form = ref(blankProfile());

async function load() {
  loading.value = true;
  try {
    const [p, s] = await Promise.all([listLLMProfiles(), getLLMStatus()]);
    profiles.value = p || [];
    status.value = s || null;
  } catch (e) {
    ElMessage.error("加载模型配置失败: " + e.message);
  } finally {
    loading.value = false;
  }
}

onMounted(load);

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

async function save() {
  if (!form.value.base_url) return ElMessage.warning("请填 Base URL");
  if (!form.value.model) return ElMessage.warning("请填模型名");
  const payload = {
    protocol: form.value.protocol,
    base_url: form.value.base_url,
    api_key: form.value.api_key,
    model: form.value.model,
    timeout: form.value.timeout,
  };
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
  } catch (e) {
    ElMessage.error("保存失败: " + e.message);
  } finally {
    saving.value = false;
  }
}

async function activate(row) {
  try {
    await activateLLMProfile(row.id);
    ElMessage.success("已设为默认: " + row.model);
    await load();
  } catch (e) {
    ElMessage.error(e.message);
  }
}

async function doTest(row) {
  testingId.value = row.id;
  lastTest.value = null;
  try {
    const r = await testLLMProfile(row.id);
    lastTest.value = { ...r, model: row.model };
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
    await ElMessageBox.confirm("删除模型配置 " + row.model + "？", "删除配置",
                               { type: "warning" });
    await deleteLLMProfile(row.id);
    ElMessage.success("已删除");
    await load();
  } catch (e) { /* 取消 */ }
}
</script>

<template>
  <div>
    <div class="toolbar">
      <el-button size="small" :loading="loading" @click="load">刷新</el-button>
      <el-button size="small" type="primary" @click="openNew">新增配置</el-button>
      <span class="grow" />
      <el-tag v-if="status && status.active" size="small" type="success">
        默认: {{ status.active.model }}
      </el-tag>
    </div>

    <el-alert v-if="lastTest" :closable="false" show-icon style="margin-top: 10px"
              :type="lastTest.ok ? 'success' : 'error'"
              :title="(lastTest.model || '') + (lastTest.ok ? ' 连接成功' : ' 连接失败')"
              :description="lastTest.ok ? (lastTest.latency_ms + 'ms · ' + lastTest.reply) : lastTest.error" />

    <el-table :data="profiles" v-loading="loading" border size="small" style="margin-top: 12px">
      <el-table-column label="模型" min-width="180">
        <template #default="{ row }">
          <b>{{ row.model }}</b>
          <el-tag v-if="status && status.active && status.active.id === row.id"
                  size="small" type="success" style="margin-left: 6px">默认</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="接口 / 地址" min-width="220">
        <template #default="{ row }">
          <div>{{ PROTOCOL_LABEL[row.protocol] || row.protocol }}</div>
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
      <el-table-column label="超时" width="80" align="center">
        <template #default="{ row }">{{ row.timeout }}s</template>
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

    <!-- append-to-body 必须保留：这个弹窗嵌在 el-drawer > el-tabs 里，
         靠它脱离抽屉的层级与 overflow 上下文 -->
    <el-dialog v-model="editVisible" :title="editingId ? '编辑模型配置' : '新增模型配置'"
               width="520px" append-to-body>
      <el-form label-width="96px" size="small">
        <el-form-item label="协议">
          <el-select v-model="form.protocol" style="width: 100%">
            <el-option v-for="p in PROTOCOLS" :key="p.value" :value="p.value"
                       :label="p.label" />
          </el-select>
        </el-form-item>
        <el-form-item label="Base URL">
          <el-input v-model="form.base_url" placeholder="https://api.deepseek.com/v1" />
        </el-form-item>
        <el-form-item label="API Key">
          <el-input v-model="form.api_key" type="password" show-password
                    :placeholder="editingId ? '留空表示不修改' : 'sk-...'" />
        </el-form-item>
        <el-form-item label="模型">
          <el-input v-model="form.model" placeholder="deepseek-chat" />
        </el-form-item>
        <el-form-item label="超时秒">
          <el-input-number v-model="form.timeout" :min="5" :max="600" controls-position="right" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="editVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="save">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>
