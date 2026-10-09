// 运行级能力事实的渲染层：App 实际撞上的 WebView 能力边界（`shadow` 记、侧车带出来）。
//
// 与五格决策**分开**是有意的：五格判据在后端一处（`core/agent_plan`），这里只负责
// 「这条事实怎么显示 + 它对某一步算不算数」。判据要它时，是把**事实**交上去（见
// `unsupportedForStep`），不是把句子搬过去。
// 键必须与 `ShadowBackstageWebView.recordUnsupported(...)` 的码逐词一致
// （`tests/test_jvm_debug_contract.py` 的边界码 parity 会拦住「各改一边」）。
export const WEBVIEW_UNSUPPORTED_TEXT = {
  unsupported_is_rule: "本机调试暂不支持需要页面环境的 webJs 规则",
  unsupported_is_rule_local: "这条 webJs 规则本机求值失败，需连 App 取证",
};

const FALLBACK_TEXT = "本机调试有一项 WebView 能力未覆盖";

function accepted(item) {
  return !!item && (item.phase === "debug" || item.phase === "matched");
}

/** 按（阶段, 码）聚合成可渲染的行。同一条边界一个源里会撞很多次（多页、两个阶段各一轮）。 */
export function webviewUnsupportedView(result) {
  if (!result || result.source !== "jvm"
      || !Array.isArray(result.webview_unsupported) || !result.webview_unsupported.length) {
    return null;
  }
  const byKey = new Map();
  for (const item of result.webview_unsupported) {
    if (!accepted(item)) continue;
    const code = String(item.code || "");
    const key = `${item.phase}:${code}`;
    let row = byKey.get(key);
    if (!row) {
      row = {
        phase: item.phase, code, count: 0, urls: [],
        // 码表对不上就露出原始码：不静默，但不再为此养 helper 与分支
        // （空码到不了这里：Python 侧的闸门要求 code 非空）
        text: WEBVIEW_UNSUPPORTED_TEXT[code] || `${FALLBACK_TEXT}（${code}）`,
      };
      byKey.set(key, row);
    }
    row.count += 1;
    const url = String(item.url || "").trim();
    if (url && !row.urls.includes(url)) row.urls.push(url);
  }
  // 角标：有地址就报地址（去重后最多两个 + 计数），没有地址就只报撞了几次
  return Array.from(byKey.values()).map((row) => ({
    ...row,
    where: row.urls.length
      ? (row.urls.length > 2
        ? `${row.urls.slice(0, 2).join("、")} 等 ${row.urls.length} 个地址`
        : row.urls.join("、"))
      : (row.count > 1 ? `${row.count} 处` : ""),
  }));
}

/**
 * 交给判据的**事实**：这一步走的页面撞过能力边界吗。
 *
 * 只认能归因的：阶段是 debug（主链）且 URL 与这一步的 URL 相同。归因不了就不说
 * ——把「读不到」当成「有」会给出错误的下一步（AGENTS #12）。
 */
export function unsupportedForStep(result, stepUrl) {
  const url = String(stepUrl || "").trim();
  if (!url || !result || result.source !== "jvm"
      || !Array.isArray(result.webview_unsupported)) {
    return false;
  }
  return result.webview_unsupported.some((item) => accepted(item)
    && item.phase === "debug" && String(item.url || "").trim() === url);
}

/** 标题按阶段分：主链撞边界会让那几段结论不可信，命中回填只是缺了证据。 */
export function webviewUnsupportedTitle(items) {
  return (items || []).some((item) => item.phase === "debug")
    ? "调试主链有未覆盖的 WebView 能力"
    : "命中回填有未覆盖的 WebView 能力";
}
