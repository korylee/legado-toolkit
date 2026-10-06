import { api } from "./client";

// JVM 校验服务（TODO §2.1 S2）。自检只读；run 提交为后台任务（跑批分钟级，
// 进度与逐块耗时看任务抽屉的时间线）；results 是最近一批结论（按 URL 去重），
// 供来源阶梯展示。
//
// `jvmRun({urls})`：只跑这几条源（列表页勾选的）。**不传或空数组 = 全部在用源**，
// 传了具体几条时范围就由选中决定。
export const jvmReadiness = () => api.get("/jvm/readiness");
export const pickJvmAppRepo = () => api.post("/jvm/pick-app-repo");
export const pickJvmAndroidSdk = () => api.post("/jvm/pick-android-sdk");
export const jvmRun = (body = {}) => api.post("/jvm/run", body);
export const jvmResults = () => api.get("/jvm/results");
