<script setup>
// 导出抽屉：直接吃列表页的「筛选条件」和「勾选」。
// 之前是独立页面，自己再拉一份 limit:300 的列表 —— 3861 条根本选不全。
import { ref, computed, watch } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import QRCode from "qrcode";
import { api } from "../api/client";

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  selected: { type: Array, default: () => [] },   // 列表页勾选的源
  filter: { type: Object, default: () => ({}) },  // 列表页当前筛选
  filteredTotal: { type: Number, default: 0 },
});
const emit = defineEmits(["update:modelValue"]);

const visible = computed({
  get: () => props.modelValue,
  set: (v) => emit("update:modelValue", v),
});

const net = ref({ ips: [], port: 8787 });
const host = ref("");
const name = ref("");
const ttlDays = ref(7);
const pinned = ref(false);
const generated = ref(null);
const qrDataUrl = ref("");
const history = ref([]);
const busy = ref(false);

const baseUrl = computed(() => "http://" + host.value + ":" + net.value.port);
const deepLink = computed(() => {
  if (!generated.value) return "";
  return "yuedu://booksource/importonline?src=" +
    encodeURIComponent(baseUrl.value + generated.value.url);
});
const hostWarning = computed(() =>
  !host.value || ["localhost", "127.0.0.1", "::1"].includes(host.value));
const filterDesc = computed(() => {
  const f = props.filter || {};
  const parts = [];
  if (f.q) parts.push("关键词=" + f.q);
  if (f.type !== null && f.type !== undefined && f.type !== "") parts.push("类型=" + f.type);
  if (f.health) parts.push("健康度=" + f.health);
  if (f.group) parts.push("分组=" + f.group);
  return parts.length ? parts.join(" / ") : "全部（未筛选）";
});

async function refreshQr() {
  if (!deepLink.value) { qrDataUrl.value = ""; return; }
  qrDataUrl.value = await QRCode.toDataURL(deepLink.value, { width: 260, margin: 1 });
}
watch(deepLink, refreshQr);
watch(host, refreshQr);
watch(() => props.modelValue, async (v) => {
  if (!v) return;
  try {
    net.value = await api.get("/net");
    if (!host.value) host.value = (net.value.ips && net.value.ips[0]) || location.hostname;
  } catch (e) { /* 忽略 */ }
  loadHistory();
  refreshQr();
});

async function loadHistory() {
  try { history.value = await api.get("/export"); } catch (e) { history.value = []; }
}

async function doExport(mode) {
  const body = { name: name.value, ttl_days: ttlDays.value, pinned: pinned.value };
  if (mode === "selected") {
    if (!props.selected.length) return ElMessage.warning("没有勾选任何源");
    body.urls = props.selected.map((r) => r.source_url);
  } else {
    body.filter = {
      type: props.filter.type, health: props.filter.health,
      group: props.filter.group, q: props.filter.q,
    };
  }
  busy.value = true;
  try {
    generated.value = await api.post("/export", body);
    ElMessage.success("已生成 " + generated.value.uid + "（" + generated.value.count + " 条）");
    await refreshQr();
    loadHistory();
  } catch (e) {
    ElMessage.error("导出失败: " + e.message);
  } finally {
    busy.value = false;
  }
}

async function removeExport(row) {
  try {
    await ElMessageBox.confirm("删除 " + row.uid + "？链接立即失效", "确认");
    await api.del("/export/" + row.uid);
    if (generated.value && generated.value.uid === row.uid) generated.value = null;
    loadHistory();
  } catch (e) { /* 取消 */ }
}

async function togglePin(row) {
  await api.post("/export/" + row.uid + "/pin?pinned=" + (!row.pinned));
  loadHistory();
}

function kb(n) {
  if (!n) return "-";
  return n > 1048576 ? (n / 1048576).toFixed(1) + " MB" : Math.round(n / 1024) + " KB";
}
</script>

