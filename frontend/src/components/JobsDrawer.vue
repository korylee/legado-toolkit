<script setup>
// 「任务中心」抽屉：**一屏列表 + 一个明细弹窗**。
//
// 它原来是一个主从布局（左表格 + 右详情面板），问题是那两半看的东西时效性不同：
// 列表答「现在在跑什么、历史跑得怎么样」，详情答「这一次到底发生了什么」。
// 挤在一个抽屉里时两边都难受——列表里在跑的任务进度是冻结的（只有被选中的那条会被
// 轮询），而详情面板大半屏被 job_id / 执行方式这类八股字段占着。
//
// 现在：抽屉只做近期任务列表（在跑任务 + 最近保留期），过期任务由后端清理；点行按
// **kind 分派**到两个明细弹窗之一（`utils/jobDetail`）：校验 / 调试走
// `TaskDetailDialog`（在跑时是进度条、结束后是结果明细），AI 补全走
// `AgentRunDialog`（逐字段四态 + 模型调用账）——后者塞进前者只会显示「没有可展示的结果」。
//
// 列表的**结论摘要**（通过 / 未通过 / 相对上次变化）由后端投影给（见
// `backend/api/jobs.py` 的 `_job_row`）：结果里有 items[:500]，不能进列表响应。
import { ref, computed, watch, onMounted, onUnmounted } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { cancelJob as cancelJobApi, deleteJob as deleteJobApi, listJobs } from "../api/jobs";
import {
  categoryKinds,
  JOB_CATEGORIES,
  jobCategoryOf,
  jobCancelHint,
  jobCanCancel,
  jobIsInFlight,
  jobIsTerminal,
  jobKindLabel,
  jobPhaseLabel,
  jobStatusLabel,
  jobStatusType,
} from "../utils/jobs";
import { useJobs } from "../composables/useJobs";
import { useMobile } from "../composables/useMobile";
import { openJobDetail } from "../utils/jobDetail";
import TaskDetailDialog from "./TaskDetailDialog.vue";
import AgentRunDialog from "./AgentRunDialog.vue";

const props = defineProps({
  modelValue: { type: Boolean, default: false },
});
const emit = defineEmits(["update:modelValue"]);

const visible = computed({
  get: () => props.modelValue,
  set: (v) => emit("update:modelValue", v),
});

const { active, activeJobs, upsertJob } = useJobs();

const loading = ref(false);
const jobs = ref([]);
//: 后端下发的列表条数上限；到顶时界面要说明还有更早的没显示
const listLimit = ref(0);
const isMobile = useMobile();
const detailRef = ref(null);
//: AI 补全的明细弹窗（按 kind 分派，见 openDetail / utils/jobDetail）
const agentRef = ref(null);

const STATUS_TYPES = ["running", "cancel_requested", "pending"];

// ---------------------------------------------------------------- 类目页签
//
// 调试也进任务中心（分类页签把流水账分走）：徽标本来就不排除调试（数得到），
// 列表却排除了（翻不到）——那正是口径分裂，一处显示 N、另一处显示 0。
// 上次看的页签记下来：打开抽屉落回它，不每次都落「全部」。
const JOBS_TAB_KEY = "legado.jobsTab";
const TABS = [
  { key: "all", label: "全部" },
  ...JOB_CATEGORIES.map((c) => ({ key: c.key, label: c.label })),
];
const jobsTab = ref(readTab());

function readTab() {
  try {
    const v = localStorage.getItem(JOBS_TAB_KEY) || "";
    return TABS.some((t) => t.key === v) ? v : "all";
  } catch (e) {
    return "all";
  }
}
watch(jobsTab, (v) => {
  try {
    localStorage.setItem(JOBS_TAB_KEY, v);
  } catch (e) {
    /* 同上 */
  }
});
//: 没登记进任何类目的 kind 落「其他」：页签是排除式过滤，藏起来就找不到了。
//: 过滤在**这一份**做（页面按类目筛），「其他」桶才推得出来——所以 load 不按 kind
//: 过滤，后端只给近期窗口。
const hasOtherTab = computed(() =>
  jobs.value.some((row) => jobCategoryOf(row.kind) === null));
