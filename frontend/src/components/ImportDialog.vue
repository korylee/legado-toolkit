<script setup>
// 书源导入：多种输入方式共用预览与导入流程
import { ref, computed } from "vue";
import { ElMessage } from "element-plus";
import { api } from "../api/client";

const props = defineProps({ modelValue: { type: Boolean, default: false } });
const emit = defineEmits(["update:modelValue", "imported"]);

const visible = computed({
  get: () => props.modelValue,
  set: (v) => emit("update:modelValue", v),
});
const file = ref(null);
const url = ref("");
const sourceName = ref("");
const pasteText = ref("");
const activeTab = ref("file");
const previewTab = ref("");
const resultTab = ref("");
const rawText = ref("");
const preview = ref(null);
const loading = ref(false);
const result = ref(null);

function clearPreview() {
  rawText.value = "";
  preview.value = null;
  result.value = null;
  previewTab.value = "";
  resultTab.value = "";
}

function onTabChange() {
  clearPreview();
}

function onFileChange(f) {
  file.value = f.raw;
  sourceName.value = f.name || "";
  clearPreview();
}
function beforeUpload() { return false; }   // 手动解析，不自动上传

//: 同 URL 但规则不同时怎么办。**只对「在用」的那行有意义**——回收站里的同 URL
//: 历史版本不构成冲突，导入会直接新建一行在用的（所以「先删掉旧的再导入新版」
//: 这条路现在也通）。默认 keep：外部源默认不该覆盖你已经在用的规则。
const strategy = ref("keep");

function setPreview(text, source, tab) {
  const data = JSON.parse(text);
  if (!Array.isArray(data)) throw new Error("不是 JSON 数组");
  rawText.value = text;
  sourceName.value = source || "";
  previewTab.value = tab;
  preview.value = {
    count: data.length,
    noUrl: data.filter((s) => !s || typeof s !== "object" || !s.bookSourceUrl).length,
    noSearch: data.filter((s) => !s || typeof s !== "object" || !s.searchUrl).length,
    types: data.reduce((acc, s) => {
      const k = s && typeof s === "object" ? (s.bookSourceType ?? 0) : "无效";
      acc[k] = (acc[k] || 0) + 1;
      return acc;
    }, {}),
  };
  return data.length;
}

async function parseLocal() {
  if (!file.value) return ElMessage.warning("先选文件");
  loading.value = true;
  try {
    const text = await file.value.text();
    const count = setPreview(text, file.value.name, "file");
    ElMessage.success("解析成功，" + count + " 条");
  } catch (e) {
    ElMessage.error("解析失败: " + e.message);
    preview.value = null;
  } finally {
    loading.value = false;
  }
}

async function parseUrl() {
  if (!url.value.trim()) return ElMessage.warning("先输入 JSON 链接");
  loading.value = true;
  clearPreview();
  try {
    const fetched = await api.post("/import/url", { url: url.value.trim() });
    const count = setPreview(fetched.content, fetched.source || url.value.trim(), "url");
    ElMessage.success("读取成功，" + count + " 条");
  } catch (e) {
    ElMessage.error("读取 JSON 链接失败：" + e.message);
  } finally {
    loading.value = false;
  }
}

function parsePaste() {
  if (!pasteText.value.trim()) return ElMessage.warning("先粘贴书源 JSON");
  clearPreview();
  try {
    const parsed = JSON.parse(pasteText.value);
    const data = Array.isArray(parsed) ? parsed : [parsed];
    const count = setPreview(JSON.stringify(data), "粘贴内容", "paste");
    ElMessage.success("解析成功，" + count + " 条");
  } catch (e) {
    ElMessage.error("解析失败: " + e.message);
  }
}

async function doImport() {
  if (!rawText.value || previewTab.value !== activeTab.value) return;
  loading.value = true;
  result.value = null;
  try {
    const r = await api.post("/import", {
      content: rawText.value,
      source: sourceName.value,
      conflict_strategy: strategy.value,
    });
    result.value = r;
    resultTab.value = activeTab.value;
    ElMessage.success("新增 " + r.new_count + " / 更新 " + (r.updated_count || 0)
                      + " / 重复 " + r.duplicate_count
                      + " / 冲突 " + r.conflict_count);
    emit("imported");
  } catch (e) {
    ElMessage.error("导入失败: " + e.message);
    result.value = { error: String(e.message) };
    resultTab.value = activeTab.value;
  } finally {
    loading.value = false;
  }
}
</script>

