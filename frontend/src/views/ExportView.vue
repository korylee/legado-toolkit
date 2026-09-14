<script setup>
import { ref, computed, watch, onMounted } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import QRCode from "qrcode";
import { api } from "../api/client";
import { listSources } from "../api/sources";

const rows = ref([]);
const selected = ref([]);
const loading = ref(false);

// 后端自报的局域网地址（localhost 手机访问不了）
const net = ref({ ips: [], port: 8787, hostname: "" });
const host = ref("");
const ttlDays = ref(7);
const pinned = ref(false);
const name = ref("");

// 每次导出生成一个 uid 临时文件：多选不同批次可以并存，互不覆盖
const generated = ref(null);
const qrDataUrl = ref("");
const history = ref([]);

const baseUrl = computed(() => "http://" + host.value + ":" + net.value.port);
const deepLink = computed(() => {
  if (!generated.value) return "";
  return "yuedu://booksource/importonline?src=" +
    encodeURIComponent(baseUrl.value + generated.value.url);
});
const hostWarning = computed(() =>
  !host.value || ["localhost", "127.0.0.1", "::1"].includes(host.value));

async function loadSources() {
  loading.value = true;
  try {
    const res = await listSources({ limit: 300, order: "-stars" });
    rows.value = res.items;
  } catch (e) {
    ElMessage.error("加载源失败: " + e.message);
  } finally {
    loading.value = false;
  }
}

async function loadNet() {
  try {
    net.value = await api.get("/net");
    host.value = (net.value.ips && net.value.ips[0]) || location.hostname;
  } catch (e) {
    host.value = location.hostname;
  }
}

async function loadHistory() {
  try {
    history.value = await api.get("/export");
  } catch (e) {
    history.value = [];
  }
}

async function refreshQr() {
  if (!deepLink.value) { qrDataUrl.value = ""; return; }
  qrDataUrl.value = await QRCode.toDataURL(deepLink.value, { width: 280, margin: 1 });
}
watch(deepLink, refreshQr);
watch(host, refreshQr);

async function doExport() {
  try {
    const res = await api.post("/export", {
      urls: selected.value.map((r) => r.source_url),
      name: name.value,
      ttl_days: ttlDays.value,
      pinned: pinned.value,
    });
    generated.value = res;
    ElMessage.success("已生成临时导出 " + res.uid + "（" + res.count + " 条）");
    loadHistory();
  } catch (e) {
    ElMessage.error("导出失败: " + e.message);
  }
}

async function removeExport(row) {
  try {
    await ElMessageBox.confirm("删除导出 " + row.uid + "？链接会立即失效", "确认");
    await api.del("/export/" + row.uid);
    if (generated.value && generated.value.uid === row.uid) generated.value = null;
    loadHistory();
    ElMessage.success("已删除");
  } catch (e) { /* 取消 */ }
}

async function togglePin(row) {
  await api.post("/export/" + row.uid + "/pin?pinned=" + (!row.pinned));
  loadHistory();
}

function bytes(n) {
  if (!n) return "-";
  return n > 1048576 ? (n / 1048576).toFixed(1) + " MB" : Math.round(n / 1024) + " KB";
}

onMounted(async () => {
  await loadNet();
  await loadSources();
  await loadHistory();
});
</script>

