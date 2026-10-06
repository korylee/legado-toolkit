<script setup>
import { computed, nextTick, ref, watch } from "vue";

const props = defineProps({
  events: { type: Array, default: () => [] },
});
const listEl = ref(null);
const following = ref(true);
const hasOlder = ref(false);
const lines = computed(() => props.events || []);

function onScroll() {
  const el = listEl.value;
  if (!el) return;
  following.value = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
}
function followLatest() {
  following.value = true;
  nextTick(() => {
    if (listEl.value) listEl.value.scrollTop = listEl.value.scrollHeight;
  });
}
watch(lines, () => {
  if (following.value) followLatest();
}, { deep: true });
</script>

<template>
  <section class="debug-timeline">
    <div class="debug-timeline-toolbar">
      <span class="muted">调试过程（{{ lines.length }} 条）</span>
      <el-button v-if="!following" size="small" link type="primary" @click="followLatest">
        回到最新
      </el-button>
    </div>
    <div ref="listEl" class="debug-timeline-list" @scroll="onScroll">
      <div v-for="(event, i) in lines" :key="event.seq || i" class="debug-timeline-line">
        <span v-if="event.ts" class="debug-timeline-ts">{{ event.ts }}</span>
        <span class="debug-timeline-text">{{ event.text || event.message || event }}</span>
      </div>
      <el-empty v-if="!lines.length" description="暂无调试事件" :image-size="48" />
    </div>
  </section>
</template>

<style scoped>
.debug-timeline { margin: 6px 0; }
.debug-timeline-toolbar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px; }
.debug-timeline-list { max-height: 32vh; overflow: auto; padding: 6px 8px; background: var(--app-bg); border-radius: var(--app-radius-sm); }
.debug-timeline-line { line-height: 1.65; white-space: pre-wrap; overflow-wrap: anywhere; font: 12px/1.65 Consolas, Monaco, monospace; }
.debug-timeline-ts { color: var(--app-text-muted); margin-right: 8px; }
.debug-timeline-text { color: var(--app-text); }
</style>
