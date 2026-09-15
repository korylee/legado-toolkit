<script setup>
import { ref, onMounted } from "vue";
import { Setting } from "@element-plus/icons-vue";
import { api } from "../api/client";
import SettingsDrawer from "../components/SettingsDrawer.vue";

// 「任务」「诊断」两个页面已并入书源页，导航只剩一项、侧栏与底部 Tab 都撤掉了。
// header 保留设置入口和后端连通性提示；源数/校验数改由书源页的统计条自己拉，
// 这里不再重复请求。
const online = ref(true);
const settingsVisible = ref(false);

async function checkBackend() {
  try {
    await api.get("/health");
    online.value = true;
  } catch (e) {
    online.value = false;
  }
}

onMounted(checkBackend);
</script>

<template>
  <div class="app-shell">
    <header class="app-header">
      <span class="title">Legado 书源管理</span>
      <el-tag v-if="!online" size="small" type="danger">后端未连接</el-tag>
      <span class="spacer" />
      <el-button link :icon="Setting" @click="settingsVisible = true" />
    </header>

    <div class="app-body">
      <main class="app-main">
        <router-view />
      </main>
    </div>

    <SettingsDrawer v-model="settingsVisible" />
  </div>
</template>