<template>
  <el-drawer v-model="visible" title="导出到 App" size="880px" destroy-on-close>
    <el-alert type="info" :closable="false" show-icon style="margin-bottom: 12px">
      <template #title>
        每次导出生成一个 <b>uid 临时文件</b>，不同批次互不覆盖。二维码里只放<b>链接</b>，
        手机扫码后由 Legado 自己去拉取。
      </template>
    </el-alert>

    <el-row :gutter="16">
      <el-col :span="14">
        <el-card shadow="never" header="导出范围">
          <el-descriptions :column="1" border size="small">
            <el-descriptions-item label="已勾选">
              <b>{{ selected.length }}</b> 条
              <span v-if="selected.length" class="muted">
                （{{ selected.slice(0, 3).map((r) => r.name).join("、") }}
                <template v-if="selected.length > 3">…</template>）
              </span>
            </el-descriptions-item>
            <el-descriptions-item label="当前筛选">
              <b>{{ filteredTotal }}</b> 条　<span class="muted">{{ filterDesc }}</span>
            </el-descriptions-item>
          </el-descriptions>

          <div class="toolbar" style="margin-top: 12px">
            <el-input v-model="name" placeholder="批次备注（可选）" size="small" style="width: 150px" />
            <el-input-number v-model="ttlDays" :min="1" :max="365" size="small"
                             controls-position="right" style="width: 110px" />
            <span class="muted">天后失效</span>
            <el-checkbox v-model="pinned" size="small">永久保留</el-checkbox>
          </div>
          <div class="toolbar" style="margin-top: 10px">
            <el-button type="primary" :loading="busy" :disabled="!selected.length"
                       @click="doExport('selected')">
              导出勾选的 {{ selected.length }} 条
            </el-button>
            <el-button :loading="busy" @click="doExport('filter')">
              导出当前筛选的 {{ filteredTotal }} 条
            </el-button>
          </div>
          <div class="muted" style="margin-top: 6px">
            第二个按钮不受分页限制 —— 这是从原独立导出页搬过来的主要原因。
          </div>
        </el-card>

        <el-card shadow="never" header="导出历史" style="margin-top: 12px">
          <el-table :data="history" border size="small" max-height="240">
            <el-table-column prop="uid" label="uid" width="116" />
            <el-table-column prop="name" label="备注" width="100" show-overflow-tooltip />
            <el-table-column prop="count" label="条数" width="62" />
            <el-table-column label="被拉取" width="76">
              <template #default="{ row }">
                <el-tag size="small" :type="row.hits > 0 ? 'success' : 'info'">{{ row.hits }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="状态" width="86">
              <template #default="{ row }">
                <el-tag v-if="row.pinned" size="small" type="warning">永久</el-tag>
                <el-tag v-else-if="row.expired" size="small" type="danger">已过期</el-tag>
                <el-tag v-else size="small">有效</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="120">
              <template #default="{ row }">
                <el-button link size="small" @click="togglePin(row)">
                  {{ row.pinned ? "取消永久" : "永久" }}
                </el-button>
                <el-button link size="small" type="danger" @click="removeExport(row)">删除</el-button>
              </template>
            </el-table-column>
            <template #empty><el-empty description="还没有导出过" :image-size="60" /></template>
          </el-table>
        </el-card>
      </el-col>

      <el-col :span="10">
        <el-card shadow="never" header="扫码 / 一键导入">
          <el-form label-width="86px" size="small">
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
                    title="手机访问不了 localhost，请选探测到的局域网 IP" />

          <div v-if="!generated" class="muted" style="padding: 36px; text-align: center">
            点左边任一「导出」按钮，这里会出现二维码与链接
          </div>
          <template v-else>
            <el-descriptions :column="2" border size="small">
              <el-descriptions-item label="uid">{{ generated.uid }}</el-descriptions-item>
              <el-descriptions-item label="条数">{{ generated.count }}</el-descriptions-item>
              <el-descriptions-item label="大小">{{ kb(generated.bytes) }}</el-descriptions-item>
              <el-descriptions-item label="到期">{{ generated.expires_at }}</el-descriptions-item>
            </el-descriptions>
            <div style="text-align: center; margin-top: 10px">
              <img v-if="qrDataUrl" :src="qrDataUrl" style="width: 260px; height: 260px" />
            </div>
            <el-input v-model="deepLink" type="textarea" :rows="3" readonly style="margin-top: 8px" />
            <a :href="deepLink" style="display: block; margin-top: 8px">
              <el-button type="success" style="width: 100%">在本机打开 yuedu:// 链接</el-button>
            </a>
            <el-alert type="success" :closable="false" show-icon style="margin-top: 10px"
                      title="手机扫码后回「导出历史」看「被拉取」列：变 1 说明手机真拉到了。仍是 0 说明 IP/防火墙问题，不是 Legado 的错。" />
          </template>
        </el-card>
      </el-col>
    </el-row>
  </el-drawer>
</template>
