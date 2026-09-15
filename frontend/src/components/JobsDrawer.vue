<script setup>
import { ref, computed, watch, onMounted, onUnmounted } from "vue";
import { ElMessage } from "element-plus";
import { api, subscribeJob } from "../api/client";

// 「任务」抽屉：替代已删掉的「任务」页。
// 打开时拉一次历史（GET /api/jobs），再对还没跑完的任务挂 SSE 实时进度。
// 原「任务」页那个「跑 ping 冒烟」按钮是调试用的，按需求不再搬运。
const props = defineProps({ modelValue: { type: Boolean, default: false } });
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
      ElMessage.success("任务完成: " + (data && data.status));
    },
  );
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

watch(() => props.modelValue, (v) => { if (v && !loading.value) load(); });

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

    <el-table :data="jobs" v-loading="loading" border size="small" style="margin-top: 10px">
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