<template>
  <el-dialog v-model="visible" title="导入书源" width="620px" destroy-on-close>
    <el-tabs v-model="activeTab" @tab-change="onTabChange">
      <el-tab-pane label="文件" name="file">
        <el-upload drag :auto-upload="false" :limit="1" accept=".json"
                   :on-change="onFileChange" :before-upload="beforeUpload">
          <div style="padding: 18px 0">
            <div style="font-size: 30px">📄</div>
            <div>选择或拖入书源 JSON 文件</div>
            <div class="muted">包括阅读 App 导出的书源文件</div>
          </div>
        </el-upload>
        <div class="toolbar" style="margin-top: 12px">
          <el-button type="primary" :loading="loading" :disabled="!file" @click="parseLocal">
            解析预览
          </el-button>
        </div>
      </el-tab-pane>

      <el-tab-pane label="链接" name="url">
        <div class="muted" style="margin-bottom: 8px">读取 HTTPS JSON 链接，不会自动导入。</div>
        <div class="import-input-row">
          <el-input v-model="url" placeholder="https://example.com/sources.json"
                    clearable @keyup.enter="parseUrl" />
          <el-button type="primary" :loading="loading" :disabled="!url.trim()"
                     @click="parseUrl">读取预览</el-button>
        </div>
      </el-tab-pane>

      <el-tab-pane label="粘贴" name="paste">
        <div class="muted" style="margin-bottom: 8px">粘贴一个书源对象或书源 JSON 数组。</div>
        <el-input v-model="pasteText" type="textarea" :rows="9"
                  placeholder='[{"bookSourceName":"示例","bookSourceUrl":"https://…"}]' />
        <div class="toolbar" style="margin-top: 12px">
          <el-button type="primary" :loading="loading" :disabled="!pasteText.trim()"
                     @click="parsePaste">解析预览</el-button>
        </div>
      </el-tab-pane>

      <el-tab-pane label="书源仓库" name="repository">
        <el-empty description="书源仓库导入尚未开放">
          <template #image>
            <div class="repository-placeholder">书源仓库</div>
          </template>
          <template #description>
            <div>后续可从不同书源仓库中搜索、挑选并导入。</div>
          </template>
        </el-empty>
      </el-tab-pane>
    </el-tabs>

    <el-form v-if="activeTab !== 'repository'" label-width="104px" size="small"
             style="margin: 8px 0 4px">
      <el-form-item label="同 URL 不同规则">
        <el-radio-group v-model="strategy">
          <el-radio value="keep">保留现有的，导入的进待审</el-radio>
          <el-radio value="overwrite">用导入的覆盖</el-radio>
        </el-radio-group>
        <div class="muted">
          覆盖只换规则，不清掉用户标签；被替换的版本会留存以便回查。
        </div>
      </el-form-item>
    </el-form>

    <div v-if="preview && previewTab === activeTab" class="preview-block">
      <div class="muted import-source">来源：{{ sourceName }}</div>
      <el-descriptions :column="2" border size="small">
        <el-descriptions-item label="条目数">{{ preview.count }}</el-descriptions-item>
        <el-descriptions-item label="缺 bookSourceUrl">{{ preview.noUrl }}</el-descriptions-item>
        <el-descriptions-item label="无搜索规则">{{ preview.noSearch }}</el-descriptions-item>
        <el-descriptions-item label="类型分布">{{ JSON.stringify(preview.types) }}</el-descriptions-item>
      </el-descriptions>
    </div>

    <div v-if="preview && previewTab === activeTab" class="toolbar" style="margin-top: 12px">
      <el-button type="success" :loading="loading" :disabled="!rawText" @click="doImport">
        确认导入
      </el-button>
    </div>

    <el-descriptions v-if="result && resultTab === activeTab && !result.error"
                     :column="4" border size="small" style="margin-top: 12px">
      <el-descriptions-item label="新增">{{ result.new_count }}</el-descriptions-item>
      <el-descriptions-item label="已更新">{{ result.updated_count || 0 }}</el-descriptions-item>
      <el-descriptions-item label="重复">{{ result.duplicate_count }}</el-descriptions-item>
      <el-descriptions-item label="冲突待审">{{ result.conflict_count }}</el-descriptions-item>
    </el-descriptions>
    <el-alert v-else-if="result && resultTab === activeTab" type="error" :closable="false"
              style="margin-top: 10px" :title="result.error" />
  </el-dialog>
</template>

<style scoped>
.import-input-row {
  display: flex;
  width: 100%;
  gap: 8px;
}

.preview-block {
  margin-top: 12px;
}

.import-source {
  margin-bottom: 6px;
}

.repository-placeholder {
  display: grid;
  width: 72px;
  height: 72px;
  place-items: center;
  border-radius: 12px;
  background: var(--el-fill-color-light);
  color: var(--el-text-color-secondary);
  font-size: 13px;
}

@media (max-width: 560px) {
  .import-input-row {
    align-items: stretch;
    flex-direction: column;
  }
}
</style>
