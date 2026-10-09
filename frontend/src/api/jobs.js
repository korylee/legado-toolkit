// 任务（jobs）的 HTTP 门面 + **唯一一条任务流**。
//
// 为什么集中在这里：这些端点原来以裸字符串散在四个组件里（`api.get("/jobs/" + id)`），
// 而流的重连/去重/兜底更是每处各写一遍。收成一份之后，"任务怎么被观测"只有一个答案。
import { BASE, api } from "./client";

export const listJobs = (params = {}) => {
  const q = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== "") q.set(k, String(v));
  });
  const qs = q.toString();
  return api.get("/jobs" + (qs ? "?" + qs : ""));
};
//: `raw` 只在真要 `items`／`transitions` 时用（列表页的就地回填与结果条）：
//: 默认的明细投影是给界面看的摘要，不带那份上百 KB 的原始体。
export const getJobDetail = (id, { raw = false } = {}) =>
  api.get("/jobs/" + id + "/detail" + (raw ? "?raw=1" : ""));
export const submitJob = (kind, payload = {}) => api.post("/jobs", { kind, payload });
export const cancelJob = (id) => api.post("/jobs/" + id + "/cancel", {});
export const retryJob = (id) => api.post("/jobs/" + id + "/retry", {});
export const deleteJob = (id) => api.del("/jobs/" + id);

//: 任务的终态。收到其中之一就必须收尾。
const TERMINAL_STATUS = ["done", "failed", "cancelled"];
//: SSE 重连上限。**必须有**：服务端持续失败（比如任务不存在）时，EventSource 会按
//: 默认节奏一直重连——那就是无限请求。
const SSE_MAX_RETRIES = 5;

/**
 * 订阅一个任务的观测流（SSE）。返回取消函数。
 *
 * 帧是**增量**的，去重交给 `utils/runStream.mergeFrame`（按行号）。所以重连不需要
 * 额外处理：EventSource 自带重连，而它重连时服务端会**从原来的游标重发**——
 * 去重之后那正好等于续传。**不能让 `onerror` 直接 `close()`**：一次网络抖动就会
 * 永久失联，界面上的「正在校验」再也不会复位。关闭这条流只有三条路：收到终态、
 * 调用方取消、重试到上限（最后一条是兜底出口，保证 onEnd 一定被叫到）。
 *
 * 终态帧之后服务端自己关流，`onEnd` 拿到的是**已经合并完的状态**（调用方按
 * `state.job.status` 分派），不需要再回查一次状态。
 */
export function openJobStream(jobId, { onFrame, onEnd } = {}) {
  let retries = 0;
  let closed = false;
  const es = new EventSource(BASE + "/jobs/" + jobId + "/stream");

  const finish = (extraJob) => {
    if (closed) return;
    closed = true;
    es.close();
    onEnd && onEnd(extraJob || null);
  };

  // 连上了就把计数清零：中间断过几次不重要，「连续失败」才是要设限的东西
  es.onopen = () => { retries = 0; };

  es.onmessage = (e) => {
    let data = null;
    try { data = JSON.parse(e.data); } catch (err) { return; }
    if (data && data.error) {
      // 服务端明说读不到这条任务：没有终态可等，直接给一个说得出的结局
      finish({ status: "unknown", error: String(data.error) });
      return;
    }
    onFrame && onFrame(data);
    if (TERMINAL_STATUS.includes(data && data.status)) finish(null);
  };

  es.onerror = () => {
    if (closed) return;   // 终态帧先到、服务端随后关流——这条竞态我们赢了
    retries += 1;
    if (retries > SSE_MAX_RETRIES) {
      finish({ status: "unknown",
               error: "任务状态获取失败（已重连 " + SSE_MAX_RETRIES + " 次）" });
    }
  };

  return () => { closed = true; es.close(); };
}
