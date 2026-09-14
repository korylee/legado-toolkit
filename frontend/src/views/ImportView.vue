<script setup>
import { ref } from "vue";
import { ElMessage } from "element-plus";

const file = ref(null);
const preview = ref(null);
const loading = ref(false);
const rawText = ref("");

function onFileChange(f) { file.value = f.raw; rawText.value = ""; preview.value = null; }
function beforeUpload() { return false; }   // 手动解析，不用自动上传

async function parseLocal() {
  if (!file.value) return ElMessage.warning("先选文件");
  loading.value = true;
  try {
    const text = await file.value.text();
    const data = JSON.parse(text);
    if (!Array.isArray(data)) throw new Error("不是 JSON 数组");
    rawText.value = text;
    // 本地先做个粗预览；真正的差异（新增/重复/冲突）由后端 registry 判定
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
    ElMessage.success("解析成功，共 " + data.length + " 条");
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
  try {
    const res = await fetch("/api/import", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content: rawText.value, source: file.value ? file.value.name : "" }),
    });
    if (!res.ok) throw new Error(await res.text());
    const r = await res.json();
    ElMessage.success("新增 " + r.new_count + " / 重复 " + r.duplicate_count + " / 冲突 " + r.conflict_count);
  } catch (e) {
    ElMessage.error("导入接口尚未实现: " + e.message);
  } finally {
    loading.value = false;
  }
}
</script>

<template>
  <div class="page">
    <el-card shadow="never" header="文件导入（走安全导入：新增待校验、同 URL 规则冲突进待审）">
      <el-upload drag :auto-upload="false" :limit="1" accept=".json"
                 :on-change="onFileChange" :before-upload="beforeUpload">
        <el-icon class="el-icon--upload"><upload-filled /></el-icon>
        <div class="el-upload__text">拖入 JSON 文件，或<em>点击选择</em></div>
      </el-upload>

      <div class="toolbar" style="margin-top: 12px">
        <el-button type="primary" :loading="loading" @click="parseLocal">本地解析预览</el-button>
        <el-button type="success" :loading="loading" :disabled="!rawText" @click="doImport">
          确认导入（后端安全导入）
        </el-button>
      </div>

      <el-descriptions v-if="preview" :column="4" border size="small" style="margin-top: 12px">
        <el-descriptions-item label="条目数">{{ preview.count }}</el-descriptions-item>
        <el-descriptions-item label="缺 URL">{{ preview.noUrl }}</el-descriptions-item>
        <el-descriptions-item label="无搜索规则">{{ preview.noSearch }}</el-descriptions-item>
        <el-descriptions-item label="类型分布">{{ JSON.stringify(preview.types) }}</el-descriptions-item>
      </el-descriptions>

      <el-alert type="info" :closable="false" show-icon style="margin-top: 12px"
                title="真正的差异统计（新增/重复/冲突）由后端 registry 判定，冲突会进审批队列逐条 diff，不会自动覆盖候选主库。" />
    </el-card>
  </div>
</template>
