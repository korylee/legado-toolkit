<script setup>
import { ref, computed, watch, onMounted, onUnmounted } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { api, subscribeJob } from "../api/client";
import { describeChanges, healthLabel } from "../utils/health";

// 「任务」抽屉：替代已删掉的「任务」页。
// 打开时拉一次历史（GET /api/jobs），再对还没跑完的任务挂 SSE 实时进度。
// 原「任务」页那个「跑 ping 冒烟」按钮是调试用的，按需求不再搬运。
const props = defineProps({
  modelValue: { type: Boolean, default: false },
  //: 打开时自动选中哪一条任务（结果条上的「查看」用）。空串 = 不选中
  focusJobId: { type: String, default: "" },
});
const emit = defineEmits(["update:modelValue", "running-change"]);

const visible = computed({
  get: () => props.modelValue,
  set: (v) => emit("update:modelValue", v),
});

const loading = ref(false);
const jobs = ref([]);
// job_id -> 取消订阅函数。取消函数不需要响应式，用普通对象存即可，ref 包一层是多余的
const stops = {};

const statusType = (s) => (s === "done" ? "success" : s === "failed" ? "danger" : "warning");
const statusLabel = (s) => ({ pending: "排队中", running: "运行中", done: "已完成", failed: "失败", cancelled: "已取消" }[s] || s || "未知");
const terminal = (s) => ["done", "failed", "cancelled"].includes(s);
const retryLabel = (s) => s === "done" ? "再次运行" : "重试";
const retryable = (row) => terminal(row.status) && row.kind !== "jvm_run";
const pct = (row) => (row.total ? Math.round((row.progress / row.total) * 100) : 0);
// 徽标口径：pending 还没轮到跑、也算「进行中」，与列表里的状态标签保持一致
const runningCount = computed(
  () => jobs.value.filter((j) => j.status === "running" || j.status === "pending").length,
);
const terminalCount = computed(
  () => jobs.value.filter((j) => ["done", "failed", "cancelled"].includes(j.status)).length,
);

// 把进行中的任务数抛给父组件，供统计条上的「任务」按钮画徽标
watch(runningCount, (n) => emit("running-change", n), { immediate: true });

// SSE 推的是整份 job 记录，字段与 list_jobs 同形，直接合并回对应的行
function mergeJob(data) {
  if (!data || data.error) return;        // 任务不存在时后端推的是 {"error": ...}
  const idx = jobs.value.findIndex((j) => j.id === data.id);
  if (idx < 0) return;
  const next = jobs.value.slice();
  next[idx] = { ...next[idx], ...data };
  jobs.value = next;
  if (["done", "failed", "cancelled"].includes(data.status)) {
    const nextDetails = { ...details.value };
    delete nextDetails[data.id];
    details.value = nextDetails;
  }
}

function watchJob(jobId) {
  if (stops[jobId]) return;               // 已订阅过，别重复挂
  stops[jobId] = subscribeJob(
    jobId,
    (data) => mergeJob(data),
    (data) => {
      delete stops[jobId];
      mergeJob(data);
      // **不弹「任务完成」的 toast**：那一行的状态标签本来就会实时变，
      // 而校验任务的结果另有 SourcesView 的结果条——再来一条浮层只是噪音。
      // status === "unknown" 是 SSE 重连到上限的兜底，mergeJob 会安全地忽略它
    },
  );
}

