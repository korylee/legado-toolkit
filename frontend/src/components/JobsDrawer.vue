<script setup>
import { ref, computed, watch, onMounted, onUnmounted } from "vue";
import { ElMessage } from "element-plus";
import { api, subscribeJob } from "../api/client";
import { describeChanges, healthLabel } from "../utils/health";

// 「任务」抽屉：替代已删掉的「任务」页。
// 打开时拉一次历史（GET /api/jobs），再对还没跑完的任务挂 SSE 实时进度。
// 原「任务」页那个「跑 ping 冒烟」按钮是调试用的，按需求不再搬运。
const props = defineProps({
  modelValue: { type: Boolean, default: false },
  //: 打开时自动展开哪一条任务（结果条上的「查看」用）。空串 = 不展开
  focusJobId: { type: String, default: "" },
});
const emit = defineEmits(["update:modelValue", "running-change"]);

const visible = computed({
  get: () => props.modelValue,
  set: (v) => emit("update:modelValue", v),
});

const loading = ref(false);
const jobs = ref([]);
const tableRef = ref(null);
// job_id -> 取消订阅函数。取消函数不需要响应式，用普通对象存即可，ref 包一层是多余的
const stops = {};

const statusType = (s) => (s === "done" ? "success" : s === "failed" ? "danger" : "warning");
const pct = (row) => (row.total ? Math.round((row.progress / row.total) * 100) : 0);
// 徽标口径：pending 还没轮到跑、也算「进行中」，与列表里的状态标签保持一致
const runningCount = computed(
  () => jobs.value.filter((j) => j.status === "running" || j.status === "pending").length,
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
// 全量校验时一条上百 KB，50 条会把抽屉打开拖成几秒）。所以展开某一行时
// 才去打 GET /api/jobs/{id}——否则抽屉一关一开，刚看过的摘要就没了
const details = ref({});

function buildDetail(r) {
    if (r && r.error) {
        // 失败的任务：后端写的是 {"error","trace"}。**不能返回 null**——那会让详情
        // 显示「没有可展示的结果」，而这里恰恰是最需要说清原因的地方（比如
        // 「进程重启，任务没写终态」）。trace 不带：几百行堆栈塞进抽屉没人看
        return { lines: [], warns: [r.error], changedItems: [], changedTotal: 0 };
    }
    if (!r || typeof r.checked !== "number") return null;   // 非校验任务（如 add）
    const cached = r.cached || 0;
    const fetched = typeof r.fetched === "number" ? r.fetched : r.checked - cached;
    const t = r.transitions || {};
    const lines = ["本次校验 " + r.checked + " 条：新校验 " + fetched
                   + " 条、复用缓存 " + cached + " 条"];
    // 「首次有结论」与「变成 X」分开报：库里绝大多数源从未校验过，混在一起
    // 「新增可用 2000 条」会被读成「比上次好」
    if (t.first_checked) lines.push("其中 " + t.first_checked + " 条首次有结论");
    const changes = describeChanges(t.changed);
    lines.push(changes.length ? "相对上次变化：" + changes.join("、")
                              : "相对上次：无状态变化");
    const warns = [];
    if (r.save_failures) warns.push(r.save_failures + " 条结果没能写入管理库，列表状态不会更新");
    if (r.hit_downgrades) warns.push(r.hit_downgrades + " 个源的命中判定降级（规则无法回放，已按「命中」处理）");
    // 变化**明细**。只有计数是不够的：摘要说「6 条变成失效」，用户下一步肯定是
    // 问「哪 6 条」——而列表里那 3800 行没法一眼找出这几个。
    // 后端只带前 N 条（`CHANGED_ITEMS_LIMIT`），用计数和条数一比就知道有没有截断
    const changedItems = r.changed_items || [];
    const changedTotal = Object.values(t.changed || {}).reduce((a, b) => a + b, 0);
    return { lines, warns, changedItems, changedTotal };
}

async function loadDetail(row) {
    if (row.id in details.value) return;   // 拉过就不重复拉（收起时也会触发本事件）
    try {
        const job = await api.get("/jobs/" + row.id);
        let parsed = null;
        try { parsed = job.result_json ? JSON.parse(job.result_json) : null; } catch (e) { parsed = null; }
        details.value = { ...details.value, [row.id]: buildDetail(parsed) };
    } catch (e) {
        ElMessage.error("加载任务结果失败: " + e.message);
        details.value = { ...details.value, [row.id]: null };
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

// 打开抽屉时自动展开 focusJobId 那条。**要等 load() 完**：列表还没数据时
// 表格里没有这一行，toggleRowExpansion 会静默什么都不做
watch(() => [props.modelValue, props.focusJobId], async ([open, id]) => {
  if (!open) return;
  if (!loading.value) await load();
  if (!id) return;
  const row = jobs.value.find((j) => j.id === id);
  if (row && tableRef.value) tableRef.value.toggleRowExpansion(row, true);
}, { immediate: true });

// 挂载即拉一次：徽标要在没打开过抽屉时就正确，光靠「打开时拉」拿不到
onMounted(load);

onUnmounted(() => { Object.values(stops).forEach((f) => f && f()); });

// 父组件提交完任务后调一下，让徽标立刻反映新任务
defineExpose({ refresh: load });
</script>

<template>
  <el-drawer v-model="visible" title="任务" direction="rtl" size="620px">
    <div class="bar">
      <span class="muted">最近 50 条任务；未跑完的会实时刷新进度</span>
      <span class="grow" />
      <el-button size="small" :loading="loading" @click="load">刷新</el-button>
    </div>

    <el-table ref="tableRef" :data="jobs" v-loading="loading" border size="small"
              style="margin-top: 10px"
              @expand-change="loadDetail">
      <!-- 展开看结果摘要与**变化明细**。列表页那条结果条只报计数，
           「哪几条变了」要看这里 -->
      <el-table-column type="expand" width="34">
        <template #default="{ row }">
          <div class="job-detail">
            <template v-if="details[row.id]">
              <div v-for="(line, i) in details[row.id].lines" :key="i">{{ line }}</div>
              <div v-for="(w, i) in details[row.id].warns" :key="'w' + i" class="warn">{{ w }}</div>
              <!-- 变化明细：光有「6 条变成失效」这个计数，下一步必然是问「哪 6 条」 -->
              <div v-if="details[row.id].changedItems.length" class="changes">
                <div class="muted head">
                  状态变化（{{ details[row.id].changedItems.length }} / {{ details[row.id].changedTotal }} 条）：
                </div>
                <div v-for="(c, i) in details[row.id].changedItems" :key="'c' + i" class="chg">
                  <!-- 名字可能重复、也可能为空，**地址才是源的身份**——所以它直接
                       显示出来而不是塞进 tooltip（原来只有悬停才看得到，等于没给） -->
                  <span class="nm" :title="c.name">{{ c.name || "（无名）" }}</span>
                  <span class="muted url" :title="c.url">{{ c.url }}</span>
                  <span class="muted">{{ healthLabel(c.from) }}</span>
                  <span class="muted">→</span>
                  <span :class="'to-' + c.to">{{ healthLabel(c.to) }}</span>
                </div>
              </div>
            </template>
            <span v-else-if="details[row.id] === null" class="muted">没有可展示的结果</span>
          </div>
        </template>
      </el-table-column>
      <el-table-column prop="id" label="job_id" width="120" />
      <el-table-column prop="kind" label="类型" width="80" />
      <el-table-column label="状态" width="94" align="center">
        <template #default="{ row }">
          <el-tag size="small" :type="statusType(row.status)">{{ row.status }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="进度" min-width="150">
        <template #default="{ row }">
          <el-progress :percentage="pct(row)" :stroke-width="8"
                       :status="row.status === 'failed' ? 'exception' : undefined" />
          <span class="muted">{{ row.progress }} / {{ row.total }}</span>
        </template>
      </el-table-column>
      <el-table-column prop="updated_at" label="更新时间" width="150" />
      <template #empty>
        <el-empty description="还没有任务" :image-size="70" />
      </template>
    </el-table>
  </el-drawer>
</template>

<style scoped>
.job-detail {
  padding: 2px 10px;
  font-size: 13px;
  line-height: 1.8;
}
.job-detail .warn {
  color: var(--el-color-danger);
}
/* 变化明细：最多 200 条，给个高度上限免得把抽屉撑到几屏 */
.job-detail .changes {
  margin-top: 6px;
  max-height: 220px;
  overflow-y: auto;
  border-top: 1px dashed var(--el-border-color-lighter);
  padding-top: 4px;
}
.job-detail .changes .head { font-size: 12px; }
.job-detail .changes .chg {
  display: flex;
  gap: 6px;
  align-items: baseline;
  font-size: 12px;
  line-height: 1.7;
}
.job-detail .changes .chg .nm {
  flex: 0 1 auto;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 300px;
}
.job-detail .changes .chg .url {
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
/* 配色与列表页的 healthType 同口径（SourcesView）：同一个状态在两处该长一样。
   结论的两极是可用/已失效；需登录是「能自己处理」的黄；
   需翻墙与待验证是「等下一步」的灰。 */
.job-detail .changes .chg .to-ok { color: var(--el-color-success); }
.job-detail .changes .chg .to-dead { color: var(--el-color-danger); }
.job-detail .changes .chg .to-auth,
.job-detail .changes .chg .to-cert { color: var(--el-color-warning); }
.job-detail .changes .chg .to-gfw,
.job-detail .changes .chg .to-pending { color: var(--el-color-info); }
</style>
