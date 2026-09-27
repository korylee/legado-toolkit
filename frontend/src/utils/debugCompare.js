// 两次调试结果的对比与记录（ux-debug-loop）。
//
// 调试是「改一点 → 跑 → 和上次比」的循环，重跑不许清掉上一份结论。这里放
// **纯函数**：新旧结果的逐段对比、以及「新结果到来时对比基线怎么滚动」——
// 组件只消费状态，判定逻辑可被 node 测试钉住（同 verifyFreshness 那条路）。
//
// 对比键是 **（段名, URL）**：同一段重跑后拿到不同 URL（搜索第一条变了、
// 章节翻页了）要标「换了目标」，不与「值变了」混着报。
//
// 历史容器一次定型为「最近 K 次」：只留步骤摘要与结论（完整 HTML 太重，
// 只活在当前这份 result 里）。

export const RUN_HISTORY_MAX = 5;

const STATUS_LABELS = {
  same: "与上次相同",
  changed: "变了",
  regressed: "新失败",
  moved: "换了目标",
  new: "新出现",
};

export function statusLabel(status) {
  return STATUS_LABELS[status] || status;
}

/** 精简一份结果：对比只认（段名, URL, verdict, detail），页面与值列表不留。 */
export function slimRunResult(result) {
  if (!result) return null;
  return {
    source: result.source || "",
    all_ok: result.all_ok,
    steps: (result.steps || []).map((s) => ({
      name: s.name,
      url: s.url || "",
      verdict: s.verdict || "",
      detail: s.detail || "",
      reason: s.reason || "",
    })),
  };
}

/**
 * 新结果到来时滚动对比基线。
 *
 * - cur 没有 steps（纯错误体）：界面根本没有可对比的结论，**基线不动**——
 *   否则一次网络失败就把用户手里最后一份真实结果从对比里抹掉。
 * - old 有 steps：它就是「上一份」，精简后成为基线并进历史。
 */
export function nextCompareState(state, oldResult, curResult) {
  if (!curResult || !(curResult.steps || []).length) return state || { prev: null, history: [] };
  const hadSteps = oldResult && (oldResult.steps || []).length;
  if (!hadSteps) return state || { prev: null, history: [] };
  const slim = slimRunResult(oldResult);
  return {
    prev: slim,
    history: [slim, ...((state && state.history) || [])].slice(0, RUN_HISTORY_MAX),
  };
}

/**
 * 逐段对比：返回与 cur.steps 对齐的 `{name, status}` 列表。
 *
 * status：same（与上次相同）/ changed（变了）/ regressed（上次没失败这次失败，
 * 即「新失败」）/ moved（URL 换了目标；结论也变了仍是 moved，换目标是更重要
 * 的事实）/ new（上一份没有这一段）。
 */
export function compareRuns(prev, cur) {
  const prevBy = new Map(((prev && prev.steps) || []).map((s) => [s.name, s]));
  return ((cur && cur.steps) || []).map((s) => {
    const p = prevBy.get(s.name);
    if (!p) return { name: s.name, status: "new" };
    const moved = String(p.url || "") !== String(s.url || "");
    if (moved) return { name: s.name, status: "moved" };
    // 「新失败」只认 **pass → fail**：unknown（比如空规则的「没验」）从来不是
    // 好的，掉成 fail 不算退步，标「变了」
    const wasPass = p.verdict === "pass";
    const nowFail = s.verdict === "fail";
    if (wasPass && nowFail) return { name: s.name, status: "regressed" };
    const changed = String(p.verdict || "") !== String(s.verdict || "")
      || String(p.detail || "") !== String(s.detail || "");
    return { name: s.name, status: changed ? "changed" : "same" };
  });
}