// job_id -> 解析好的结果摘要（null = 拉过了但没有可展示的结果）。
// **必须懒加载单条**：列表接口 list_jobs 不带 result_json（那玩意儿含 items[:500]，
// 全量校验时一条上百 KB，50 条会把抽屉打开拖成几秒）。所以选中某一行时
// 才去打 GET /api/jobs/{id}/detail——否则抽屉一关一开，刚看过的摘要就没了
const details = ref({});
const activeJobId = ref("");
const detailLoading = ref(false);
const selectedJob = computed(() => jobs.value.find((j) => j.id === activeJobId.value) || null);
const selectedDetail = computed(() => details.value[activeJobId.value] || null);
const summaryLines = (s) => {
  if (!s) return [];
  const lines = ["本次校验 " + s.checked + " 条：新校验 " + s.fetched + " 条、复用缓存 " + s.cached + " 条"];
  if (s.first_checked) lines.push("其中 " + s.first_checked + " 条首次有结论");
  const changes = describeChanges(s.changed);
  lines.push(changes.length ? "相对上次变化：" + changes.join("、") : "相对上次：无状态变化");
  return lines;
};

async function loadDetail(row) {
    // 运行中打开时通常还没有 result_json，不能把“暂时没有结果”永久缓存；
    // 任务终态后再次打开必须重新拉一次，才能看到最终结果。
    const terminal = ["done", "failed", "cancelled"].includes(row.status);
    if (terminal && row.id in details.value) {
      return details.value[row.id];
    }
    try {
        const detail = await api.get("/jobs/" + row.id + "/detail");
        details.value = { ...details.value, [row.id]: detail };
        return detail;
    } catch (e) {
        ElMessage.error("加载任务结果失败: " + e.message);
        details.value = { ...details.value, [row.id]: null };
        return null;
    }
}

async function selectJob(row) {
  activeJobId.value = row.id;
  detailLoading.value = true;
  try {
    await loadDetail(row);
  } finally {
    detailLoading.value = false;
  }
}

async function cancelJob(row) {
  try {
    await ElMessageBox.confirm("取消后任务不会继续推进，已产生的结果会保留。", "取消任务", {
      type: "warning", confirmButtonText: "取消任务", cancelButtonText: "返回",
    });
    await api.post("/jobs/" + row.id + "/cancel", {});
    ElMessage.success("已请求取消任务");
    await load();
  } catch (e) {
    if (e !== "cancel" && e !== "close") ElMessage.error("取消任务失败：" + e.message);
  }
}

async function deleteJob(row) {
  try {
    await ElMessageBox.confirm("删除后将移除任务记录和结果，不能恢复。", "删除任务", {
      type: "warning", confirmButtonText: "删除", cancelButtonText: "返回",
    });
    await api.del("/jobs/" + row.id);
    delete details.value[row.id];
    if (activeJobId.value === row.id) activeJobId.value = "";
    jobs.value = jobs.value.filter((j) => j.id !== row.id);
    ElMessage.success("任务已删除");
  } catch (e) {
    if (e !== "cancel" && e !== "close") ElMessage.error("删除任务失败：" + e.message);
  }
}

async function retryJob(row) {
  try {
    await ElMessageBox.confirm(
      row.status === "done" ? "将使用相同参数再运行一次，原任务会保留。" : "将使用相同参数重新提交，原任务会保留。",
      row.status === "done" ? "再次运行" : "重试任务",
      { type: "warning", confirmButtonText: row.status === "done" ? "再次运行" : "重试", cancelButtonText: "返回" },
    );
    const created = await api.post("/jobs/" + row.id + "/retry", {});
    ElMessage.success("已创建新任务");
    await load();
    const next = jobs.value.find((j) => j.id === created.job_id);
    if (next) await selectJob(next);
  } catch (e) {
    if (e !== "cancel" && e !== "close") ElMessage.error("提交重试失败：" + e.message);
  }
}

async function load() {
  loading.value = true;
  try {
    const list = await api.get("/jobs");
    jobs.value = list;
    // 只订阅没跑完的：已完成的任务一订阅就会立刻推终态，会误报「任务完成」提示
    list.forEach((j) => {
      if (j.status === "running" || j.status === "pending") watchJob(j.id);
    });
  } catch (e) {
    ElMessage.error("加载任务失败: " + e.message);
  } finally {
    loading.value = false;
  }
}

