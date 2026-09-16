// 健康度的显示文案。
//
// **真源是后端 core/models.py 的 HEALTH_NAMES**（core/models.py 里有注释明确
// 不许两边各存一份枚举，/sources/tags/meta 就是为此存在的）。
//
// 这里是**显示层**的副本，与后端那份只有一处不同：`gfw` 用 🌐 而不是 🔒——
// 后端把 gfw 和 auth 都写成 🔒，两条标签在界面上长得一模一样、分不出来。
// 后端那份会写进书源分组名，改不得，所以差异留在这一侧。
//
// 没走 /tags/meta 下发是**范围限制**（本轮只动 SourcesView 与 JobsDrawer），
// 不是设计选择——完整的健康度下发（含 skipped）建议二期跟 status_tags 一起做。
// 若两边漂移，以 core/models.py 为准。
//
// **8 个状态一个不能少**：统计条的 chip、表格的标签都从这份派生。少一个的后果
// 不是少个标签，而是那种源的计数在整个统计条上都不出现（见 SourcesView）。
export const HEALTH_LABELS = {
  ok: "✅可用",
  dead: "❌失效",
  auth: "🔒需验证",
  gfw: "🌐需翻墙",
  no_search: "🔍不可搜",
  timeout: "⏱超时",
  error: "⚠️异常",
  skipped: "⏭跳过",
};

//: 认不出的取值原样返回：宁可显示 "xxx" 也不要显示 undefined
export const healthLabel = (health) => HEALTH_LABELS[health] || health || "未知";

// 星级旁边那个「这一级是实测来的，还是按规则推的」。
//
// 真源是后端 `core/checker.py` 的 `evaluate_stars`（返回 "measured" / "static" / ""）。
// **为什么要这个词**：3★ 有两种完全不同的来源——搜索实测命中 / 只是静态规则齐全——
// 而界面上长得一模一样。4★/5★ 同样可能是推的（目录正文验不了时会回退静态规则）。
//
// 空串表示「没有可标注的来源」（0★ 不可达），此时**不渲染**。
// **不要兜底成「实测」**——那正是这一维要消灭的事：把「不知道」说成「验过了」。
export const STAR_BASIS_LABELS = {
  measured: "实测",
  static: "仅规则",
};

export const starBasisLabel = (basis) => STAR_BASIS_LABELS[basis] || "";

/**
 * 把 transitions.changed 分桶转成可读文案的数组。
 *
 * 两处展示共用这一份：校验完成时的通知、任务抽屉里的结果摘要。分开写的话
 * 同一批数字在两处会变成两种说法，用户会以为是两回事。
 * 计数为 0 的桶不出现——后端本来就不下发，这里再兜一次。
 */
export function describeChanges(changed) {
  return Object.entries(changed || {})
    .filter(([, count]) => count > 0)
    .map(([health, count]) => "变成" + healthLabel(health) + " " + count + " 条");
}
