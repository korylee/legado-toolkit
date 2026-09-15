<script setup>
// 导出到 App —— 极简版。
//
// 这个抽屉真正的目的只有一个：把源导进手机上的 Legado。
// 所以主路径就两步：选范围 → 扫码。
// 其余（备注/有效期/后端地址/导出历史）全部降到「折叠项」，
// 默认收起 —— 视觉上只占一行，需要时才展开。
import { ref, computed, watch } from "vue";
import { ElMessage } from "element-plus";
import { CopyDocument, Refresh, Delete, Upload } from "@element-plus/icons-vue";
import QRCode from "qrcode";
import { api } from "../api/client";

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  selected: { type: Array, default: () => [] },
  filter: { type: Object, default: () => ({}) },
  filteredTotal: { type: Number, default: 0 },
});
const emit = defineEmits(["update:modelValue"]);

const visible = computed({
  get: () => props.modelValue,
  set: (v) => emit("update:modelValue", v),
});

const host = ref("");
const port = ref(8787);
const ips = ref([]);
const ttlDays = ref(7);
const generated = ref(null);
const qrDataUrl = ref("");
const busy = ref(false);
const history = ref([]);
const tabs = ref("");

const hasFilter = computed(() => {
  const f = props.filter || {};
  return !!(f.q || f.health || f.group || f.tag || (f.type !== null && f.type !== undefined && f.type !== ""));
});
const link = computed(() => {
  if (!generated.value) return "";
  return "yuedu://booksource/importonline?src=" +
    encodeURIComponent("http://" + host.value + ":" + port.value + generated.value.url);
});
const hostBad = computed(() =>
  !host.value || ["localhost", "127.0.0.1", "::1"].includes(host.value));

async function makeQr() {
  qrDataUrl.value = link.value
    ? await QRCode.toDataURL(link.value, { width: 240, margin: 1 })
    : "";
}
watch(link, makeQr);
watch(() => props.modelValue, async (v) => {
  if (!v) return;
  generated.value = null;
  try {
    const n = await api.get("/net");
    ips.value = n.ips || [];
    port.value = n.port || 8787;
    if (!host.value) host.value = ips.value[0] || location.hostname;
  } catch (e) { host.value = location.hostname; }
  loadHistory();
});

async function loadHistory() {
  try { history.value = await api.get("/export"); } catch (e) { history.value = []; }
}

async function doExport(mode) {
  const body = { ttl_days: ttlDays.value };
  if (mode === "selected") {
    if (!props.selected.length) return ElMessage.warning("先勾选要导出的源");
    body.urls = props.selected.map((r) => r.source_url);
  } else if (mode === "filter") {
    const f = props.filter || {};
    body.filter = { type: f.type, health: f.health, group: f.group, tag: f.tag, q: f.q };
  }
  busy.value = true;
  try {
    generated.value = { ...(await api.post("/export", body)), kind: "export" };
    await makeQr();
    loadHistory();
    ElMessage.success("已生成 " + generated.value.count + " 条的导入链接");
  } catch (e) {
    ElMessage.error("导出失败: " + e.message);
  } finally {
    busy.value = false;
  }
}

function makeFeed(scope) {
  const kind = scope === "all" ? "all" : "ok";
  generated.value = {
    kind: "feed",
    label: scope === "all" ? "完整源订阅" : "可用源订阅",
    url: "/api/feed/" + kind + ".json",
  };
}

async function refreshHits() {
  if (!generated.value) return;
  try {
    const m = await api.get("/export/" + generated.value.uid);
    generated.value = { ...generated.value, hits: m.hits };
  } catch (e) { /* 忽略 */ }
}

async function copyLink() {
  try {
    await navigator.clipboard.writeText(link.value);
    ElMessage.success("链接已复制");
  } catch (e) {
    ElMessage.warning("复制失败，请手动选中文本");
  }
}

async function delHistory(uid) {
  await api.del("/export/" + uid);
  if (generated.value && generated.value.uid === uid) generated.value = null;
  loadHistory();
}
</script>

