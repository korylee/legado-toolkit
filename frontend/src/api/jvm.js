import { api } from "./client";

// JVM 校验服务。自检只读；run 提交为后台任务（批量校验分钟级，
// 进度与逐块耗时看任务抽屉的时间线）；results 是最近一批结论（按 URL 去重），
// 供来源阶梯展示。
//
// `jvmRun({urls})`：只跑这几条源（列表页勾选的）。**不传或空数组 = 全部在用源**，
// 传了具体几条时范围就由选中决定。
export const jvmReadiness = () => api.get("/jvm/readiness");
export const jvmDaemonStatus = () => api.get("/jvm/daemon/status");
export const stopJvmDaemon = () => api.post("/jvm/daemon/stop");
//: 预热引擎（后台跑，立即 202）。冷启动 41.5 秒，先点着能让用户填表的时间把它盖掉。
//: 409 = 引擎正被别的任务用着（那时本来就是热的），调用方当「跳过」处理，不是错误。
export const prepareJvmDaemon = () => api.post("/jvm/daemon/prepare");

export const pickJvmAppRepo = () => api.post("/jvm/pick-app-repo");
export const pickJvmAndroidSdk = () => api.post("/jvm/pick-android-sdk");
export const jvmRun = (body = {}) => api.post("/jvm/run", body);