const tabList = computed(() =>
  hasOtherTab.value ? [...TABS, { key: "other", label: "其他" }] : TABS);

//: 在跑的行要有实时进度：列表拿回来的是快照，而进度是提交页的 SSE 在推。
//: 读同一份共享状态（useJobs）而不是自己再轮询一遍——否则同一件事两处各拉一次。
function rowOf(row) {
  const live = active.value.get(row.id);
  return live ? { ...row, ...live } : row;
}

const rows = computed(() => {
  const all = jobs.value.map(rowOf);
  if (jobsTab.value === "all") return all;
  if (jobsTab.value === "other") {
    return all.filter((r) => jobCategoryOf(r.kind) === null);
  }
  const kinds = categoryKinds(jobsTab.value).split(",").filter(Boolean);
  return all.filter((r) => kinds.includes(String(r.kind)));
});
const inFlightCount = computed(
  () => jobs.value.filter((r) => jobIsInFlight(r.status)).length);

function pct(row) {
  const total = Number(row.total || 0);
  const progress = Number(row.progress || 0);
  return total > 0 ? Math.min(100, Math.round((progress / total) * 100)) : 0;
}

function summaryText(row) {
  const s = row.summary;
  if (!s) return "";
  return `通过 ${s.ok} · 未通过 ${s.fail}`
    + (s.changed_total ? ` · 变化 ${s.changed_total} 条` : "");
}

async function load() {
  loading.value = true;
  try {
    // 调试也进这份列表（分类页签把流水账分走）：徽标本来就不排除调试，列表再排除
    // 就是「徽标数得到、抽屉里翻不到」。不按 kind 过滤——类目在客户端筛，其他桶才推得出来
    const page = await listJobs({ scope: "recent" });
    jobs.value = Array.isArray(page?.items) ? page.items : [];
    //: 上限由后端下发（不在这里写 200）：界面要说明「只显示最近 N 条」
    listLimit.value = Number(page?.limit || 0);
    // **必须写回共享状态**：提交任务的页面刷新之后它的 SSE 就断了，没人再 upsert，
    // 徽标和在跑条会一直空着（批量校验是分钟级，刷新很常见）。列表是唯一还能拿到
    // 「本页刷新前就在跑的 / 别处提交的」任务的来源。
    for (const row of jobs.value) upsertJob(row);
  } catch (e) {
    ElMessage.error("加载任务失败：" + e.message);
  } finally {
    loading.value = false;
  }
}

// 取消：与明细弹窗同一句后果说明——批量校验是**块级中止**，说成「立刻停止」会让人
// 以为按钮失灵（详见 utils/jobs 的 jobCancelHint）
async function cancelJob(row) {
  try {
    await ElMessageBox.confirm(jobCancelHint(row.kind), "取消任务", {
      type: "warning", confirmButtonText: "取消任务", cancelButtonText: "返回",
    });
    await cancelJobApi(row.id);
    ElMessage.success("已请求取消任务");
    await load();
  } catch (e) {
    if (e !== "cancel" && e !== "close") ElMessage.error("取消任务失败：" + e.message);
  }
}

async function deleteJobRow(row) {
  try {
    await ElMessageBox.confirm("删除后将移除任务记录和结果，不能恢复。", "删除任务", {
      type: "warning", confirmButtonText: "删除", cancelButtonText: "返回",
    });
    await deleteJobApi(row.id);
    ElMessage.success("任务已删除");
    await load();
  } catch (e) {
    if (e !== "cancel" && e !== "close") ElMessage.error("删除任务失败：" + e.message);
  }
}

function openDetail(row) {
  // 按 kind 分派：AI 补全有自己的明细弹窗（判据与两个 ref 在 utils/jobDetail）
  openJobDetail(row && row.kind, row && row.id, { detailRef, agentRef });
}

