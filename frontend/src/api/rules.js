import { api } from "./client";

// 连 App 调试：借阅读 App 内建的调试 WebSocket 跑一次完整链路。
// 与 /rules/chain（我们离线回放 CSS 规则）互补——JS 规则、cookie、webView
// 只有 App 那边跑得了。
//
// source 必须传**详情接口返回的源对象**：它的 bookSourceUrl 是导入原文，
// 后端直接拿它当调试 tag。传列表里的 source_url（规范化过，尾部斜杠/lower
// 都可能变过）App 会查不到源，**静默无响应**（App 侧是精确匹配）。
//
// port 省略 / 传 0 时后端用默认端口（App 的 HTTP 端口 + 1 = 1123）。
//
// push=true 时后端会先把源推进 App 再调试（App 侧是 REPLACE，幂等）。
// 这会**改动 App 里的书源数据**，所以只在用户显式点「推送并调试」时传。
//
// cache 是**页面缓存**策略，只管我们补抓的那几页（跑链本身还得联网，是 App 在跑）：
//   "auto"（默认）命中就用，缺失就抓 · "only" 一页都不补抓 · "refresh" 忽略缓存重抓。
// 取值就是后端 core.fetch 的那三个常量（后端按同一份枚举校验，对不上给 400）。
export const appDebug = (source, key, host, port, push = false, cache = "auto") =>
  api.post("/rules/app-debug", { source, key, host, port, push, cache });

// 本机引擎调试（S5-A4）：**App 的真引擎跑在本机**（Robolectric 里跑 App 源码），
// 不填 IP、不推送、不用预检——本机什么都有。返回体与 /rules/app-debug **同形状**
// （只有 source 是 "jvm"），所以抽屉与卡片零改动就能吃。
//
// cookie 留空就按源 URL 从**我们自己的浏览器 profile** 读登录态（A3）：登录墙的源先跑
// `scripts/jvm_login.py` 在那个 profile 里登一次，之后自动带上。
//
// cache 与连 App 那条同一个含义：只管**我们补抓的那几页**。
export const jvmDebug = (source, key, timeout = 60, cookie = "", cache = "auto") =>
  api.post("/rules/jvm-debug", { source, key, timeout, cookie, cache });

// 调试前预检：把「静默无响应」拆成 unreachable / missing / ready 三种状态。
export const appPreflight = (source, host, port) =>
  api.post("/rules/app-preflight", { source, host, port });

// 把源推送到 App（幂等）。改动 App 数据，需用户显式触发。
export const appPush = (source, host, port) =>
  api.post("/rules/app-push", { source, host, port });

// 用已抓到的 HTML 重放一步规则，**不发网络请求**。
// 改完规则立刻看判定变化用这个，比重跑整条链（要重新联网搜索）快得多。
export const replayStep = (html, rule, step, sourceType) =>
  api.post("/rules/replay-step", {
    html, rule, step, source_type: sourceType,
  });

// 离线回放整条链（本地粗验，跑不了 JS 规则）
export const chainTest = (source, keyword, detailUrl, pick) =>
  api.post("/rules/chain", { source, keyword, detail_url: detailUrl, pick });

// 让 AI 给**某一步**提候选规则。模型只提议：后端会把每条候选拿本地回放器验一遍，
// 结果里带 verified / count / samples / rule_error——「本地回放不了」的那类
// （@js: 等）只能连 App 试，前端必须分开显示，不能混进「已验证」。
// 没配模型 / 模型输出不是 JSON 都从 llm 三态（ok|off|error|dry_run）+ error 里读。
//
// **这是一个会花钱的动作，必须由用户显式触发**（同 appPush 那条边界）：
// 只在按钮的点击回调里调，不得自动调用。唯一的例外是 dry_run=true——它只跑本地的
// 「程序先挑一遍」与登录墙判断，一个模型请求都不发，所以换步骤时可以自动跑。
export const suggestRule = (body) => api.post("/rules/suggest-rule", body);
