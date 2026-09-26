// 生成后验证的**新鲜度**判定（strengthen-src）：验证结论是「生成时那一版规则」的，
// 改完规则要重验才作数。改了哪组规则就只让映射到的步骤过期——分步粒度，
// 别整份作废（右列 testStale 已是整份粒度），也别让用户拿旧结论当现状保存。
// bookUrl 规则同样在搜索页求值（proj-3-bookurl），所以 ruleSearch 映射两个步骤。

export const RULE_GROUP_TO_STEPS = {
  ruleSearch: ["search", "bookUrl"],
  ruleBookInfo: ["bookUrl"],
  ruleToc: ["toc"],
  ruleContent: ["content"],
};

/** 验证时刻定格一份各规则组的 JSON 快照。 */
export function snapshotRuleGroups(form) {
  const snap = {};
  for (const group of Object.keys(RULE_GROUP_TO_STEPS)) {
    snap[group] = JSON.stringify((form || {})[group] || {});
  }
  return snap;
}

/**
 * 算出已过期的步骤名集合。
 * @param {object|null} snapshot 验证时刻的快照（snapshotRuleGroups 的返回值）
 * @param {object} current 当前的规则组
 * @param {Iterable<string>} refreshed 已点过「重新调试本步」、拿到新结论的步骤
 */
export function staleVerifySteps(snapshot, current, refreshed = []) {
  if (!snapshot) return new Set();
  const done = new Set(refreshed);
  const out = new Set();
  for (const [group, steps] of Object.entries(RULE_GROUP_TO_STEPS)) {
    if (JSON.stringify((current || {})[group] || {}) !== snapshot[group]) {
      for (const step of steps) {
        if (!done.has(step)) out.add(step);
      }
    }
  }
  return out;
}