<template>
  <el-drawer v-model="visible" title="导出 / 订阅" direction="rtl" size="400px" destroy-on-close>
    <div v-if="!generated" class="export-body">
      <p class="muted" style="margin: 0 0 12px">
        已勾选 <b>{{ selected.length }}</b> 条
        <template v-if="hasFilter">　当前筛选 <b>{{ filteredTotal }}</b> 条</template>
      </p>

      <el-button type="primary" size="large" style="width: 100%" :loading="busy"
                 :disabled="!selected.length" @click="doExport('selected')">
        导出勾选的 {{ selected.length }} 条
      </el-button>

      <el-button v-if="hasFilter" size="large" style="width: 100%; margin: 10px 0 0"
                 :loading="busy" @click="doExport('filter')">
        导出当前筛选的 {{ filteredTotal }} 条
      </el-button>

      <div style="text-align: center; margin-top: 12px">
        <el-button link :loading="busy" @click="doExport('all')">导出全部（含未校验）</el-button>
      </div>

      <el-divider style="margin: 16px 0 10px" />
      <p class="muted" style="margin: 0 0 8px">
        固定订阅（不过期，内容自动更新）
      </p>
      <el-button size="large" style="width: 100%" :disabled="!host"
                 @click="makeFeed('ok')">
        可用源订阅
      </el-button>
      <el-button size="large" style="width: 100%; margin: 10px 0 0"
                 :disabled="!host" @click="makeFeed('all')">
        完整源订阅
      </el-button>
    </div>

    <div v-else class="export-body">
      <p v-if="generated.kind === 'feed'" class="muted" style="margin: 0 0 10px">
        <b>{{ generated.label }}</b>　固定链接，不过期
      </p>
      <p v-else class="muted" style="margin: 0 0 10px">
        <span class="mono">{{ generated.uid }}</span>
        　{{ generated.count }} 条　{{ ttlDays }} 天后失效
      </p>

      <div class="qr-box">
        <img v-if="qrDataUrl" :src="qrDataUrl" alt="二维码" />
      </div>

      <el-button type="success" size="large" style="width: 100%; margin-top: 12px"
                 tag="a" :href="link">
        在手机上打开
      </el-button>

      <p v-if="generated.kind === 'export'"
         class="muted" style="margin: 12px 0 0; text-align: center">
        手机扫码后，拉取次数会变成 1
        <el-button link :icon="Refresh" @click="refreshHits" />
        <b style="color: #409eff">已拉取 {{ generated.hits ?? 0 }} 次</b>
      </p>
      <p v-else class="muted" style="margin: 12px 0 0; text-align: center">
        固定链接，源更新后手机重新拉取即可。
      </p>

      <el-button link style="width: 100%; margin-top: 10px" @click="generated = null">
        ← {{ generated.kind === 'feed' ? '返回订阅选择' : '重新选择导出范围' }}
      </el-button>
    </div>

    <el-divider style="margin: 16px 0" />

    <!-- 次要功能全部折叠，默认只占一行 -->
    <el-collapse v-model="tabs">
      <el-collapse-item name="link" title="手动复制链接">
        <el-input v-model="link" type="textarea" :rows="3" readonly />
        <el-button :icon="CopyDocument" style="width: 100%; margin-top: 8px" @click="copyLink">
          复制
        </el-button>
      </el-collapse-item>

      <el-collapse-item name="addr" title="手机连不上？检查地址">
        <el-select v-model="host" filterable allow-create style="width: 100%">
          <el-option v-for="ip in ips" :key="ip" :value="ip" :label="ip" />
        </el-select>
        <el-input-number v-model="port" :min="1" :max="65535" controls-position="right"
                         style="width: 100%; margin-top: 8px" />
        <el-alert v-if="hostBad" type="warning" :closable="false" show-icon
                  style="margin-top: 8px"
                  title="localhost / 127.0.0.1 手机访问不到，要填电脑的局域网 IP" />
        <p class="muted" style="margin: 8px 0 0">
          提示：手机连不上时先检查这里是否选了局域网 IP、端口是否放行。
        </p>
      </el-collapse-item>

      <el-collapse-item v-if="!generated || generated.kind !== 'feed'" name="opt" title="有效期">
        <el-input-number v-model="ttlDays" :min="1" :max="365" controls-position="right"
                         style="width: 100%" />
        <p class="muted" style="margin: 8px 0 0">链接过期后重新导出即可，一次点击。</p>
      </el-collapse-item>

      <el-collapse-item :title="'导出历史（' + history.length + '）'" name="hist">
        <div v-for="h in history" :key="h.uid" class="hist-row">
          <span class="mono">{{ h.uid }}</span>
          <span class="muted">{{ h.count }} 条</span>
          <el-tag size="small" :type="h.hits > 0 ? 'success' : 'info'">{{ h.hits }}</el-tag>
          <el-button link type="danger" :icon="Delete" @click="delHistory(h.uid)" />
        </div>
        <el-empty v-if="!history.length" description="还没有导出过" :image-size="50" />
      </el-collapse-item>
    </el-collapse>
  </el-drawer>
</template>

<style scoped>
.export-body { padding: 2px 0; }

.qr-box {
  display: flex; justify-content: center;
  padding: 12px;
  background: #fff;
  border: 1px solid #e4e7ed;
  border-radius: 8px;
}
.qr-box img { width: 240px; height: 240px; }

.hist-row {
  display: flex; align-items: center; gap: 10px;
  padding: 6px 0;
  border-bottom: 1px solid #f0f2f5;
}
.hist-row .mono { flex: 1 1 auto; }

/* 手机上抽屉占满宽 */
@media (max-width: 900px) {
  .qr-box img { width: 200px; height: 200px; }
}
</style>
