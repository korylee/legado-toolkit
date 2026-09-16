// 后端 API 薄封装。开发走 vite proxy，生产同源。
const BASE = import.meta.env.VITE_API_BASE || "/api";

async function request(path, options = {}) {
  const res = await fetch(BASE + path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) {
    let detail = "";
    try {
      const body = await res.json();
      detail = body.detail || JSON.stringify(body);
    } catch (e) {
      detail = res.statusText;
    }
    throw new Error(res.status + " " + detail);
  }
  if (res.status === 204) return null;
  return res.json();
}

export const api = {
  get: (p) => request(p),
  post: (p, body) => request(p, { method: "POST", body: JSON.stringify(body ?? {}) }),
  patch: (p, body) => request(p, { method: "PATCH", body: JSON.stringify(body ?? {}) }),
  del: (p) => request(p, { method: "DELETE" }),
};

//: 任务的终态。收到其中之一就必须收尾
const TERMINAL_STATUS = ["done", "failed", "cancelled"];

//: SSE 重连上限。**必须有**：服务端持续失败（比如任务不存在）时，
//: EventSource 会按默认节奏一直重连——那就是无限请求
const SSE_MAX_RETRIES = 5;

/**
 * SSE 订阅任务进度。返回取消函数。
 *
 * **不能让 `onerror` 直接 `close()`**——原来那句 `es.onerror = () => es.close()`
 * 是个真 bug：EventSource 自带重连，而服务端**每次连接的第一帧就是当前全量状态**
 * （`backend/api/jobs.py` 的 `gen()` 里 `last = None`，第一轮必然 `yield`）。
 * 也就是说**重连一次就等于把可能错过的终态补齐**。把重连关掉，一次网络抖动就
 * 永久失联，调用方那边的「正在校验」状态再也不会复位，只能刷新页面。
 *
 * 所以这里不加重连逻辑，只是**不去关它**。关闭订阅只有三条路：
 * 收到终态、调用方主动取消、**重试到上限**。最后一条是兜底出口——
 * 无论发生什么，`onFinish` 都必须被调用一次，否则调用方的 loading 状态没有出路。
 */
export function subscribeJob(jobId, onEvent, onFinish) {
  let retries = 0;
  let closed = false;
  const es = new EventSource(BASE + "/jobs/" + jobId + "/events");

  const finish = (data) => {
    if (closed) return;
    closed = true;
    es.close();
    onFinish && onFinish(data);
  };

  // 连上了就把计数清零：中间断过几次不重要，「连续失败」才是要设限的东西
  es.onopen = () => { retries = 0; };

  es.onmessage = (e) => {
    let data = null;
    try { data = JSON.parse(e.data); } catch (err) { return; }
    onEvent && onEvent(data);
    if (TERMINAL_STATUS.includes(data.status)) finish(data);
  };

  es.onerror = () => {
    if (closed) return;   // 终态帧先到、服务端随后关流——这条竞态我们赢了，什么都不用做
    retries += 1;
    if (retries > SSE_MAX_RETRIES) {
      // **兜底出口**：还没拿到终态也必须给调用方一个交代，
      // 否则它那边的「正在校验」会永远卡着
      finish({ status: "unknown",
               error: "任务状态获取失败（已重连 " + SSE_MAX_RETRIES + " 次）" });
    }
    // 否则什么都不做——让 EventSource 按自己的节奏重连，重连即自愈
  };

  return () => { closed = true; es.close(); };
}
