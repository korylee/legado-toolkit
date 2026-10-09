<script setup>
// 「任务中心」抽屉：**一屏列表 + 一个明细弹窗**。
//
// 它原来是一个主从布局（左表格 + 右详情面板），问题是那两半看的东西时效性不同：
// 列表答「现在在跑什么、历史跑得怎么样」，详情答「这一次到底发生了什么」。
// 挤在一个抽屉里时两边都难受——列表里在跑的任务进度是冻结的（只有被选中的那条会被
// 轮询），而详情面板大半屏被 job_id / 执行方式这类八股字段占着。
//
// 现在：抽屉只做列表（默认「进行中 + 最近一个保留期」，可切全部），点行开
// `TaskDetailDialog`——那个弹窗在跑时是进度条、结束后是结果明细，自带轮询。
//
// 列表的**结论摘要**（通过 / 未通过 / 相对上次变化）由后端投影给（见
// `backend/api/jobs.py` 的 `_job_row`）：结果里有 items[:500]，不能进列表响应。
import { ref, computed, watch, onMounted, onUnmounted } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { cancelJob as cancelJobApi, listJobs } from "../api/jobs";
import {
  jobCancelHint,
  jobIsInFlight,
  jobKindLabel,
  jobPhaseLabel,
  jobStatusLabel,
  jobStatusType,
} from "../utils/jobs";
import { useJobs } from "../composables/useJobs";
import { useMobile } from "../composables/useMobile";
import TaskDetailDialog from "./TaskDetailDialog.vue";

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
// 档位名里的「最近 N 天」由后端下发（= 任务的保留期），不在这里抄一份
const recentDays = ref(0);
//: 默认只给「进行中 + 最近一个保留期」。全部历史在任务多起来后首屏就是噪音
const scope = ref("recent");
const detailOpen = ref(false);
const detailJobId = ref("");
const isMobile = useMobile();

const STATUS_TYPES = ["running", "cancel_requested", "pending"];

//: 在跑的行要有实时进度：列表拿回来的是快照，而进度是提交页的 SSE 在推。
//: 读同一份共享状态（useJobs）而不是自己再轮询一遍——否则同一件事两处各拉一次。
function rowOf(row) {
  const live = active.value.get(row.id);
  return live ? { ...row, ...live } : row;
}

const rows = computed(() => jobs.value.map(rowOf));
const inFlightCount = computed(
  () => rows.value.filter((r) => jobIsInFlight(r.status)).length);

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
    // 调试运行不进这份列表：它由调试页自己观测（一进 jobs 表，点一次调试就多一条，
    // 任务中心会被刷成流水账）。要回看某次调试的运行，从调试页那条运行条进
    const page = await listJobs({ scope: scope.value, exclude_kind: "jvm_debug" });
    jobs.value = Array.isArray(page?.items) ? page.items : [];
    //: 上限由后端下发（不在这里写 200）：界面要说明「只显示最近 N 条」
    listLimit.value = Number(page?.limit || 0);
    recentDays.value = Number(page?.recent_days || 0);
    // **必须写回共享状态**：提交任务的页面刷新之后它的 SSE 就断了，没人再 upsert，
    // 徽标和在跑条会一直空着（跑批是分钟级，刷新很常见）。列表是唯一还能拿到
    // 「本页刷新前就在跑的 / 别处提交的」任务的来源。
    for (const row of jobs.value) upsertJob(row);
  } catch (e) {
    ElMessage.error("加载任务失败：" + e.message);
  } finally {
    loading.value = false;
  }
}

// 取消：与明细弹窗同一句后果说明——跑批是**块级中止**，说成「立刻停止」会让人
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

function openDetail(row) {
  detailJobId.value = row.id;
  detailOpen.value = true;
}

// 有在跑任务时把列表刷一遍：进度靠共享状态是活的，但**新出现的任务**（别处提交的）
// 只有重拉才知道。没有在跑任务就完全不动
let timer = null;
function ensureTicker() {
  if (timer) return;
  timer = window.setInterval(() => {
    if (visible.value && active.value.size) load();
  }, 5000);
}
function stopTicker() {
  if (timer) window.clearInterval(timer);
  timer = null;
}

watch(scope, () => { if (visible.value) load(); });

// 打开抽屉时拉一次最新列表
watch(() => props.modelValue, async (open) => {
  if (open) await load();
}, { immediate: true });

// 提交任务的页面会 upsert 进共享状态，但列表快照要重拉。
// **抽屉关着就不用拉**：那时没有读者，等在打开时那一次 load 里补（它本来就拉最新）
watch(() => active.value.size, (n, prev) => {
  if (visible.value && n !== prev) load();
});

onMounted(() => { load(); ensureTicker(); });
onUnmounted(stopTicker);

// 父组件提交完任务后调一下，让列表快照刷新
defineExpose({ refresh: load });
</script>

<template>
  <el-drawer v-model="visible" title="任务中心" direction="rtl" size="min(960px, 96vw)"
             class="jobs-drawer">
    <div class="jobs-toolbar">
      <el-radio-group v-model="scope" size="small">
        <el-radio-button value="recent">{{
          recentDays ? "进行中 + 最近 " + recentDays + " 天" : "进行中"
        }}</el-radio-button>
        <el-radio-button value="all">全部</el-radio-button>
      </el-radio-group>
      <span class="chip is-active"><b>{{ inFlightCount }}</b> 进行中</span>
      <span class="grow" />
      <el-button size="small" :loading="loading" @click="load">刷新</el-button>
    </div>

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
          <el-button v-if="jobIsInFlight(row.status)" link type="warning" size="small"
                     @click.stop="cancelJob(row)">
            取消
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

    <TaskDetailDialog v-model="detailOpen" :job-id="detailJobId" @changed="load" />
  </el-drawer>
</template>

<style scoped>
.jobs-toolbar {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 12px;
}
.jobs-toolbar .grow { flex: 1 1 auto; }
/* 统计胶囊用全局 `.chip` 的骨架视觉（任务明细、执行方式、统计条筛选共用）：
   原来这里另写一份，padding 与那边漂成 3px 8px / 3px 10px，
   还挂着一个没定义的 active class */
.jobs-table { height: calc(100% - 52px); }
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

.job-cards { height: calc(100% - 52px); overflow-y: auto; display: flex; flex-direction: column; gap: 8px; }
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
/* el-drawer 会 teleport 到 body，抽屉本身不承担滚动；滚动交给表格。 */
.jobs-drawer .el-drawer__body { overflow: hidden; }
</style>
