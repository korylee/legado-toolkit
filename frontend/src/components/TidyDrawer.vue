<script setup>
// 整理源：三步流水线。当前落地的是**第 1 步 · 名称清洗**。
//
// 为什么清洗排在梳理之前：清洗让重名暴露得更充分（清洗前同名组 407 → 清洗后 618），
// 而「同名 / 同站点」正是下一步找重复的判据之一。
// 为什么把校验放到最后一步：改名会改 fingerprint（`loader.fingerprint` 含
// `bookSourceName`），那些源的校验缓存随之失效——集中到最后重跑一次，而不是边改边跑。
//
// 预演是**只读**的，且每条都要人过一眼：0.95 以上是确定性规则（首尾空白、全角字符、
// 名字里混进的网址/域名），默认勾上；0.85 那一档是「去首尾装饰符号」，会连带去掉
// ◎辞晨◎ / 🎃 这类分享者签名——有人想留，所以默认不勾。
import { ref, computed, watch } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { previewNames, applyNames, undoNames } from "../api/sources";
import { useMobile } from "../composables/useMobile";

const props = defineProps({ modelValue: { type: Boolean, default: false } });
const emit = defineEmits(["update:modelValue", "changed"]);

const isMobile = useMobile();
const visible = computed({
  get: () => props.modelValue,
  set: (v) => emit("update:modelValue", v),
});

//: 默认勾选的置信度门槛。0.95 = 确定性规则；0.85 那一档（去装饰符号）留给人过目
const AUTO_CONFIDENCE = 0.95;

const loading = ref(false);
const applying = ref(false);
const rows = ref([]);
const total = ref(0);
const reasonLabels = ref({});
const checked = ref(new Set());
const showAll = ref(false);
const result = ref(null);      // { applied, prev } —— 有值就显示结果条（含撤销）
const undone = ref(false);

const showHeight = computed(() => (isMobile.value ? "46vh" : "calc(100vh - 420px)"));

//: 默认只看高置信那一档：1651 条一次性铺开会把「我到底在批什么」淹掉
const listRows = computed(() => showAll.value
  ? rows.value
  : rows.value.filter((r) => Number(r.confidence) >= AUTO_CONFIDENCE));

const highCount = computed(
  () => rows.value.filter((r) => Number(r.confidence) >= AUTO_CONFIDENCE).length);

const checkedRows = computed(() => rows.value.filter((r) => checked.value.has(r.url)));

function confType(c) {
  const n = Number(c) || 0;
  if (n >= 0.98) return "success";
  if (n >= 0.95) return "primary";
  if (n >= 0.85) return "warning";
  return "info";
}

function reasonText(r) {
  return (r.reasons || []).map((x) => reasonLabels.value[x] || x).join("、");
}

async function load() {
  loading.value = true;
  result.value = null;
  undone.value = false;
  try {
    const res = await previewNames();
    rows.value = res.items || [];
    total.value = res.total || 0;
    reasonLabels.value = res.reason_labels || {};
    // 默认勾选：只勾确定性规则那一档。**不是「全选」**——0.85 那批会去掉
    // 分享者签名，那个决定必须由人来做
    checked.value = new Set(
      rows.value.filter((r) => Number(r.confidence) >= AUTO_CONFIDENCE).map((r) => r.url));
    showAll.value = false;
  } catch (e) {
    ElMessage.error("预演失败: " + e.message);
  } finally {
    loading.value = false;
  }
}

watch(() => props.modelValue, (v) => { if (v) load(); });

function toggle(row) {
  const next = new Set(checked.value);
  if (next.has(row.url)) next.delete(row.url);
  else next.add(row.url);
  checked.value = next;
}

function checkAll(inList) {
  const next = new Set(checked.value);
  listRows.value.forEach((r) => (inList ? next.add(r.url) : next.delete(r.url)));
  checked.value = next;
}

async function apply() {
  const changes = checkedRows.value.map((r) => ({ url: r.url, name: r.new_name }));
  if (!changes.length) return ElMessage.warning("先勾选要改的条目");
  try {
    await ElMessageBox.confirm(
      "将改 " + changes.length + " 条源的名字。"
      + "改的是展示名（地址不动）；导出的 JSON 会同步，"
      + "而这些源的校验结果会失效、需要重新校验。",
      "应用改名", { type: "warning", confirmButtonText: "应用" });
  } catch (e) { return; }
  applying.value = true;
  try {
    const res = await applyNames(changes);
    result.value = { applied: res.applied, missing: res.missing, prev: res.prev || [] };
    ElMessage.success("已改 " + res.applied + " 条");
    emit("changed");
    const res2 = await previewNames();
    rows.value = res2.items || [];
    reasonLabels.value = res2.reason_labels || {};
    checked.value = new Set();
  } catch (e) {
    ElMessage.error("应用失败: " + e.message);
  } finally {
    applying.value = false;
  }
}

