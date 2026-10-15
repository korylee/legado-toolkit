<script setup>
// 书源仓库导入的预览抽屉：**一步闭环**——预览（统计 + 逐条清单）就在这里，
// 每行带勾选（默认全选），勾掉的不导入；确认后直接调 /import。
// 「校验」在操作列、按行触发：对单条源先静默入库、再提交本机引擎校验。
// 校验必须先入库（引擎从管理库导出源，未入库的 URL 一条都匹配不上），按钮
// 的语义因此是「导入并校验这一条」。
import { computed, ref, watch } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { api } from "../api/client";
import { domainOf } from "../utils/domain";
import { jvmReadiness, jvmRun } from "../api/jvm.js";
import { useJobs } from "../composables/useJobs";

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  sources: { type: Array, default: () => [] },
  sourceName: { type: String, default: "" },
});
const emit = defineEmits(["update:modelValue", "imported"]);

const { upsertJob } = useJobs();

const IMPORT_STATUS_LABELS = {
  new: "新增",
  duplicate: "重复",
  conflict: "规则冲突",
  update: "将覆盖",
  invalid: "无效",
};

// ---------------------------------------------------------------- 勾选（导入范围）
//: index 对应 props.sources 的下标；预览 items 的 `index` 字段也是这个下标，
//: 两者按 index 关联。
const selected = ref(new Set());
const selectedCount = computed(() => selected.value.size);
const allSelected = computed(() => props.sources.length > 0 && selectedCount.value === props.sources.length);

watch(() => [props.modelValue, props.sources], ([open, sources]) => {
  if (open) {
    selected.value = new Set(sources.map((_source, index) => index));
    resetPreview();
    // 打开即预览：进来就是统计 + 清单（勾选、校验都在清单里），不再有
    // 单独的挑选列表垫在前面。
    loadPreview();
  }
}, { immediate: true });

function toggle(index) {
  const next = new Set(selected.value);
  if (next.has(index)) next.delete(index);
  else next.add(index);
  selected.value = next;
}

function toggleAll() {
  selected.value = allSelected.value
    ? new Set()
    : new Set(props.sources.map((_source, index) => index));
}

// ---------------------------------------------------------------- 预览
//: 同 URL 但规则不同时怎么办。默认 keep：外部源默认不该覆盖你已经在用的规则。
const strategy = ref("keep");
const previewing = ref(false);
const preview = ref(null);   // { count, counts, items, rulesIncomplete }
const importing = ref(false);
const result = ref(null);    // /import 的返回；error 时 { error }

// ---------------------------------------------------------------- 行内校验
//: 引擎就绪才显示校验按钮（结论必须由本机引擎产生，没配引擎就没有校验可提交）。
const jvmReady = ref(false);
watch(() => props.modelValue, async (open) => {
  if (!open) return;
  try {
    const r = await jvmReadiness();
    jvmReady.value = !!(r && r.ok);
  } catch {
    jvmReady.value = false;
  }
}, { immediate: true });
//: 正在「导入并校验」的那行 index（按钮自己的 loading，不锁整张表）
const checkingIndex = ref(-1);

async function submitCheck(urls) {
  const r = await jvmRun({ urls });
  if (!r.started) {
    // 引擎没起来的原因在接口 reason 里（忙 / 空范围 / 环境未就绪）——原样转述
    ElMessage.warning(r.reason || "校验未提交：本机引擎不可用");
    return;
  }
  upsertJob({
    id: r.job_id,
    status: "pending",
    progress: 0,
    total: r.count || urls.length,
    phase: "queued",
    kind: "jvm_run",
  });
  ElMessage.success("已提交校验任务，进度见任务中心");
}

function resetPreview() {
  preview.value = null;
  result.value = null;
}

function importStatusLabel(status) {
  return IMPORT_STATUS_LABELS[status] || "未知";
}

function importStatusType(status) {
  return status === "new"
    ? "success"
    : status === "conflict" || status === "invalid"
      ? "warning"
      : "info";
}

