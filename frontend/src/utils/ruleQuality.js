// 规则质量评估：只汇总可观察证据，不替代规则回放结论。
// 分数用于候选排序和解释；最终是否可用仍以真实引擎重调结果为准。

const LEVELS = [
  { min: 85, key: "good", label: "推荐" },
  { min: 65, key: "usable", label: "需确认" },
  { min: 0, key: "bad", label: "不建议" },
];

function levelOf(score) {
  return LEVELS.find((x) => score >= x.min) || LEVELS[LEVELS.length - 1];
}

function nonEmptyValues(values) {
  return (values || []).map((v) => String(v || "").trim()).filter(Boolean);
}

/**
 * 评估一条当前规则。
 *
 * 输入中的 preview 是本地 HTML 上的选择器实测，values 是引擎实际返回值；
 * 两者故意分开，避免把本地投影误当成 App 的验收结论。
 */
export function assessRuleQuality({
  rule = "",
  preview = null,
  values = [],
  expectedCount = null,
  verdict = "",
  kind = "",
} = {}) {
  const text = String(rule || "").trim();
  const got = nonEmptyValues(values);
  const uniqueValues = new Set(got).size;
  const reasons = [];
  let score = 0;

  if (text) score += 20;
  else reasons.push("规则为空");

  const hits = Number(preview && preview.hits) || 0;
  const uniqNodes = Number(preview && preview.uniq) || 0;
  if (hits > 0) score += 20;
  else reasons.push("本地页面没有命中节点");

  if (got.length > 0) score += 25;
  else reasons.push("真实引擎没有返回有效值");

  if (got.length > 0) {
    const uniqueRatio = uniqueValues / got.length;
    score += Math.round(15 * uniqueRatio);
    if (uniqueRatio < 0.7) {
      score -= 15;
      reasons.push("结果重复较多");
    }
  } else if (uniqNodes > 0 && hits > 0) {
    // 没有引擎值时不把 DOM 命中误算成取值成功。
    reasons.push("只有 DOM 命中，尚无真实取值证据");
  }

  if (expectedCount !== null && expectedCount !== undefined && got.length > 0) {
    const n = Number(expectedCount);
    if (Number.isFinite(n) && n === got.length) score += 15;
    else reasons.push(`数量不一致（引擎 ${got.length} / 预期 ${n}）`);
  } else if (got.length > 0) {
    score += 8;
  }

  // 稳定性是候选质量的一部分：nth-of-type 和裸 tag 对改版更脆弱。
  if (text && !/:nth-(?:child|of-type)\(/i.test(text)) score += 5;
  else if (text) reasons.push("包含位置选择器，页面改版后容易失效");
  if (/^(?:tag\.)?(?:a|div|p|li|span)(?:@|$)/i.test(text)) {
    score = Math.max(0, score - 8);
    reasons.push("选择范围过宽，容易混入导航或噪声");
  }

  // pass 只作为附加证据，不允许单独把空值规则评成优质。
  if (verdict === "pass" && got.length > 0) score = Math.min(100, score + 5);
  if (verdict === "fail") reasons.push("真实引擎判定失败");
  if (verdict === "unknown") reasons.push("真实引擎暂时无法判定");

  const level = levelOf(score);
  return {
    score,
    key: level.key,
    label: level.label,
    reasons,
    metrics: {
      hits,
      uniqueNodes: uniqNodes,
      values: got.length,
      uniqueValues,
      expectedCount: expectedCount == null ? null : Number(expectedCount),
      kind,
    },
  };
}

export { LEVELS };
