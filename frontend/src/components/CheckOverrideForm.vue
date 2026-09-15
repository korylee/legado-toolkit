<script setup>
// 「本次校验」的参数覆盖表单。桌面挂在工具栏的 popover 里，移动端挂在 dialog 里，
// 两处共用这一份——两份必然漂移（字段、文案、diff 规则各写一遍）。
//
// 契约刻意做小：进的是**与全局不同的键**（diff），不是全量值。
// 这样父组件永远不需要判断「空串 / false 算不算传了」——`{}` 就是「不覆盖」，
// 与后端 resolve_check 的语义一一对应。
import { ref, computed, watch } from "vue";
import { ElMessage } from "element-plus";
import { getSettings } from "../api/settings";

const props = defineProps({
  //: 当前的 diff（与全局设置不同的键）
  modelValue: { type: Object, default: () => ({}) },
  //: 「忽略缓存，全部重校」。**它不是覆盖项**：没有全局对应值可比，
  //: 纯粹是本次动作的开关，所以单独一条 v-model，不进 modelValue 的 diff。
  refresh: { type: Boolean, default: false },
});
const emit = defineEmits(["update:modelValue", "update:refresh", "summary"]);

const refreshOn = computed({
  get: () => props.refresh,
  set: (v) => emit("update:refresh", v),
});

const global = ref(null);   // 打开时拉到的全局值
const form = ref(null);     // 编辑态（全局值打底 + 上次的 diff 叠上去）
const limits = ref(null);
const loading = ref(false);

const DEPTH_LABELS = {
  1: "1 · 浅探测（只验域名与搜索）",
  2: "2 · 验目录（比对章节数）",
  3: "3 · 验正文（抓一章全文）",
};

const FIELD_LABELS = {
  concurrency: "并发数", timeout: "超时", probe_depth: "探测深度",
  probe_search: "搜索探测", verify_ssl: "校验 SSL", proxy: "代理",
};

async function reload() {
  loading.value = true;
  try {
    const s = await getSettings();
    global.value = { ...s.values.check };
    limits.value = s.limits;
    // 全局值打底，**再把上次的覆盖叠回去**：直接 form = {...全局} 会把用户上次
    // 设的覆盖静默清掉，而按钮上的「N 项」徽标还亮着——自相矛盾
    form.value = { ...s.values.check, ...(props.modelValue || {}) };
  } catch (e) {
    // 拉不到不等于不能校验：保持 form = null，diff 为空，提交时后端走全局设置。
    // 降级到「不覆盖」正好是最安全的行为
    form.value = null;
    global.value = null;
    ElMessage.warning("读取全局设置失败，本次将直接使用全局设置");
  } finally {
    loading.value = false;
  }
}

defineExpose({ reload });

function buildDiff() {
  if (!form.value || !global.value) return {};
  const out = {};
  for (const key of Object.keys(global.value)) {
    if (form.value[key] !== global.value[key]) out[key] = form.value[key];
  }
  return out;
}

const overrideCount = computed(() => Object.keys(buildDiff()).length);
const activeCount = computed(() => overrideCount.value + (props.refresh ? 1 : 0));

// 也盯着 refresh：只切「忽略缓存」而不动参数时，摘要同样要更新
watch([form, () => props.refresh], () => {
  const diff = buildDiff();
  emit("update:modelValue", diff);
  const parts = Object.keys(diff).map((k) => {
    const v = diff[k];
    const text = typeof v === "boolean" ? (v ? "开" : "关") : (v === "" ? "直连" : v);
    return (FIELD_LABELS[k] || k) + " " + text;
  });
  if (props.refresh) parts.unshift("忽略缓存全部重校");
  emit("summary", parts.length ? "本次：" + parts.join("、") : "");
}, { deep: true });
</script>

<template>
  <div v-loading="loading">
    <el-form v-if="form && limits" label-position="top" size="small"
             style="margin-bottom: 0">
      <el-form-item label="并发数" style="margin-bottom: 10px">
        <el-input-number v-model="form.concurrency" controls-position="right" size="small"
                         :min="limits.concurrency[0]" :max="limits.concurrency[1]"
                         style="width: 100%" />
      </el-form-item>

      <el-form-item label="单请求超时（秒）" style="margin-bottom: 10px">
        <el-input-number v-model="form.timeout" controls-position="right" size="small"
                         :min="limits.timeout[0]" :max="limits.timeout[1]"
                         style="width: 100%" />
      </el-form-item>

      <el-form-item label="探测深度" style="margin-bottom: 10px">
        <el-select v-model="form.probe_depth" size="small" style="width: 100%">
          <el-option v-for="d in limits.probe_depth" :key="d" :value="d"
                     :label="DEPTH_LABELS[d] || ('深度 ' + d)" />
        </el-select>
      </el-form-item>

      <el-form-item label="搜索探测" style="margin-bottom: 10px">
        <el-switch v-model="form.probe_search" size="small" />
      </el-form-item>

      <el-form-item label="校验 SSL 证书" style="margin-bottom: 10px">
        <el-switch v-model="form.verify_ssl" size="small" />
      </el-form-item>

      <el-form-item label="代理" style="margin-bottom: 4px">
        <el-input v-model="form.proxy" placeholder="留空直连" clearable size="small" />
      </el-form-item>
    </el-form>

    <!-- 本次动作：没有全局对应值，所以不参与上面的 diff -->
    <el-divider style="margin: 10px 0" />
    <div class="muted" style="margin-bottom: 6px">本次动作</div>
    <el-switch v-model="refreshOn" size="small" active-text="忽略缓存，全部重校"
               inline-prompt style="--el-switch-on-color: var(--el-color-warning)" />
    <div class="muted" style="margin-top: 4px">
      有效期内的缓存本来会直接复用、不重新请求；打开就这次全部重来
    </div>

    <el-divider style="margin: 10px 0" />
    <div class="muted">
      与全局设置相同的项不会进本次覆盖
      <template v-if="activeCount"> · 本次生效 {{ activeCount }} 项</template>
    </div>
  </div>
</template>