// 有在跑任务时把列表刷一遍：进度靠共享状态是活的，但**新出现的任务**（别处提交的）
// 只有重拉才知道。没有在跑任务就完全不动。
// 计时器**按需启停**：抽屉常驻主布局，onMounted 永久起计时器的话，整个会话
// 都在每 5 秒空醒一次（回调里再判 visible/active 挡住请求，计时器本身还在跑）。
let timer = null;
function syncTicker() {
  const needed = visible.value && active.value.size > 0;
  if (needed && !timer) {
    timer = window.setInterval(() => {
      if (visible.value && active.value.size) load();
    }, 5000);
  } else if (!needed && timer) {
    stopTicker();
  }
}
function stopTicker() {
  if (timer) window.clearInterval(timer);
  timer = null;
}

// 打开抽屉时拉一次最新列表
watch(() => props.modelValue, async (open) => {
  if (open) await load();
  syncTicker();
}, { immediate: true });

// 提交任务的页面会 upsert 进共享状态，但列表快照要重拉。
// **抽屉关着就不用拉**：那时没有读者，等在打开时那一次 load 里补（它本来就拉最新）
watch(() => active.value.size, (n, prev) => {
  syncTicker();
  if (visible.value && n !== prev) load();
});

onMounted(() => { load(); syncTicker(); });
onUnmounted(stopTicker);

// 父组件提交完任务后调一下，让列表快照刷新
defineExpose({ refresh: load });
</script>

