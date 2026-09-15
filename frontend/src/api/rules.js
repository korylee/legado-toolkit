import { api } from "./client";

// 连 App 调试：借阅读 App 内建的调试 WebSocket 跑一次完整链路。
// 与 /rules/chain（我们离线回放 CSS 规则）互补——JS 规则、cookie、webView
// 只有 App 那边跑得了。
//
// source 必须传**详情接口返回的源对象**：它的 bookSourceUrl 是导入原文，
// 后端直接拿它当调试 tag。传列表里的 source_url（规范化过，尾部斜杠/lower
// 都可能变过）App 会查不到源，**静默无响应**。
//
// port 省略 / 传 0 时后端用默认端口（App 的 HTTP 端口 + 1 = 1123）。
export const appDebug = (source, key, host, port) =>
  api.post("/rules/app-debug", { source, key, host, port });
