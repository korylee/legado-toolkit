<script setup>
import { ref, computed, onMounted, nextTick } from "vue";
import { ElMessage } from "element-plus";
import QRCode from "qrcode";
import { listSources, getStats } from "../api/sources";

const rows = ref([]);
const selected = ref([]);
const loading = ref(false);
const backendIp = ref(location.hostname);
const port = ref(8787);
const qrDataUrl = ref("");
const deepLink = ref("");
const exported = ref(null);

// importonline 的 src 必须从手机可达：用后端局域网 IP + 固定路径
const exportUrl = computed(() =>
  "http://" + backendIp.value + ":" + port.value + "/api/export/latest.json");
const link = computed(() =>
  "yuedu://booksource/importonline?src=" + encodeURIComponent(exportUrl.value));

async function load() {
  loading.value = true;
  try {
    const res = await listSources({ limit: 200, order: "-stars" });
    rows.value = res.items;
  } finally { loading.value = false; }
}

async function makeQr() {
  deepLink.value = link.value;
  qrDataUrl.value = await QRCode.toDataURL(deepLink.value, { width: 260, margin: 1 });
}

function downloadJson() {
  // 多选导出：只导出选中的源，保持 App 期望的完整字段
  const data = selected.value.length ? selected.value.map((r) => r.raw || r) : rows.value;
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "bookSource_" + new Date().toISOString().slice(0, 10) + ".json";
  a.click();
  URL.revokeObjectURL(a.href);
  ElMessage.success("已下载 " + data.length + " 条");
}

async function doExport() {
  try {
    const res = await fetch("/api/export", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ urls: selected.value.map((r) => r.source_url), name: "latest" }),
    });
    exported.value = await res.json();
    ElMessage.success("后端已生成导出文件");
    makeQr();
  } catch (e) {
    ElMessage.error("导出接口尚未实现: " + e.message);
  }
}

onMounted(() => { load(); makeQr(); });
</script>

<template>
  <div class="page">
    <el-alert type="info" :closable="false" show-icon style="margin-bottom: 12px">
      <template #title>
        二维码里只放 <b>链接</b>，不放源内容 —— 所以 3861 条也就 90 个字符，
        手机扫码后由 Legado 自己去拉取。
      </template>
    </el-alert>

    <el-row :gutter="16">
      <el-col :span="14">
        <el-card shadow="never" header="1. 选源（不选=全部）">
          <el-table :data="rows" v-loading="loading" border size="small" height="440"
                    @selection-change="(v) => (selected = v)">
            <el-table-column type="selection" width="42" />
            <el-table-column prop="name" label="名称" min-width="160" show-overflow-tooltip />
            <el-table-column prop="stars" label="★" width="60" />
            <el-table-column prop="group_name" label="分组" min-width="150" show-overflow-tooltip />
          </el-table>
          <div style="margin-top: 10px">
            <el-button type="primary" @click="doExport">生成固定导出文件</el-button>
            <el-button @click="downloadJson">下载 JSON 文件</el-button>
            <span class="muted">
              已选 {{ selected.length }} 条；导出链接固定为 /api/export/latest.json（覆盖式，手机里存的链接不会失效）
            </span>
          </div>
        </el-card>
      </el-col>

      <el-col :span="10">
        <el-card shadow="never" header="2. 扫码 / 一键导入">
          <el-form label-width="90px">
            <el-form-item label="后端地址">
              <el-input v-model="backendIp" placeholder="局域网 IP，如 192.168.1.5" />
            </el-form-item>
            <el-form-item label="端口">
              <el-input-number v-model="port" :min="1" :max="65535" controls-position="right" />
            </el-form-item>
          </el-form>
          <el-alert v-if="backendIp === 'localhost' || backendIp === '127.0.0.1'"
                    type="warning" :closable="false" show-icon style="margin-bottom: 10px"
                    title="手机访问不了 localhost，请填电脑的局域网 IP" />
          <div style="text-align: center">
            <img v-if="qrDataUrl" :src="qrDataUrl" style="width: 260px; height: 260px" />
          </div>
          <el-input v-model="deepLink" type="textarea" :rows="3" readonly style="margin-top: 10px" />
          <div style="margin-top: 8px">
            <a :href="link"><el-button type="success" style="width: 100%">在本机打开 yuedu:// 链接</el-button></a>
          </div>
          <div class="muted" style="margin-top: 8px">
            手机端：Legado → 扫码导入 / 打开链接。需保证手机能访问上面的地址。
          </div>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>
