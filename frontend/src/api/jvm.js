import { api } from "./client";

// JVM 校验服务（TODO §2.1 S2）。自检只读；run 同步等完成（全量约 17 分钟）；
// results 是最近一批结论（按 URL 去重），供来源阶梯展示。
//
// `jvmRun({urls})`：只跑这几条源（列表页勾选的）。**不传或空数组 = 全部在用源**，
// 那时才受设置里的「条数上限」约束；选了具体几条时范围就由选中决定。
export const jvmSelftest = () => api.get("/jvm/selftest");
export const jvmRun = (body = {}) => api.post("/jvm/run", body);
export const jvmResults = () => api.get("/jvm/results");
