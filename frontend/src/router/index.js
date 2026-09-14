import { createRouter, createWebHashHistory } from "vue-router";
import MainLayout from "../layouts/MainLayout.vue";

// 用 hash 模式：后端静态托管时不用配 rewrite 规则
const routes = [
  {
    path: "/",
    component: MainLayout,
    redirect: "/sources",
    children: [
      { path: "sources", name: "sources", component: () => import("../views/SourcesView.vue"),
        meta: { title: "源浏览器" } },
      { path: "source/new", name: "source-new", component: () => import("../views/SourceEditView.vue"),
        meta: { title: "新建源" } },
      { path: "source/edit", name: "source-edit", component: () => import("../views/SourceEditView.vue"),
        meta: { title: "编辑源" } },
      { path: "export", name: "export", component: () => import("../views/ExportView.vue"),
        meta: { title: "导出与导入到 App" } },
      { path: "import", name: "import", component: () => import("../views/ImportView.vue"),
        meta: { title: "文件导入" } },
      { path: "jobs", name: "jobs", component: () => import("../views/JobsView.vue"),
        meta: { title: "任务中心" } },
      { path: "dashboard", name: "dashboard", component: () => import("../views/DashboardView.vue"),
        meta: { title: "诊断看板" } },
    ],
  },
];

export default createRouter({ history: createWebHashHistory(), routes });
