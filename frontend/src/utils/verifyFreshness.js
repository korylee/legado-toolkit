// 生成后验证的**新鲜度**判定（strengthen-src）：验证结论是「生成时那一版规则」的，
// 改完规则要重验才作数。改了哪组规则就只让映射到的步骤过期——分步粒度，
// 别整份作废（右列 testStale 已是整份粒度），也别让用户拿旧结论当现状保存。
//
// 规则组 → 步骤的映射是后端 verify 链的领域事实（「bookUrl 规则在搜索页求值」，
// proj-3-bookurl），唯一一份在 core/verify.py，经 GET /api/rules/meta 下发——
// 前端抄一份的话，后端改了求值位置这里会静默判错。取数走**动态 import**：
// node 测试直接加载本模块（不经过 api/client），下发只在浏览器发生。

let GROUP_TO_STEPS = null;   // ensureRuleSteps 填充；null = 还没加载

/** 下发口：拿到后本会话不再请求；失败不缓存（下次调用重试，同 ensureTagMeta）。 */
export async function ensureRuleSteps() {
  if (GROUP_TO_STEPS) return GROUP_TO_STEPS;
  const { rulesMeta } = await import("../api/rules.js");
  const meta = await rulesMeta();
  const mapping = (meta || {}).rule_group_to_steps || null;
  if (!mapping || !Object.keys(mapping).length) {
    throw new Error("rules/meta 没有下发 rule_group_to_steps");
  }
  GROUP_TO_STEPS = mapping;
  return mapping;
}

/** 验证时刻定格一份各规则组的 JSON 快照。映射没加载 → null（同「没有快照」）。 */
export function snapshotRuleGroups(form, mapping = GROUP_TO_STEPS) {
  if (!mapping) return null;
  const snap = {};
  for (const group of Object.keys(mapping)) {
    snap[group] = JSON.stringify((form || {})[group] || {});
  }
  return snap;
}

/**
 * 算出已过期的步骤名集合。
 * @param {object|null} snapshot 验证时刻的快照（snapshotRuleGroups 的返回值）
 * @param {object} current 当前的规则组
 * @param {Iterable<string>} refreshed 已点过「重新调试本步」、拿到新结论的步骤
 * @param {object} [mapping] 规则组→步骤映射；不传用下发的（node 测试显式传）
 */
export function staleVerifySteps(snapshot, current, refreshed = [], mapping = GROUP_TO_STEPS) {
  if (!mapping || !snapshot) return new Set();
  const done = new Set(refreshed);
  const out = new Set();
  for (const [group, steps] of Object.entries(mapping)) {
    if (JSON.stringify((current || {})[group] || {}) !== snapshot[group]) {
      for (const step of steps) {
        if (!done.has(step)) out.add(step);
      }
    }
  }
  return out;
}
