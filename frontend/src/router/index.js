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
    ],
  },
  // 任务/诊断两个页面已并入书源页，旧书签（#/jobs、#/dashboard）兜底回书源页，
  // 否则 vue-router 匹配不到会渲染出一片空白
  { path: "/:pathMatch(.*)*", redirect: "/sources" },
];

export default createRouter({ history: createWebHashHistory(), routes });
