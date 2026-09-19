import { api } from "./client";

// JVM 校验服务（TODO §2.1 S2）。自检只读；run 同步等完成（全量约 17 分钟）；
// results 是最近一批结论（按 URL 去重），供来源阶梯展示。
export const jvmSelftest = () => api.get("/jvm/selftest");
export const jvmRun = () => api.post("/jvm/run", {});
export const jvmResults = () => api.get("/jvm/results");
