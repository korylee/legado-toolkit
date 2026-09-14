<script setup>
import { ref, onMounted } from "vue";
import { useRoute } from "vue-router";
import { getStats } from "../api/sources";
import {
  Monitor, Document, Upload, Download, List, DataAnalysis, Refresh,
} from "@element-plus/icons-vue";

const route = useRoute();
const stats = ref(null);

const menus = [
  { path: "/sources", label: "源浏览器", icon: List },
  { path: "/source/new", label: "新建源", icon: Document },
  { path: "/export", label: "导出 / 导入到 App", icon: Upload },
  { path: "/import", label: "文件导入", icon: Download },
  { path: "/jobs", label: "任务中心", icon: Monitor },
  { path: "/dashboard", label: "诊断看板", icon: DataAnalysis },
];

async function loadStats() {
  try { stats.value = await getStats(); } catch (e) { stats.value = null; }
}
onMounted(loadStats);
</script>

<template>
  <div class="app-shell">
    <header class="app-header">
      <span class="title">Legado 书源管理</span>
      <span class="muted" v-if="stats">
        源 <b>{{ stats.sources }}</b> · 校验 <b>{{ stats.checks }}</b>
      </span>
      <el-tag v-else size="small" type="danger">后端未连接</el-tag>
      <span class="spacer" />
      <el-button link :icon="Refresh" @click="loadStats">刷新</el-button>
    </header>

    <div class="app-body">
      <aside class="app-aside">
        <el-menu :default-active="route.path" router>
          <el-menu-item v-for="m in menus" :key="m.path" :index="m.path">
            <el-icon><component :is="m.icon" /></el-icon>
            <span>{{ m.label }}</span>
          </el-menu-item>
        </el-menu>
      </aside>

      <main class="app-main">
        <router-view />
      </main>
    </div>
  </div>
</template>