<template>
  <el-drawer v-model="visible" title="任务中心" direction="rtl" size="min(960px, 96vw)"
             class="jobs-drawer">
    <div class="jobs-toolbar">
      <span class="muted">近期任务</span>
      <span class="chip is-active"><b>{{ inFlightCount }}</b> 进行中</span>
      <span class="grow" />
      <el-button size="small" :loading="loading" @click="load">刷新</el-button>
    </div>

    <!-- 类目页签：全部 / 校验 / 调试 / AI（未登记的 kind 落「其他」，不藏）。
         上次看的页签记下来，打开抽屉落回它 -->
    <el-radio-group v-model="jobsTab" size="small" class="jobs-tabs">
      <el-radio-button
        v-for="t in tabList"
        :key="t.key"
        :value="t.key"
      >{{ t.label }}</el-radio-button>
    </el-radio-group>

    <div v-if="isMobile" class="job-cards" v-loading="loading">
      <div v-for="row in rows" :key="row.id" class="job-card" @click="openDetail(row)">
        <div class="job-card-head">
          <el-tag size="small" :type="jobStatusType(row.status)">
            {{ jobStatusLabel(row.status) }}
          </el-tag>
          <span class="job-card-kind">{{ jobKindLabel(row.kind) }}</span>
          <span class="grow" />
          <span class="muted">{{ row.created_at }}</span>
        </div>
        <el-progress v-if="row.total" :percentage="pct(row)" :stroke-width="7"
                     :status="row.status === 'failed' ? 'exception' : undefined" />
        <div class="job-card-foot">
          <span class="muted">{{ row.progress }} / {{ row.total || "?" }}</span>
          <span v-if="summaryText(row)">{{ summaryText(row) }}</span>
          <span v-else-if="row.error" class="job-error" :title="row.error">{{ row.error }}</span>
          <span v-else class="muted">{{ jobPhaseLabel(row.phase) }}</span>
        </div>
      </div>
      <el-empty v-if="!loading && !rows.length" description="这段时间没有任务"
                :image-size="70" />
      <p v-if="listLimit && rows.length >= listLimit" class="muted jobs-cap">
        只显示最近 {{ listLimit }} 条，更早的任务不在列表里。
      </p>
    </div>

    <el-table v-else class="jobs-table" :data="rows" v-loading="loading" border size="small"
              highlight-current-row @row-click="openDetail">
      <el-table-column label="状态" width="90" align="center">
        <template #default="{ row }">
          <el-tag size="small" :type="jobStatusType(row.status)">
            {{ jobStatusLabel(row.status) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="类型" width="130">
        <template #default="{ row }">{{ jobKindLabel(row.kind) }}</template>
      </el-table-column>
      <el-table-column label="进度" min-width="150">
        <template #default="{ row }">
          <el-progress class="row-progress" :percentage="pct(row)" :stroke-width="7"
                       :status="row.status === 'failed' ? 'exception' : undefined" />
          <span class="muted">{{ row.progress }} / {{ row.total || "?" }}</span>
        </template>
      </el-table-column>
      <el-table-column label="结论 / 原因" min-width="200">
        <template #default="{ row }">
          <span v-if="summaryText(row)">{{ summaryText(row) }}</span>
          <!-- 失败原因要留在列表上：只说「失败」用户就得逐条点开弹窗才知道为什么 -->
          <span v-else-if="row.error" class="job-error" :title="row.error">{{ row.error }}</span>
          <span v-else class="muted">—</span>
        </template>
      </el-table-column>
      <el-table-column label="阶段" min-width="120">
        <template #default="{ row }">{{ jobPhaseLabel(row.phase) }}</template>
      </el-table-column>
      <el-table-column prop="created_at" label="提交时间" width="150" />
      <el-table-column label="操作" width="140" fixed="right" align="center">
        <template #default="{ row }">
          <el-button link type="primary" size="small" @click.stop="openDetail(row)">
            明细
          </el-button>
          <!-- 在跑的能就地取消：不必先点开弹窗（取消的后果说明在确认框里按类型给） -->
          <el-button v-if="jobIsInFlight(row.status) && jobCanCancel(row.kind)" link type="warning" size="small"
                     @click.stop="cancelJob(row)">
            取消
          </el-button>
          <el-button v-if="jobIsTerminal(row.status)" link type="danger" size="small"
                     @click.stop="deleteJobRow(row)">
            删除
          </el-button>
        </template>
      </el-table-column>
      <template #empty>
        <el-empty description="这段时间没有任务" :image-size="70" />
      </template>
    </el-table>

    <p v-if="listLimit && rows.length >= listLimit" class="muted jobs-cap">
      只显示最近 {{ listLimit }} 条，更早的任务不在列表里。
    </p>

    <TaskDetailDialog ref="detailRef" @changed="load" />
    <AgentRunDialog ref="agentRef" />
  </el-drawer>
</template>

<style scoped>
.jobs-toolbar {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 12px;
  flex: 0 0 auto;
}
.jobs-toolbar .grow { flex: 1 1 auto; }
/* 统计胶囊用全局 `.chip` 的骨架视觉（任务明细、执行方式、统计条筛选共用）：
   原来这里另写一份，padding 与那边漂成 3px 8px / 3px 10px，
   还挂着一个没定义的 active class */
.jobs-tabs { margin-bottom: 10px; flex: 0 0 auto; }
.jobs-table { flex: 1 1 auto; min-height: 0; }
.jobs-table :deep(.el-table__body tr) { cursor: pointer; }
.jobs-table :deep(.el-button) { padding: 2px 3px; }
.jobs-table :deep(.el-tag) { border-radius: 999px; }
.row-progress { margin-bottom: 2px; }
.row-progress :deep(.el-progress__text) { display: none; }
.job-error {
  color: var(--el-color-danger);
  display: inline-block; max-width: 100%;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  vertical-align: bottom;
}
.jobs-cap { margin: 8px 2px 0; font-size: 12px; }

.job-cards { flex: 1 1 auto; min-height: 0; overflow-y: auto; display: flex; flex-direction: column; gap: 8px; }
.job-card {
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  padding: 10px 12px;
  cursor: pointer;
}
.job-card-head { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
.job-card-head .grow { flex: 1 1 auto; }
.job-card-kind { font-size: 13px; font-weight: 600; }
.job-card-foot {
  display: flex; gap: 10px; flex-wrap: wrap;
  margin-top: 6px; font-size: 12px;
}

@media (max-width: 760px) {
  .jobs-table :deep(.el-table__body-wrapper) { font-size: 12px; }
}
</style>

<style>
/* el-drawer 会 teleport 到 body，抽屉本身不承担滚动；滚动交给表格（桌面）与
   卡片列表（移动）。抽屉体是 **flex 列**：头部行（工具条/类目页签）固定，
   列表用 flex 撑满剩余高度——写死 calc(100% - N) 的话，头部每加一行就要
   改一次数字，漏改的表现正是「列表底部被裁掉、滚不到」（移动端真踩过） */
.jobs-drawer .el-drawer__body {
  overflow: hidden;
  display: flex;
  flex-direction: column;
}
</style>
