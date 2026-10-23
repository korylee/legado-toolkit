// 「下一步做什么」的**消费侧**取词与事实组装。
//
// 判据一份在 `core/agent_plan.build_plan`（后端只出缺口码与动作种类）；这里只做两件事：
//   1. `planSnapshot(...)` 把工作台手上**已有的事实**拼成请求体——不猜、不补默认值：
//      读不到的事实**不带那个键**（带了会让判据多出一档凭空结论，AGENTS #12）；
//   2. `gapLine(plan)` 按缺口码取一句人话。认不出的码**原样显示**：那是对端多了一个码，
//      藏起来就没人知道要补这一条（AGENTS #4）。
//
// 事实与判据分离的收益：同一次调试里「下一步做什么」只有一处判据，界面不再自己判断。

//: 缺口码 → 一句话。**必须与 `core/agent_plan.GAP_CODES` 逐码对齐**（契约测试钉住）。
//: 句子里带可执行的东西（加什么、点哪里），不解释机制。
const GAP_TEXT = {
  no_engine: "这一步还没跑过：先跑一次本机调试",
  rule_missing: "这一步的规则是空的：先写一条再重调本步",
  page_not_recorded: "引擎没为这一步记录页面：先把它跑起来",
  no_url: "引擎没请求这一步的页面，也没有可用的上游链接：先检查上一步",
  page_fetch_missing: "页面没取回来：重调本步再试",
  webview_unsupported: "这一段用到了本机没覆盖的页面能力：连 App 调试取证",
  render_missing: '这一页要渲染才取得到正文：在该步的 URL 规则末尾加上 ,{"webView":true} 再重调本步',
  no_wanted_nodes: "这一页没有这类节点：改选择器没用，先看这一步该不该换取数方式",
  runtime_missing: "内容在页面运行时里：先取运行时材料（本机渲染或连 App 调试）",
  no_hit: "规则在这份页面上一条都没选中：改选择器，或让 AI 提议一条",
  fail_content: "取到了但判定不达标：改规则后重调本步",
  stale: "规则改过，旧结论不作数：重调本步",
  unknown: "引擎暂时判不了这一步：看材料与事件流",
};

//: 渲染声明的字面写法（URL 规则末尾的请求选项）：句子里要**逐字**出现——写成
//: 「加 webView」这种说法，用户不知道该写在哪、写成什么样，照着做还是无效配置。

/**
 * 按缺口码取一句话（没有缺口 → null）。
 *
 * 只回「是哪一档、该说什么」：界面一次只说一条（AGENTS #24），其余缺口由后端放在
 * `deferred_gaps` 里备查——**不在这里折叠展示**（现在没有折叠位，摆出来就是第二遍）。
 */
export function gapLine(plan) {
  const code = String(((plan || {}).gap || {}).code || "");
  if (!code) return null;
  return { code, text: GAP_TEXT[code] || code };
}

/**
 * 把工作台手上的事实拼成 `/agent-plan` 的请求体。
 *
 * **只带确证的事实**：`rendered` / `has_wanted` / `stats` 都只在真有值时出现。少了它们
 * 判据只是「少一档」，补一个默认值会变成「多一个凭空结论」——两者代价不同（AGENTS #12）。
 */
export function planSnapshot({ source = null, step = null, page = null,
                               layer = "", channel = "", webviewUnsupported = false } = {}) {
  const s = step && typeof step === "object" ? step : null;
  if (!s) return null;
  const name = String(s.name || "");
  const p = page && typeof page === "object" ? page : null;
  const layerVerdict = (p && p.page_layer) || null;
  const target = { step: name };
  if (name === "content") {
    // 「规则是不是空的」是全局前置：规则没写，换通道也读不出东西
    target.rule_empty = !String(((source || {}).ruleContent || {}).content || "").trim();
  }
  const pageFacts = { present: !!p };
  if (p) {
    if (typeof p.rendered === "boolean") pageFacts.rendered = p.rendered;
    if (p.render_reason) pageFacts.render_reason = String(p.render_reason);
    if (layerVerdict && typeof layerVerdict.has_wanted === "boolean") {
      pageFacts.has_wanted = layerVerdict.has_wanted;
    }
    if (layerVerdict && layerVerdict.stats) pageFacts.stats = layerVerdict.stats;
  }
  const body = {
    layer: String(layer || ""),
    target,
    step: {
      verdict: String(s.verdict || ""),
      detail: String(s.detail || ""),
      page_id: String(s.page_id || ""),
      url: String(s.url || ""),
    },
    page: pageFacts,
    channel: String(channel || ""),
  };
  if (webviewUnsupported) body.webview_unsupported = true;
  return body;
}
