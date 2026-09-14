<script setup>
import { ref, onMounted } from "vue";
import { useRoute } from "vue-router";
import { getStats } from "../api/sources";
import { useMobile } from "../composables/useMobile";
import { Monitor, List, DataAnalysis, Refresh } from "@element-plus/icons-vue";

const route = useRoute();
const isMobile = useMobile();
const stats = ref(null);

const menus = [
  { path: "/sources", label: "书源", icon: List },
  { path: "/jobs", label: "任务", icon: Monitor },
  { path: "/dashboard", label: "诊断", icon: DataAnalysis },
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
      <span class="muted desktop-only" v-if="stats">
        源 <b>{{ stats.sources }}</b> · 校验 <b>{{ stats.checks }}</b>
        <template v-if="stats.deleted"> · 回收站 <b>{{ stats.deleted }}</b></template>
      </span>
      <el-tag v-else-if="!isMobile" size="small" type="danger">后端未连接</el-tag>
      <span class="spacer" />
      <el-button link :icon="Refresh" @click="loadStats" />
    </header>

    <div class="app-body">
      <!-- 桌面侧栏 -->
      <aside class="app-aside desktop-only" v-if="!isMobile">
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

    <!-- 移动端底部 Tab Bar（拇指可达） -->
    <nav class="app-tabbar" v-if="isMobile">
      <router-link v-for="m in menus" :key="m.path" :to="m.path" class="tab-item"
                   :class="{ active: route.path === m.path }">
        <el-icon :size="20"><component :is="m.icon" /></el-icon>
        <span>{{ m.label }}</span>
      </router-link>
    </nav>
  </div>
</template>