async function loadPreview() {
  if (!props.sources.length) return;
  previewing.value = true;
  resetPreview();
  try {
    // **必须传全量**：后端返回的 items[].index 是入落数组的下标，而勾选集合
    // （selected）用的是 props.sources 的下标——传过滤数组的话，
    // 只要取消勾选过一行，两个下标系就错位，行内勾选会勾到别的行。
    const rules = await api.post("/import/preview", {
      content: JSON.stringify(props.sources),
      conflict_strategy: strategy.value,
    });
    preview.value = {
      count: props.sources.length,
      counts: rules.counts || {},
      items: rules.items || [],
      rulesIncomplete: Number(rules.counts?.rules_incomplete || 0),
    };
  } catch (error) {
    ElMessage.error("读取导入预览失败：" + error.message);
  } finally {
    previewing.value = false;
  }
}

async function doImport() {
  const sources = props.sources.filter((_s, index) => selected.value.has(index));
  if (!sources.length) return ElMessage.warning("先勾选要导入的书源");
  importing.value = true;
  result.value = null;
  try {
    const r = await api.post("/import", {
      content: JSON.stringify(sources),
      source: props.sourceName,
      conflict_strategy: strategy.value,
    });
    result.value = r;
    ElMessage.success(
      `新增 ${r.new_count} / 更新 ${r.updated_count || 0} / 重复 ${r.duplicate_count} / 冲突 ${r.conflict_count}`,
    );
    emit("imported");
  } catch (e) {
    ElMessage.error("导入失败：" + e.message);
    result.value = { error: String(e.message) };
  } finally {
    importing.value = false;
  }
}

//: 操作列的「校验」：对**这一条**源先静默入库、再提交本机引擎校验。
//: 校验必须先入库（引擎只认管理库里已入库的源），按钮语义是「导入并校验这一条」。
//: 无效行校验不了（引擎导不出没有 URL 的源）；冲突行在 keep 下没落库，同样跳过。
async function checkOne(index) {
  const item = (preview.value?.items || []).find((it) => it.index === index);
  if (item && ["invalid", "conflict"].includes(item.status)) {
    return ElMessage.warning(
      item.status === "invalid"
        ? "这条源无效（缺少 URL），无法校验"
        : "这条与库中现有源规则冲突，确认导入（覆盖）后才能校验",
    );
  }
  const source = props.sources[index];
  if (!source) return;
  const confirmed = await ElMessageBox.confirm(
    "校验需要源先入库：将导入这一条，再提交本机引擎校验。继续？",
    "校验「" + (source.bookSourceName || "（无名）") + "」",
    { type: "info", confirmButtonText: "导入并校验", cancelButtonText: "返回" },
  ).then(() => true).catch(() => false);
  if (!confirmed) return;
  checkingIndex.value = index;
  try {
    const r = await api.post("/import", {
      content: JSON.stringify([source]),
      source: props.sourceName,
      conflict_strategy: strategy.value,
    });
    emit("imported");
    const urls = Array.isArray(r.imported_urls) ? r.imported_urls : [];
    if (!urls.length) {
      ElMessage.warning("这条没有落库（可能已在库且内容一致），没有可校验的");
      return;
    }
    await submitCheck(urls);
  } catch (e) {
    ElMessage.error("校验失败：" + e.message);
  } finally {
    checkingIndex.value = -1;
  }
}

function close() {
  emit("update:modelValue", false);
}
</script>