<template>
  <div class="page">
    <el-alert type="info" :closable="false" show-icon style="margin-bottom: 12px">
      <template #title>
        每次导出生成一个 <b>uid 临时文件</b>（不是固定链接）—— 这样多选不同批次可以并存，
        之前发出去的二维码内容不会被后一次导出覆盖。二维码里只放 <b>链接</b>，
        所以 3861 条也就 90 个字符。
      </template>
    </el-alert>

    <el-row :gutter="16">
      <el-col :span="13">
        <el-card shadow="never" header="1. 选源（不选 = 全部导出）">
          <el-table :data="rows" v-loading="loading" border size="small" height="380"
                    @selection-change="(v) => (selected = v)">
            <el-table-column type="selection" width="42" />
            <el-table-column prop="name" label="名称" min-width="150" show-overflow-tooltip />
            <el-table-column prop="stars" label="★" width="56" />
            <el-table-column prop="group_name" label="分组" min-width="140" show-overflow-tooltip />
          </el-table>
          <div class="toolbar" style="margin-top: 10px">
            <el-input v-model="name" placeholder="批次备注（可选）" style="width: 180px" />
            <el-input-number v-model="ttlDays" :min="1" :max="365" controls-position="right"
                             style="width: 130px" />
            <span class="muted">天后失效</span>
            <el-checkbox v-model="pinned">永久保留</el-checkbox>
            <el-button type="primary" @click="doExport">
              生成导出（已选 {{ selected.length || "全部" }}）
            </el-button>
          </div>
        </el-card>

        <el-card shadow="never" header="导出历史" style="margin-top: 12px">
          <el-table :data="history" border size="small" max-height="260">
            <el-table-column prop="uid" label="uid" width="120" />
            <el-table-column prop="name" label="备注" width="110" show-overflow-tooltip />
            <el-table-column prop="count" label="条数" width="64" />
            <el-table-column prop="hits" label="被拉取" width="76">
              <template #default="{ row }">
                <el-tag v-if="row.hits > 0" size="small" type="success">{{ row.hits }}</el-tag>
                <el-tag v-else size="small" type="info">0</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="状态" width="90">
              <template #default="{ row }">
                <el-tag v-if="row.pinned" size="small" type="warning">永久</el-tag>
                <el-tag v-else-if="row.expired" size="small" type="danger">已过期</el-tag>
                <el-tag v-else size="small">有效</el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="expires_at" label="到期" width="150" />
            <el-table-column label="操作" width="130">
              <template #default="{ row }">
                <el-button link size="small" @click="togglePin(row)">
                  {{ row.pinned ? "取消永久" : "永久" }}
                </el-button>
                <el-button link size="small" type="danger" @click="removeExport(row)">删除</el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-col>

      <el-col :span="11">
        <el-card shadow="never" header="2. 扫码 / 一键导入">
          <el-form label-width="92px" size="small">
            <el-form-item label="后端地址">
              <el-select v-model="host" filterable allow-create style="width: 100%">
                <el-option v-for="ip in net.ips" :key="ip" :value="ip" :label="ip" />
              </el-select>
            </el-form-item>
            <el-form-item label="端口">
              <el-input-number v-model="net.port" :min="1" :max="65535"
                               controls-position="right" />
            </el-form-item>
          </el-form>
          <el-alert v-if="hostWarning" type="warning" :closable="false" show-icon
                    style="margin-bottom: 10px"
                    title="手机访问不了 localhost，请选上面探测到的局域网 IP" />

          <div v-if="!generated" class="muted" style="padding: 30px; text-align: center">
            左侧选好源、点「生成导出」，这里会出现二维码与链接
          </div>

          <template v-else>
            <el-descriptions :column="2" border size="small">
              <el-descriptions-item label="uid">{{ generated.uid }}</el-descriptions-item>
              <el-descriptions-item label="条数">{{ generated.count }}</el-descriptions-item>
              <el-descriptions-item label="大小">{{ bytes(generated.bytes) }}</el-descriptions-item>
              <el-descriptions-item label="到期">{{ generated.expires_at }}</el-descriptions-item>
            </el-descriptions>
            <div style="text-align: center; margin-top: 12px">
              <img v-if="qrDataUrl" :src="qrDataUrl" style="width: 280px; height: 280px" />
            </div>
            <el-input v-model="deepLink" type="textarea" :rows="3" readonly
                      style="margin-top: 10px" />
            <div style="margin-top: 8px">
              <a :href="deepLink">
                <el-button type="success" style="width: 100%">在本机打开 yuedu:// 链接</el-button>
              </a>
            </div>
            <el-alert type="success" :closable="false" show-icon style="margin-top: 10px"
                      title="手机扫码后，回「导出历史」看「被拉取」列：变成 1 就说明手机真的拉到了文件。仍是 0 说明 IP/防火墙有问题，不是 Legado 的错。" />
          </template>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>
