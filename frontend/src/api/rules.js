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
export const appDebug = ({
  source,
  target,
  query,
  host,
  port,
  push = false,
  cache = "auto",
  signal = null,
}) =>
  api.post(
    "/rules/app-debug",
    { source, target, query, host, port, push, cache },
    signal ? { signal } : {},
  );

// 本机引擎调试（S5-A4）：**App 的真引擎跑在本机**（Robolectric 里跑 App 源码），
// 不填 IP、不推送、不用预检——本机什么都有。
//
// **这是一次提交**：返回 `{job_id}`（调试已经是任务，kind="jvm_debug"），过程与结果
// 都从那一条观测流里看（`useJobs.watchJob`），跑完的返回体在 `getJobDetail(job_id)`
// ——形状与 /rules/app-debug 同（只有 source 是 "jvm"），所以抽屉与卡片零改动就能吃。
//
// cookie 留空就按源 URL 从**我们自己的浏览器 profile** 读登录态（A3）：登录墙的源先跑
// `scripts/jvm_login.py` 在那个 profile 里登一次，之后自动带上。
//
// cache 与连 App 那条同一个含义：只管**我们补抓的那几页**。
//
// target + query 语义化入参，与上面 appDebug 同一约定：key 由后端
// `core/debug_keys` 拼装，前端不拼。
//
// timeout **不传**（null）就由后端按段取（`debug.stage_timeout` / `debug.chain_timeout`）
// ——默认值只有后端一份（AGENTS #8）；前端曾经写死 60，与桥的渲染上限同值、互相掐死。
export const jvmDebug = ({
  source,
  target,
  query,
  timeout = null,
  cookie = "",
  cache = "auto",
  signal = null,
}) =>
  api.post(
    "/rules/jvm-debug",
    timeout == null
      ? { source, target, query, cookie, cache }
      : { source, target, query, timeout, cookie, cache },
    signal ? { signal } : {},
  );

// 调试前预检：把「静默无响应」拆成 unreachable / missing / ready 三种状态。
export const appPreflight = (source, host, port) =>
  api.post("/rules/app-preflight", { source, host, port });

// 把源推送到 App（幂等）。改动 App 数据，需用户显式触发。
export const appPush = (source, host, port) =>
  api.post("/rules/app-push", { source, host, port });

// 规则域的结构事实（规则组 → 求值步骤）。判据的唯一一份在 core/verify.py，
// 前端抄一份的话后端改了求值位置就静默判错新鲜度。
export const rulesMeta = () => api.get("/rules/meta");

// 轻量验证（rule_try）：拿这一步的整页 HTML 直跑一条建议规则（不跑调试链，
// 秒级），返回 {ok, values, count, error, via}。只回答「选不选得中」，
// 不替代整步验收（AGENTS #3）。只报引用 {job_id, step, rule}，材料在后端。
export const ruleTry = (body) =>
  api.post("/rules/rule-try", body);

// 这一步页面的 DOM 大纲（缩进树）：人写选择器要看标签/class/层级，而不是 30 万
// 字符原文。判据在后端 `core.repair.evidence.dom_outline`（AI 提示词同一份实现），
// 前端不解析 HTML 造第二份。返回 {text, truncated, max_lines}。
export const ruleOutline = (body) =>
  api.post("/rules/outline", body);

// 首屏「下一步做什么」的唯一判据（`core/agent_plan`）：前端只送**观测到的事实**，
// 后端只回缺口码与动作种类，中文句子在消费侧按码取词（utils/agentPlan）。
export const agentPlan = (body) =>
  api.post("/rules/agent-plan", body);

// Agent 循环（一轮）：**AI 段是一次任务**——提交后返回 {job_id}，过程从那条观测流
// 看（events 里有 ai_family / ai_try / ai_accepted / ai_failed），模型调用的输入输出
// 落该任务的 llm.jsonl（`/api/jobs/{id}/llm-calls`）。跑完的结果体走 getJobDetail。
// use_ai=false 时仍是免费段的同步返回（纯计算，秒级）。花钱动作，用户显式点击触发。
export const agentLoop = (body) =>
  api.post("/rules/agent-loop", body);

// 这次 AI 补全用过的模型调用（摘要列表；全文按 seq 单取 `/llm-calls/{seq}`）。
export const jobLlmCalls = (jobId, after = 0) =>
  api.get(`/jobs/${jobId}/llm-calls?after=${after}`);

export const jobLlmCallDetail = (jobId, seq) =>
  api.get(`/jobs/${jobId}/llm-calls/${seq}`);
