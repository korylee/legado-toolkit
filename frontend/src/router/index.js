import { createRouter, createWebHashHistory } from "vue-router";
import MainLayout from "../layouts/MainLayout.vue";

// 用 hash 模式：后端静态托管时不用配 rewrite 规则
const routes = [
  {
    path: "/",
    component: MainLayout,
    redirect: "/sources",
    children: [
      { path: "sources", name: "sources", component: () => import("../views/SourcesView.vue") },
      { path: "jobs", name: "jobs", component: () => import("../views/JobsView.vue"),
        meta: { title: "任务中心" } },
      { path: "dashboard", name: "dashboard", component: () => import("../views/DashboardView.vue"),
        meta: { title: "诊断看板" } },
    ],
  },
];

export default createRouter({ history: createWebHashHistory(), routes });
