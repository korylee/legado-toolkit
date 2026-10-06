<script setup>
import { ref, onMounted, provide } from "vue";
import { Setting } from "@element-plus/icons-vue";
import { api } from "../api/client";
import SettingsDrawer from "../components/SettingsDrawer.vue";
import SourcesView from "../views/SourcesView.vue";
import SourceWorkspaceDrawer from "../components/SourceWorkspaceDrawer.vue";
import { createSourceWorkspace, SOURCE_WORKSPACE_KEY } from "../composables/useSourceWorkspace";

// 「任务」「诊断」两个页面已并入书源页；应用保持单页面，调试通过覆盖层打开。
const online = ref(true);
const settingsVisible = ref(false);
const workspace = createSourceWorkspace();
provide(SOURCE_WORKSPACE_KEY, workspace);

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
        <SourcesView />
      </main>
    </div>

    <SettingsDrawer v-model="settingsVisible" />
    <SourceWorkspaceDrawer />
  </div>
</template>