<template>
  <el-drawer
    :model-value="modelValue"
    title="导入预览"
    size="min(720px, 94vw)"
    append-to-body
    destroy-on-close
    class="repository-source-preview-drawer"
    @update:model-value="emit('update:modelValue', $event)"
  >
    <div class="repository-preview" v-loading="previewing && !preview">
      <div class="repository-selection-head">
        <span class="muted">{{ sourceName }} · 共 {{ sources.length }} 条</span>
        <span class="grow" />
        <el-button link type="primary" :loading="previewing" @click="loadPreview">
          重新预览
        </el-button>
      </div>

      <el-descriptions :column="3" border size="small">
        <el-descriptions-item label="书源数">{{ preview.count }}</el-descriptions-item>
        <el-descriptions-item label="新增">{{ preview.counts.new || 0 }}</el-descriptions-item>
        <el-descriptions-item label="重复">{{ preview.counts.duplicate || 0 }}</el-descriptions-item>
        <el-descriptions-item label="规则冲突">{{ preview.counts.conflict || 0 }}</el-descriptions-item>
        <el-descriptions-item label="将覆盖">{{ preview.counts.update || 0 }}</el-descriptions-item>
        <el-descriptions-item label="无效">{{ preview.counts.invalid || 0 }}</el-descriptions-item>
        <el-descriptions-item label="规则不完整">{{ preview.rulesIncomplete }}</el-descriptions-item>
      </el-descriptions>

      <el-table
        v-if="preview.items.length"
        :data="preview.items"
        size="small"
        class="repository-preview-table"
      >
        <el-table-column width="44" align="center">
          <template #header>
            <el-checkbox
              :model-value="allSelected"
              :indeterminate="selectedCount > 0 && !allSelected"
              @change="toggleAll"
            />
          </template>
          <template #default="scope">
            <el-checkbox
              :model-value="selected.has(scope.row.index)"
              @change="toggle(scope.row.index)"
            />
          </template>
        </el-table-column>
        <el-table-column prop="name" label="书源" min-width="140" show-overflow-tooltip />
        <el-table-column prop="url" label="域名" min-width="130" show-overflow-tooltip>
          <template #default="scope">{{ domainOf(scope.row.url) }}</template>
        </el-table-column>
        <el-table-column label="处理方式" width="92">
          <template #default="scope">
            <el-tag size="small" :type="importStatusType(scope.row.status)">
              {{ importStatusLabel(scope.row.status) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="规则" width="100">
          <template #default="scope">
            <el-tag v-if="scope.row.rules_incomplete" size="small" type="warning">
              规则不完整
            </el-tag>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column v-if="jvmReady" label="操作" width="72" align="center">
          <template #default="scope">
            <el-tooltip
              content="先导入这一条，再提交本机引擎校验"
              placement="top"
            >
              <el-button
                link
                type="primary"
                size="small"
                :loading="checkingIndex === scope.row.index"
                @click="checkOne(scope.row.index)"
              >
                校验
              </el-button>
            </el-tooltip>
          </template>
        </el-table-column>
      </el-table>

      <div class="conflict-options">
        <span class="conflict-label">同 URL 不同规则</span>
        <el-radio-group v-model="strategy">
          <el-radio value="keep">保留现有的，导入的进待审</el-radio>
          <el-radio value="overwrite">用导入的覆盖</el-radio>
        </el-radio-group>
      </div>

      <el-descriptions v-if="result && !result.error" :column="4" border size="small">
        <el-descriptions-item label="新增">{{ result.new_count }}</el-descriptions-item>
        <el-descriptions-item label="已更新">{{ result.updated_count || 0 }}</el-descriptions-item>
        <el-descriptions-item label="重复">{{ result.duplicate_count }}</el-descriptions-item>
        <el-descriptions-item label="冲突待审">{{ result.conflict_count }}</el-descriptions-item>
      </el-descriptions>
      <el-alert
        v-else-if="result"
        type="error"
        :closable="false"
        :title="result.error"
      />

      <div class="repository-selection-footer">
        <el-button :disabled="importing" @click="close">关闭</el-button>
        <el-button
          type="success"
          :loading="importing"
          :disabled="importing || (!!result && !result.error) || !selectedCount"
          @click="doImport"
        >
          确认导入（{{ selectedCount }}）
        </el-button>
      </div>
    </div>
  </el-drawer>
</template>

<style scoped>
.repository-preview { display: flex; height: 100%; min-height: 0; flex-direction: column; gap: 12px; }
.repository-selection-head, .repository-selection-footer { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex: 0 0 auto; }
.grow { flex: 1 1 auto; }
.repository-selection-footer { justify-content: flex-end; padding-top: 8px; }
.repository-preview-table { flex: 1 1 auto; min-height: 0; }
.conflict-options { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.conflict-label { color: var(--el-text-color-regular); font-size: 13px; }
</style>

<style>
.repository-source-preview-drawer .el-drawer__body { display: flex; min-height: 0; flex-direction: column; overflow: hidden; }
</style>
