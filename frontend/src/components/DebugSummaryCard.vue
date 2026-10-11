<script setup>
defineProps({
  summary: { type: Object, required: true },
  running: { type: Boolean, default: false },
});
const emit = defineEmits(["action", "probe", "evidence"]);
</script>

<template>
  <section class="debug-summary" :class="'is-' + summary.status">
    <div class="debug-summary-head">
      <div>
        <b>{{ summary.step.label || "调试" }}</b>
        <span class="muted debug-summary-status">{{ summary.statusLabel }}</span>
      </div>
      <div class="debug-summary-meta">
        <el-tag v-if="summary.run.channel" size="small"
                :type="summary.run.channel === 'app' ? 'success' : 'primary'">
          {{ summary.run.channel === "app" ? "App 实测" : "本机引擎" }}
        </el-tag>
        <span v-if="summary.run.targetLabel" class="muted">
          入口：{{ summary.run.targetLabel }}
        </span>
        <span v-if="summary.run.lastRunMs" class="muted">
          用时 {{ Math.max(1, Math.round(summary.run.lastRunMs / 1000)) }} 秒
        </span>
      </div>
    </div>
    <p class="debug-summary-reason">{{ summary.reason }}</p>
    <div v-if="summary.run.query" class="debug-summary-query mono" :title="summary.run.query">
      {{ summary.run.query }}
    </div>
    <div class="debug-summary-actions">
      <el-button v-if="summary.primaryAction" size="small" type="primary"
                 :disabled="running"
                 @click="emit('action', summary.primaryAction)">
        {{ summary.primaryAction.label }}
      </el-button>
      <el-button v-if="summary.probeAction" size="small" plain
                 :disabled="running" @click="emit('probe', summary.probeAction)">
        {{ summary.probeAction.label }}
      </el-button>
      <span v-if="!summary.planReady" class="muted">
        下一步计划暂时不可用，先查看证据与事件流
      </span>
    </div>
    <div v-if="summary.evidenceActions && summary.evidenceActions.length" class="debug-summary-evidence">
      <span class="muted">相关证据</span>
      <el-button v-for="item in summary.evidenceActions" :key="item.evidenceRef"
                 size="small" link type="primary" :disabled="running"
                 @click="emit('evidence', item)">
        {{ item.label }}
      </el-button>
    </div>
    <div v-if="summary.evidenceHint" class="muted debug-summary-evidence">
      {{ summary.evidenceHint }}
    </div>
    <details v-if="summary.deferred.length" class="debug-summary-deferred">
      <summary>还有 {{ summary.deferred.length }} 个待处理问题</summary>
      <ul>
        <li v-for="(gap, i) in summary.deferred" :key="i">
          <span>{{ gap.reason }}</span><span class="muted"> · {{ gap.todo }}</span>
        </li>
      </ul>
    </details>
  </section>
</template>

<style scoped>
.debug-summary {
  margin: 0 0 var(--app-space-3);
  padding: var(--app-space-3);
  background: var(--app-surface);
  border: 1px solid var(--app-border-light);
  border-left: 3px solid var(--app-status-unknown);
  border-radius: var(--app-radius-md);
}
.debug-summary.is-running { border-left-color: var(--app-primary); }
.debug-summary.is-completed { border-left-color: var(--app-status-pass); }
.debug-summary.is-stale { border-left-color: var(--app-status-warning); }
.debug-summary.is-error, .debug-summary.is-cancelled { border-left-color: var(--app-status-fail); }
.debug-summary-head,
.debug-summary-meta,
.debug-summary-actions {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--app-space-2);
}
.debug-summary-head { justify-content: space-between; gap: var(--app-space-3); }
.debug-summary-status { margin-left: var(--app-space-2); }
.debug-summary-reason { margin: var(--app-space-2) 0 0; line-height: 1.6; }
.debug-summary-query {
  max-width: 100%;
  margin-top: 4px;
  overflow: hidden;
  color: var(--app-text-muted);
  text-overflow: ellipsis;
  white-space: nowrap;
}
.debug-summary-evidence { display: flex; align-items: center; flex-wrap: wrap; gap: var(--app-space-2); margin-top: var(--app-space-2); }
.debug-summary-actions { margin-top: var(--app-space-3); }
.debug-summary-deferred { margin-top: var(--app-space-2); color: var(--app-text-secondary); font-size: 12px; }
.debug-summary-deferred summary { cursor: pointer; color: var(--app-primary); }
.debug-summary-deferred ul { margin: 6px 0 0; padding-left: 18px; line-height: 1.7; }
</style>
