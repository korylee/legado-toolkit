// 一次调试的**完整结果体**：按任务号懒取、同一任务号只取一次。
//
// 为什么需要它：`GET /rules/last-debug` 只回**元数据**（每步 name/page_id/url/verdict，
// 页面只有 page_id/url/len 没有 html）——它够拿来定位与重跑，但不够拿来「看证据」。
// 完整的那份在任务行的结果体里（`GET /jobs/{id}/detail` 的 `result`，要 `raw=1`），
// 与调试工作台读的是同一份：**证据只有这一个来源**，别在编辑页另抓页面。
//
// 取数函数由**调用方注入**（本件不 import api）：这样去重与缓存这段行为能被 node
// 测试直接驱动，也免掉「工具模块反向依赖 HTTP 门面」。
//
// 失败不缓存：下次展开再试一次（一次网络抖动不该让这个源的证据永久打不开）。

//: jobId → Promise<{steps, pages, source, error}>（存 Promise 本身：并发调用共用一次请求）
const CACHE = new Map();
//: 最多留几份。结果体带 `pages[].html`（单页上限 40 万字符），留着不淘汰的话，
//: 一次会话翻十几条源就是几十 MB 常驻——LRU 上限比"相信会话会很短"可靠。
const MAX_ENTRIES = 5;

/** 记一份并保持 LRU：重插到队尾，超出上限就丢最久没用的那份。 */
function remember(id, entry) {
  CACHE.delete(id);
  CACHE.set(id, entry);
  while (CACHE.size > MAX_ENTRIES) {
    const oldest = CACHE.keys().next().value;
    CACHE.delete(oldest);
  }
}

export function resetDebugResultCache() {
  CACHE.clear();
}

/**
 * 取这次调试的完整结果。
 *
 * @param {string} jobId 调试任务号（空 = 没有材料，直接给空结果，不发请求）
 * @param {(id: string, opts: {raw: boolean}) => Promise<object>} fetchDetail
 *        任务明细的取数函数（生产传 `getJobDetail`）
 * @returns {Promise<{steps: Array, pages: Array, source: string, error: string}>}
 */
export function debugResult(jobId, fetchDetail) {
  if (typeof fetchDetail !== "function") {
    throw new TypeError("debugResult 需要注入 fetchDetail（任务明细的取数函数）");
  }
  const id = String(jobId || "").trim();
  if (!id) return Promise.resolve({ steps: [], pages: [], source: "", error: "" });
  const hit = CACHE.get(id);
  if (hit) {
    remember(id, hit);         // 命中要顶到队尾：LRU 淘汰的是「最久没用的」
    return hit;
  }

  const pending = Promise.resolve()
    .then(() => fetchDetail(id, { raw: true }))
    .then((detail) => {
      // `raw=1` 才保证 `result` 一定下发（默认明细对某些任务只给摘要）
      const body = (detail && detail.result && typeof detail.result === "object")
        ? detail.result : {};
      const out = {
        steps: Array.isArray(body.steps) ? body.steps : [],
        pages: Array.isArray(body.pages) ? body.pages : [],
        // 这次跑在哪台引擎上（`jvm` / `app`）：只用来把「命中片段为什么是空」说准
        source: String(body.source || ""),
        error: String((detail && detail.error) || body.error || ""),
      };
      remember(id, Promise.resolve(out));
      return out;
    })
    .catch((e) => {
      CACHE.delete(id);          // 失败不缓存：下次再试
      throw e;
    });
  remember(id, pending);
  return pending;
}