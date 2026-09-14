<script setup>
import { ref, onMounted } from "vue";
import { useRoute } from "vue-router";
import { getStats } from "../api/sources";
import { Monitor, Document, Upload, Download, List, DataAnalysis } from "@element-plus/icons-vue";

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

onMounted(async () => {
  try { stats.value = await getStats(); } catch (e) { stats.value = null; }
});
</script>

<template>
  <el-container style="height: 100%">
    <el-header class="app-header">
      <span class="title">Legado 书源管理</span>
      <span class="muted" v-if="stats">
        源 {{ stats.sources }} · 校验 {{ stats.checks }}
      </span>
      <el-tag v-else size="small" type="danger">后端未连接</el-tag>
    </el-header>
    <el-container>
      <el-aside width="200px">
        <el-menu :default-active="route.path" router>
          <el-menu-item v-for="m in menus" :key="m.path" :index="m.path">
            <el-icon><component :is="m.icon" /></el-icon>
            <span>{{ m.label }}</span>
          </el-menu-item>
        </el-menu>
      </el-aside>
      <el-main style="padding: 0">
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>

<style scoped>
.app-header {
  display: flex; align-items: center; gap: 16px;
  background: #fff; border-bottom: 1px solid #e4e7ed;
}
.title { font-weight: 600; }
</style>
