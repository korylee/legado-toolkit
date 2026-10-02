import { api } from "./client";

// 连 App 调试：借阅读 App 内建的调试 WebSocket 跑一次完整链路。
// 与「本机引擎」（`jvmDebug`）互补：调试归本机引擎，真机只在登录态 / WebView /
// 网络出口上不可替代（§二）。
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
// target + query 是**语义化**入参（调试目标 + 用户输入原文）：App 的 key 语法
// 只有后端 `core/debug_keys` 一份（五类形态、手抄前缀幂等、留空回落都在那里），
// 前端不再各抄一遍。发现页留空时后端回落到 source.exploreUrl 原文。
//
// cache 是**页面缓存**策略，只管我们补抓的那几页（跑链本身还得联网，是 App 在跑）：
//   "auto"（默认）命中就用，缺失就抓 · "only" 一页都不补抓 · "refresh" 忽略缓存重抓。
// 取值就是后端 core.fetch 的那三个常量（后端按同一份枚举校验，对不上给 400）。
// 下面两个调试入口一律**对象传参**：形参七八个，位置传参在 JS 下不设防——
// 801a0ec 曾整体右移一位（signal 位吃到 "auto"，fetch 当场抛 TypeError）。
export const appDebug = ({ source, target, query, host, port, push = false,
                           cache = "auto", signal = null }) =>
  api.post("/rules/app-debug",
           { source, target, query, host, port, push, cache },
           signal ? { signal } : {});

// 本机引擎调试（S5-A4）：**App 的真引擎跑在本机**（Robolectric 里跑 App 源码），
// 不填 IP、不推送、不用预检——本机什么都有。返回体与 /rules/app-debug **同形状**
// （只有 source 是 "jvm"），所以抽屉与卡片零改动就能吃。
//
// cookie 留空就按源 URL 从**我们自己的浏览器 profile** 读登录态（A3）：登录墙的源先跑
// `scripts/jvm_login.py` 在那个 profile 里登一次，之后自动带上。
//
// cache 与连 App 那条同一个含义：只管**我们补抓的那几页**。
//
// target + query 语义化入参，与上面 appDebug 同一约定：key 由后端
// `core/debug_keys` 拼装，前端不拼。
//
// timeout **不传**（null）就吃设置里的 debug.timeout——默认值只有后端一份
// （AGENTS #8）；前端曾经写死 60，与桥的渲染上限同值、互相掐死。
export const jvmDebug = ({ source, target, query, timeout = null, cookie = "",
                           cache = "auto", signal = null }) =>
  api.post("/rules/jvm-debug",
           timeout == null
             ? { source, target, query, cookie, cache }
             : { source, target, query, timeout, cookie, cache },
           signal ? { signal } : {});

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

// 让 AI 给**某一步**提候选规则。模型只提议：后端会把每条候选拿本地回放器验一遍，
// 结果里带 verified / count / samples / rule_error——「本地回放不了」的那类
// （@js: 等）只能连 App 试，前端必须分开显示，不能混进「已验证」。
// 没配模型 / 模型输出不是 JSON 都从 llm 三态（ok|off|error|dry_run）+ error 里读。
//
// **这是一个会花钱的动作，必须由用户显式触发**（同 appPush 那条边界）：
// 只在按钮的点击回调里调，不得自动调用。唯一的例外是 dry_run=true——它只跑本地的
// 「程序先挑一遍」与登录墙判断，一个模型请求都不发，所以换步骤时可以自动跑。
export const suggestRule = (body) => api.post("/rules/suggest-rule", body);

export const verifyCandidate = (source, field, rule, target, query, timeout = null) =>
  api.post("/rules/verify-candidate", timeout == null
    ? { source, field, rule, target, query }
    : { source, field, rule, target, query, timeout });

// 规则域的结构事实（规则组 → 求值步骤）。判据的唯一一份在 core/verify.py，
// 前端抄一份的话后端改了求值位置就静默判错新鲜度。
export const rulesMeta = () => api.get("/rules/meta");

// 从已抓到的 HTML 里找候选规则（交互候选面板）。启发式的唯一一份在后端
// core/candidates；前端只在换页/换步时调一次（不逐键），点选与逐键高亮仍在本机。
// 返回 {candidates: [{rule, count, samples, hits, uniq, ratio}]}——**空列表是个
// 结论**（页面上确实没有），不是失败。
export const ruleCandidates = (html, kind, limit = 6) =>
  api.post("/rules/candidates", { html, kind, limit });

// 首屏五格决策（现状 / 解决 / 取证 / AI 补足）。判据的唯一一份在 `core/agent_plan`：
// 缺口唯一、fix 与 probe 分栏、AI 只认合格材料。这里**只提交观测到的事实**，
// 返回的 `gap` / `fix` / `probe` / `ai` 直接渲染（中文句子在前端按码取词）。
// 它不发请求、不落库、不调用模型，所以换步骤/换页面时可以自动调。
export const agentPlan = (facts) => api.post("/rules/agent-plan", facts);
