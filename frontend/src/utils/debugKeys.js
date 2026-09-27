// 调试 key 的拼装（useDebugSession 的纯函数部分）。
//
// key 是 App `Debug.startDebug` 的分派依据（`ViewModel:91-97` 的形态）：
//   关键字 / `发现::URL` / 绝对URL（详情起步）/ `++URL`（目录起步）/ `--URL`（只跑正文）。
// 拼错的后果不是报错，是 **App 静默无响应**——最难排查的那类，所以形态判据要纯函数化、
// 有测试钉着。

/** 幂等去前缀：用户手抄 URL 时常把 ++ / -- 一起带上，不去重就会拼成 ++++。
 *  只去**一次**（等价 Kotlin 的 removePrefix），写成 /^\++/ 会把真想要的
 *  `+++url` 也一起吃掉、改成别的意思。 */
function stripPrefix(url, prefix) {
  return url.replace(new RegExp("^" + prefix), "");
}

/** 目标 + 输入 → key。`exploreUrl` 由调用方传（发现页 URL 直接从配置取——
 *  库里大多数源都有，让用户再抄一遍是重复劳动且抄错即静默失败）。 */
export function buildKey(target, query, exploreUrl) {
  const q = String(query || "").trim();
  switch (target) {
    case "explore": {
      const url = q || String(exploreUrl || "").trim();
      return url ? `发现::${url}` : "";
    }
    case "toc": return q ? `++${stripPrefix(q, "\\+\\+")}` : "";
    case "content": return q ? `--${stripPrefix(q, "--")}` : "";
    case "info": return q;              // 详情页就是裸 URL，App 靠 isAbsUrl 认它
    // 搜索：空则用默认关键词
    default: return q || "我";
  }
}

/** 交给 App 的调试 key：详情/目录/正文**留空回落搜索**——App 会自己往下串，
 *  真想从中间切入（手里已有一本书的 URL）时填上它即可，两条路都在。 */
export function debugKeyOf(target, query, exploreUrl) {
  const q = String(query || "").trim();
  if (!q && ["info", "toc", "content"].includes(target)) {
    return "我";
  }
  return buildKey(target, q, exploreUrl);
}

/** 「从此步重跑」的 key 构造：拿上一轮结果里这一步的 URL 拼分段 key，与
 *  App `Debug.startDebug` 的 when 链一一对应：绝对URL → 详情起步（详情→目录→正文）/
 *  `++` → 目录起步 / `--` → 只跑正文；搜索/发现本来就是链头，重跑即整链。
 *
 *  URL 用**原文**：它是 App 自己请求过的地址，传回去 App 端 AnalyzeUrl 能吃
 *  （含 ,{...} 选项形态）；库内那种规范化（AGENTS #5）是给关联键用的，这里做了
 *  反而改坏 App 的目标。URL 为空返回空串——调用方据此提示「上一轮没有这一步」。 */
export function rerunKey(stepName, url) {
  const u = String(url || "").trim().replace(/^(\+\+|--)/, "");
  switch (stepName) {
    case "search": return null;         // 调用方走整链入口（debugKeyOf）
    case "explore": return u ? `发现::${u}` : "";
    case "bookUrl": return u;
    case "toc": return u ? `++${u}` : "";
    case "content": return u ? `--${u}` : "";
    default: return "";
  }
}
