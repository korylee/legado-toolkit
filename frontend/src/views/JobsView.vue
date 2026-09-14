<script setup>
import { ref, computed, onMounted, onUnmounted } from "vue";
import { ElMessage } from "element-plus";
import { Monitor } from "@element-plus/icons-vue";
import { api, subscribeJob } from "../api/client";
import { useMobile } from "../composables/useMobile";

const isMobile = useMobile();
const kinds = ref([]);
const live = ref({});          // job_id -> 最新事件
const stops = ref({});

const jobs = computed(() =>
  Object.entries(live.value).map(([id, d]) => ({ id, ...d })));

const statusType = (s) => (s === "done" ? "success" : s === "failed" ? "danger" : "warning");
const pct = (row) => (row.total ? Math.round((row.progress / row.total) * 100) : 0);

async function loadKinds() {
  try { kinds.value = await api.get("/jobs/kinds"); } catch (e) { kinds.value = []; }
}

async function runPing() {
  try {
    const r = await api.post("/jobs", { kind: "ping", payload: { steps: 5, total: 5 } });
    ElMessage.success("已提交 " + r.job_id);
    watch(r.job_id);
  } catch (e) { ElMessage.error(e.message); }
}

function watchJob(jobId) {
  if (stops.value[jobId]) return;
  stops.value[jobId] = subscribeJob(
    jobId,
    (data) => { live.value = { ...live.value, [jobId]: data }; },
    (data) => { ElMessage.success("任务完成: " + data.status); }
  );
}

onMounted(loadKinds);
onUnmounted(() => { Object.values(stops.value).forEach((f) => f && f()); });
</script>

<template>
  <div class="page">
    <el-alert type="info" :closable="false" show-icon class="card-gap">
      所有耗时任务都走 job：POST 拿 job_id → SSE 订阅实时进度。
      check 3861 个源要几分钟，靠这条链路前端才不会超时。
    </el-alert>

    <div class="bar card-gap">
      <el-tag size="small">已注册任务: {{ kinds.join(", ") || "(无)" }}</el-tag>
      <span class="grow" />
      <el-button type="primary" :size="isMobile ? 'default' : 'small'" :icon="Monitor"
                 @click="runPing">跑 ping 冒烟</el-button>
    </div>

    <!-- 移动端：卡片 -->
    <div class="card-list" v-if="isMobile">
      <div v-for="j in jobs" :key="j.id" class="src-card" style="display: block">
        <div class="row1">
          <span class="nm mono">{{ j.id }}</span>
          <el-tag size="small" :type="statusType(j.status)">{{ j.status }}</el-tag>
        </div>
        <div class="meta">
          <el-tag size="small" type="info">{{ j.kind }}</el-tag>
          <span class="muted">{{ j.progress }} / {{ j.total }}</span>
        </div>
        <el-progress style="margin-top: 8px" :percentage="pct(j)" :stroke-width="8"
                     :status="j.status === 'failed' ? 'exception' : undefined" />
      </div>
      <el-empty v-if="!jobs.length" description="还没有任务。点上面按钮跑一个试试" :image-size="80" />
    </div>

    <!-- 桌面：表格 -->
    <el-table v-else :data="jobs" border size="small">
      <el-table-column prop="id" label="job_id" width="140" />
      <el-table-column prop="kind" label="类型" width="100" />
      <el-table-column prop="status" label="状态" width="110">
        <template #default="{ row }">
          <el-tag size="small" :type="statusType(row.status)">{{ row.status }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="进度" min-width="240">
        <template #default="{ row }">
          <el-progress :percentage="pct(row)"
                       :status="row.status === 'failed' ? 'exception' : undefined" />
          <span class="muted">{{ row.progress }} / {{ row.total }}</span>
        </template>
      </el-table-column>
      <el-table-column prop="updated_at" label="更新时间" width="160" />
      <template #empty>
        <el-empty description="还没有任务" :image-size="70" />
      </template>
    </el-table>
  </div>
</template>
