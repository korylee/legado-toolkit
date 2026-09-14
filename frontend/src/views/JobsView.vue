<script setup>
import { ref, onMounted, onUnmounted } from "vue";
import { ElMessage } from "element-plus";
import { api, subscribeJob } from "../api/client";

const kinds = ref([]);
const jobs = ref([]);
const live = ref({});          // job_id -> 最新事件
const stops = ref({});

async function loadKinds() {
  try { kinds.value = await api.get("/jobs/kinds"); } catch (e) { kinds.value = []; }
}

async function runPing() {
  try {
    const r = await api.post("/jobs", { kind: "ping", payload: { steps: 5, total: 5 } });
    ElMessage.success("已提交任务 " + r.job_id);
    watch(r.job_id);
  } catch (e) { ElMessage.error(e.message); }
}

function watch(jobId) {
  if (stops.value[jobId]) return;
  stops.value[jobId] = subscribeJob(
    jobId,
    (data) => { live.value = { ...live.value, [jobId]: data }; },
    (data) => { ElMessage.success("任务 " + jobId + " 完成: " + data.status); }
  );
}

onMounted(() => {
  loadKinds();
});
onUnmounted(() => { Object.values(stops.value).forEach((f) => f && f()); });
</script>

<template>
  <div class="page">
    <el-alert type="success" :closable="false" show-icon style="margin-bottom: 12px">
      所有耗时任务都走 job：POST 拿 job_id → SSE 订阅实时进度。
      check 3861 个源要几分钟，靠这条链路前端才不会超时。
    </el-alert>

    <div class="toolbar">
      <el-tag>已注册任务类型: {{ kinds.join(", ") || "(无)" }}</el-tag>
      <el-button type="primary" @click="runPing">跑一个 ping 冒烟任务</el-button>
    </div>

    <el-table :data="Object.entries(live).map(([id, d]) => ({ id, ...d }))" border size="small">
      <el-table-column prop="id" label="job_id" width="140" />
      <el-table-column prop="kind" label="类型" width="100" />
      <el-table-column prop="status" label="状态" width="110">
        <template #default="{ row }">
          <el-tag size="small" :type="row.status === 'done' ? 'success'
            : row.status === 'failed' ? 'danger' : 'warning'">{{ row.status }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="进度" min-width="240">
        <template #default="{ row }">
          <el-progress :percentage="row.total ? Math.round((row.progress / row.total) * 100) : 0"
                       :status="row.status === 'failed' ? 'exception' : undefined" />
          <span class="muted">{{ row.progress }} / {{ row.total }}</span>
        </template>
      </el-table-column>
      <el-table-column prop="updated_at" label="更新时间" width="160" />
    </el-table>
  </div>
</template>
