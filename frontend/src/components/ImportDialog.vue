<script setup>
// 文件导入：对话框形态（原来也是独立页面）
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
const rawText = ref("");
const preview = ref(null);
const loading = ref(false);
const result = ref(null);

function onFileChange(f) {
  file.value = f.raw;
  rawText.value = "";
  preview.value = null;
  result.value = null;
}
function beforeUpload() { return false; }   // 手动解析，不自动上传

//: 同 URL 但规则不同时怎么办。**只对「在用」的那行有意义**——回收站里的同 URL
//: 历史版本不构成冲突，导入会直接新建一行在用的（所以「先删掉旧的再导入新版」
//: 这条路现在也通）。默认 keep：外部源默认不该覆盖你已经在用的规则。
const strategy = ref("keep");

async function parseLocal() {
  if (!file.value) return ElMessage.warning("先选文件");
  loading.value = true;
  try {
    const text = await file.value.text();
    const data = JSON.parse(text);
    if (!Array.isArray(data)) throw new Error("不是 JSON 数组");
    rawText.value = text;
    preview.value = {
      count: data.length,
      noUrl: data.filter((s) => !s.bookSourceUrl).length,
      noSearch: data.filter((s) => !s.searchUrl).length,
      types: data.reduce((acc, s) => {
        const k = s.bookSourceType ?? 0;
        acc[k] = (acc[k] || 0) + 1;
        return acc;
      }, {}),
    };
    ElMessage.success("解析成功，" + data.length + " 条");
  } catch (e) {
    ElMessage.error("解析失败: " + e.message);
    preview.value = null;
  } finally {
    loading.value = false;
  }
}

async function doImport() {
  if (!rawText.value) return;
  loading.value = true;
  result.value = null;
  try {
    const r = await api.post("/import", {
      content: rawText.value,
      source: file.value ? file.value.name : "",
      conflict_strategy: strategy.value,
    });
    result.value = r;
    ElMessage.success("新增 " + r.new_count + " / 更新 " + (r.updated_count || 0)
                      + " / 重复 " + r.duplicate_count
                      + " / 冲突 " + r.conflict_count);
    emit("imported");
  } catch (e) {
    ElMessage.error("导入失败: " + e.message);
    result.value = { error: String(e.message) };
  } finally {
    loading.value = false;
  }
}
</script>

<template>
  <el-dialog v-model="visible" title="导入书源" width="620px" destroy-on-close>
    <el-alert type="info" :closable="false" show-icon style="margin-bottom: 12px"
              title="走安全导入：新 URL 进待校验；同 URL 规则冲突按下面的策略处理。" />

    <el-form label-width="104px" size="small" style="margin-bottom: 4px">
      <el-form-item label="同 URL 不同规则">
        <el-radio-group v-model="strategy">
          <el-radio value="keep">保留现有的，导入的进待审</el-radio>
          <el-radio value="overwrite">用导入的覆盖</el-radio>
        </el-radio-group>
        <div class="muted">
          覆盖只换规则，**不会清掉你打的用户标签**；被替换掉的那份会留在
          <code>data/imports/conflicts/</code> 里可回查。
        </div>
      </el-form-item>
    </el-form>

    <el-upload drag :auto-upload="false" :limit="1" accept=".json"
               :on-change="onFileChange" :before-upload="beforeUpload">
      <div style="padding: 18px 0">
        <div style="font-size: 30px">📄</div>
        <div>把 JSON 文件拖进来，或<em>点击选择</em></div>
      </div>
    </el-upload>

    <div class="toolbar" style="margin-top: 12px">
      <el-button type="primary" :loading="loading" @click="parseLocal">本地解析预览</el-button>
      <el-button type="success" :loading="loading" :disabled="!rawText" @click="doImport">
        确认导入
      </el-button>
    </div>

    <el-descriptions v-if="preview" :column="2" border size="small" style="margin-top: 12px">
      <el-descriptions-item label="条目数">{{ preview.count }}</el-descriptions-item>
      <el-descriptions-item label="缺 bookSourceUrl">{{ preview.noUrl }}</el-descriptions-item>
      <el-descriptions-item label="无搜索规则">{{ preview.noSearch }}</el-descriptions-item>
      <el-descriptions-item label="类型分布">{{ JSON.stringify(preview.types) }}</el-descriptions-item>
    </el-descriptions>

    <el-descriptions v-if="result && !result.error" :column="4" border size="small"
                     style="margin-top: 12px">
      <el-descriptions-item label="新增">{{ result.new_count }}</el-descriptions-item>
      <el-descriptions-item label="已更新">{{ result.updated_count || 0 }}</el-descriptions-item>
      <el-descriptions-item label="重复">{{ result.duplicate_count }}</el-descriptions-item>
      <el-descriptions-item label="冲突待审">{{ result.conflict_count }}</el-descriptions-item>
    </el-descriptions>
    <pre v-else-if="result" class="mono" style="margin-top: 10px">{{ JSON.stringify(result, null, 1) }}</pre>
  </el-dialog>
</template>