// 打开抽屉时自动选中 focusJobId 那条。**要等 load() 完**：列表还没数据时
// 还找不到对应行，不能提前请求详情
watch(() => [props.modelValue, props.focusJobId], async ([open, id]) => {
  if (!open) return;
  if (!loading.value) await load();
  if (!id) return;
  const row = jobs.value.find((j) => j.id === id);
  if (row) await selectJob(row);
}, { immediate: true });

// 挂载即拉一次：徽标要在没打开过抽屉时就正确，光靠「打开时拉」拿不到
onMounted(load);

onUnmounted(() => { Object.values(stops).forEach((f) => f && f()); });

// 父组件提交完任务后调一下，让徽标立刻反映新任务
defineExpose({ refresh: load });
</script>

<template>
  <el-drawer v-model="visible" title="任务中心" direction="rtl" size="900px" class="jobs-drawer">
    <div class="jobs-toolbar">
      <div class="toolbar-copy">
        <span class="toolbar-title">任务记录</span>
        <span class="muted">最近 50 条，运行中的任务会实时更新</span>
      </div>
      <div class="toolbar-stats">
        <span class="stat-chip"><b>{{ jobs.length }}</b> 条记录</span>
        <span class="stat-chip active"><b>{{ runningCount }}</b> 进行中</span>
        <span class="stat-chip"><b>{{ terminalCount }}</b> 已结束</span>
      </div>
      <span class="grow" />
      <el-button class="refresh-button" size="small" :loading="loading" @click="load">刷新列表</el-button>
    </div>

    <div class="jobs-layout">
      <section class="job-list">
        <div class="section-caption"><span>最近任务</span><span class="muted">点击任务查看右侧详情</span></div>
        <el-table class="jobs-table" :data="jobs" v-loading="loading" border size="small"
                  highlight-current-row @row-click="selectJob">
          <el-table-column prop="id" label="job_id" width="120" />
          <el-table-column prop="kind" label="类型" width="80" />
          <el-table-column label="状态" width="88" align="center">
            <template #default="{ row }">
              <el-tag size="small" :type="statusType(row.status)">{{ statusLabel(row.status) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="进度" min-width="130">
            <template #default="{ row }">
              <el-progress class="row-progress" :percentage="pct(row)" :stroke-width="7"
                           :status="row.status === 'failed' ? 'exception' : undefined" />
              <span class="muted">{{ row.progress }} / {{ row.total }}</span>
            </template>
          </el-table-column>
          <el-table-column prop="updated_at" label="更新时间" width="150" />
          <el-table-column label="操作" width="150" fixed="right" align="center">
            <template #default="{ row }">
              <el-button link type="primary" size="small" @click.stop="selectJob(row)">明细</el-button>
              <el-button v-if="row.status === 'pending' || row.status === 'running'"
                         link type="warning" size="small" @click.stop="cancelJob(row)">取消</el-button>
              <template v-else>
                <el-button v-if="retryable(row)" link type="primary" size="small" @click.stop="retryJob(row)">{{ retryLabel(row.status) }}</el-button>
                <el-button link type="danger" size="small" @click.stop="deleteJob(row)">删除</el-button>
              </template>
            </template>
          </el-table-column>
          <template #empty>
            <el-empty description="还没有任务" :image-size="70" />
          </template>
        </el-table>
      </section>

      <aside class="job-detail-panel" v-loading="detailLoading">
        <template v-if="selectedJob">
          <div class="detail-title">
            <div>
              <div class="detail-heading">任务明细</div>
              <div class="muted detail-id">{{ selectedJob.id }}</div>
            </div>
            <el-tag size="small" :type="statusType(selectedJob.status)">{{ statusLabel(selectedJob.status) }}</el-tag>
          </div>
          <el-descriptions :column="1" border size="small">
            <el-descriptions-item label="类型">{{ selectedJob.kind }}</el-descriptions-item>
            <el-descriptions-item label="进度">{{ selectedJob.progress }} / {{ selectedJob.total }}</el-descriptions-item>
            <el-descriptions-item label="创建时间">{{ selectedJob.created_at }}</el-descriptions-item>
            <el-descriptions-item label="更新时间">{{ selectedJob.updated_at }}</el-descriptions-item>
            <el-descriptions-item label="保留至">{{ selectedJob.expires_at || "服务端默认期限" }}</el-descriptions-item>
            <el-descriptions-item v-if="selectedJob.retry_of" label="来源任务">{{ selectedJob.retry_of }}</el-descriptions-item>
          </el-descriptions>

          <template v-if="selectedDetail">
            <template v-if="selectedDetail.summary">
              <div v-for="(line, i) in summaryLines(selectedDetail.summary)" :key="'line' + i" class="detail-line">{{ line }}</div>
              <div v-for="(w, i) in selectedDetail.summary.warnings" :key="'warn' + i" class="detail-warn">{{ w }}</div>
              <div v-if="selectedDetail.summary.changed_items.length" class="detail-changes">
                <div class="muted detail-section-title">
                  状态变化（{{ selectedDetail.summary.changed_items.length }} / {{ selectedDetail.summary.changed_total }} 条）
                </div>
                <div v-for="(c, i) in selectedDetail.summary.changed_items" :key="'item' + i" class="detail-change">
                  <span class="detail-name" :title="c.name">{{ c.name || "（无名）" }}</span>
                  <span class="muted detail-url" :title="c.url">{{ c.url }}</span>
                  <span class="muted">{{ healthLabel(c.from) }} → </span>
                  <span :class="'to-' + c.to">{{ healthLabel(c.to) }}</span>
                </div>
              </div>
            </template>
            <div v-if="selectedDetail.error" class="detail-warn">{{ selectedDetail.error }}</div>
            <div v-if="selectedDetail.result !== null && selectedDetail.result !== undefined" class="detail-result">
              <div class="muted detail-section-title">任务结果</div>
              <pre>{{ JSON.stringify(selectedDetail.result, null, 2) }}</pre>
            </div>
            <div v-if="!selectedDetail.summary && !selectedDetail.error && selectedDetail.result == null" class="muted detail-empty">
              暂无可展示结果
            </div>
          </template>
          <div v-else-if="!detailLoading" class="muted detail-empty">暂无任务结果</div>
        </template>
        <el-empty v-else description="选择任务查看明细" :image-size="90" />
      </aside>
    </div>
  </el-drawer>
</template>

<style scoped>
.jobs-toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  padding: 12px 14px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 10px;
  background: linear-gradient(135deg, var(--el-fill-color-blank), var(--el-fill-color-light));
}
.toolbar-copy {
  display: flex;
  align-items: baseline;
  gap: 10px;
  min-width: 220px;
}
.toolbar-title { font-size: 15px; font-weight: 650; color: var(--el-text-color-primary); }
.toolbar-stats { display: flex; gap: 6px; flex-wrap: wrap; }
.stat-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 4px 8px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 999px;
  background: var(--el-fill-color-blank);
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
.stat-chip b { color: var(--el-text-color-primary); font-weight: 650; }
.stat-chip.active { border-color: var(--el-color-primary-light-7); color: var(--el-color-primary); }
.stat-chip.active b { color: var(--el-color-primary); }
.refresh-button { margin-left: auto; }
.jobs-layout {
  display: flex;
  gap: 14px;
  margin-top: 12px;
  height: calc(100% - 62px);
  min-height: 0;
}
.job-list {
  flex: 1 1 56%;
  min-width: 0;
  min-height: 0;
  overflow: hidden;
  padding: 2px;
}
.section-caption {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin: 0 2px 8px;
  font-size: 13px;
  font-weight: 600;
  color: var(--el-text-color-primary);
}
.job-detail-panel {
  flex: 1 1 44%;
  min-width: 300px;
  overflow-y: auto;
  padding: 16px 16px 24px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 10px;
  background: var(--el-fill-color-blank);
  box-shadow: 0 2px 10px rgb(0 0 0 / 3%);
}
.detail-title {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 12px;
}
.detail-heading { font-size: 16px; font-weight: 650; letter-spacing: .01em; }
.detail-id { margin-top: 3px; font-size: 12px; }
.jobs-table :deep(.el-table__header-wrapper th) {
  height: 38px;
  background: var(--el-fill-color-light);
  color: var(--el-text-color-secondary);
  font-weight: 600;
}
.jobs-table { height: calc(100% - 32px); }
.jobs-table :deep(.el-table__body tr) { cursor: pointer; transition: background-color .15s ease; }
.jobs-table :deep(.el-table__body tr:hover > td) { background: var(--el-fill-color-light); }
.jobs-table :deep(.el-table__body tr.current-row > td) {
  background: var(--el-color-primary-light-9);
}
.jobs-table :deep(.el-table__body tr.current-row td:first-child) {
  box-shadow: inset 3px 0 0 var(--el-color-primary);
}
.jobs-table :deep(.el-button) { padding: 2px 3px; }
.jobs-table :deep(.el-tag) { border-radius: 999px; }
.row-progress :deep(.el-progress-bar__outer) { background: var(--el-fill-color); }
.row-progress :deep(.el-progress__text) { display: none; }
.detail-line { margin-top: 10px; line-height: 1.6; }
.detail-warn { margin-top: 10px; color: var(--el-color-danger); line-height: 1.6; }
.detail-section-title {
  margin: 14px 0 6px;
  font-size: 12px;
}
.detail-changes {
  max-height: 300px;
  overflow-y: auto;
  margin-top: 12px;
  border-top: 1px dashed var(--el-border-color-lighter);
  padding-top: 6px;
}
.detail-change {
  display: flex;
  gap: 7px;
  align-items: baseline;
  line-height: 1.8;
  font-size: 12px;
}
.detail-name {
  flex: 0 1 auto;
  max-width: 150px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.detail-url {
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.detail-result { margin-top: 12px; }
.detail-result pre {
  max-height: 320px; overflow: auto; padding: 10px;
  background: var(--el-fill-color-light); border-radius: 4px; white-space: pre-wrap;
}
.detail-empty { padding: 28px 0; text-align: center; }
.detail-title + :deep(.el-descriptions) { margin-bottom: 14px; }
.job-detail-panel :deep(.el-descriptions__label) { width: 78px; }
.job-detail-panel :deep(.el-descriptions__cell) { padding: 8px 10px; }
.detail-change + .detail-change { border-top: 1px solid var(--el-border-color-extra-light); }
/* 配色与列表页的 healthType 同口径：同一个状态在两处该长一样。 */
.detail-change .to-ok { color: var(--el-color-success); }
.detail-change .to-dead { color: var(--el-color-danger); }
.detail-change .to-auth,
.detail-change .to-cert { color: var(--el-color-warning); }
.detail-change .to-gfw,
.detail-change .to-pending { color: var(--el-color-info); }

@media (max-width: 760px) {
  .jobs-layout { display: block; height: calc(100% - 104px); }
  .job-list { height: 46vh; max-height: 46vh; }
  .jobs-table { height: 100% !important; }
  .job-detail-panel {
    min-width: 0;
    margin-top: 14px;
    padding: 14px 12px 20px;
    border-top: 1px solid var(--el-border-color-lighter);
    border-left: 0;
  }
  .toolbar-copy { min-width: 0; flex-direction: column; gap: 3px; }
  .toolbar-stats { order: 3; width: 100%; }
  .refresh-button { margin-left: auto; }
}
</style>

<style>
/* el-drawer 会 teleport 到 body，抽屉本身不承担滚动；滚动交给表格和详情面板。 */
.jobs-drawer .el-drawer__body { overflow: hidden; }
</style>