async function undo() {
  if (!result.value || !result.value.prev.length) return;
  try {
    const res = await undoNames(result.value.prev);
    undone.value = true;
    ElMessage.success("已还原 " + res.restored + " 条");
    emit("changed");
    await load();
  } catch (e) {
    ElMessage.error("撤销失败: " + e.message);
  }
}
</script>

<template>
  <el-drawer v-model="visible" title="整理源 · 名称清洗" size="860px" destroy-on-close>
    <el-alert type="info" :closable="false" show-icon style="margin-bottom: 12px">
      <template #title>
        清洗的是<b>展示名</b>，<b>不动地址</b>（地址是源的身份，改了在 App 里等于换了一条源）。
        名字变了会让这些源的校验结果失效——所以清洗、找重复、合并做完之后，
        最后再重跑一次校验。
      </template>
    </el-alert>

    <div class="toolbar" style="margin-bottom: 10px">
      <el-button @click="load" :loading="loading">重新预演</el-button>
      <el-switch v-model="showAll" size="small"
                 :active-text="'显示全部 ' + rows.length + ' 条'" inactive-text="只看高置信" />
      <el-button size="small" @click="checkAll(true)">本页全选</el-button>
      <el-button size="small" @click="checkAll(false)">本页全不选</el-button>
      <span class="grow" />
      <span class="muted">
        共 {{ total }} 条 · 建议改 {{ rows.length }} 条 · 其中确定性规则 {{ highCount }} 条
      </span>
    </div>

    <div v-loading="loading" class="list" :style="{ height: showHeight }">
      <div v-for="r in listRows" :key="r.url" class="row"
           :class="{ on: checked.has(r.url) }" @click="toggle(r)">
        <el-checkbox :model-value="checked.has(r.url)" @click.prevent.stop="toggle(r)" />
        <div class="names">
          <div class="old">{{ r.old_name || "(无名)" }}</div>
          <div class="new">{{ r.new_name }}</div>
        </div>
        <el-tag size="small" :type="confType(r.confidence)">
          {{ Math.round((Number(r.confidence) || 0) * 100) }}%
        </el-tag>
        <el-tooltip v-if="r.collides" content="改完与库里另一条源同名——下一步「重复梳理」里能看到它俩" placement="top">
          <el-tag size="small" type="danger">同名</el-tag>
        </el-tooltip>
        <span class="muted why">{{ reasonText(r) }}</span>
      </div>
      <el-empty v-if="!loading && !listRows.length" description="没有需要清洗的名字" :image-size="70" />
    </div>

    <div v-if="result" class="result">
      <span>已改 <b>{{ result.applied }}</b> 条</span>
      <span v-if="result.missing" class="muted">（{{ result.missing }} 条已不在库里，跳过）</span>
      <span class="muted">· 这些源需要重新校验</span>
      <el-button link type="primary" :disabled="undone" @click="undo">
        {{ undone ? "已撤销" : "撤销本次改名" }}
      </el-button>
    </div>

    <template #footer>
      <el-button @click="visible = false">关闭</el-button>
      <el-button type="primary" :loading="applying" :disabled="!checked.size" @click="apply">
        应用改名（{{ checked.size }} 条）
      </el-button>
    </template>
  </el-drawer>
</template>

<style scoped>
.list { overflow: auto; border: 1px solid #ebeef5; border-radius: 4px; }
.row {
  display: flex; align-items: center; gap: 8px;
  padding: 6px 10px; border-bottom: 1px solid #f5f7fa; cursor: pointer;
}
.row:last-child { border-bottom: 0; }
.row:hover { background: #fafcff; }
.row.on { background: #f0f9ff; }
.names { flex: 1 1 auto; min-width: 0; display: flex; align-items: baseline; gap: 8px; }
.old { color: #909399; text-decoration: line-through; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.new { color: #303133; font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.why { flex: 0 0 auto; font-size: 12px; }
.result {
  display: flex; align-items: center; gap: 8px; margin-top: 10px;
  padding: 8px 12px; background: #f0f9eb; border-radius: 4px;
}
</style>
