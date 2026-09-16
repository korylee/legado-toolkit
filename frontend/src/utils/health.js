// 健康度的显示文案。
//
// **真源是后端 core/models.py 的 HEALTH_NAMES**（core/models.py 里有注释明确
// 不许两边各存一份枚举，/sources/tags/meta 就是为此存在的）。
//
// 这里是一份**显示层**的最小副本，只为让「校验变化摘要」能把 changed 的 key
// 说成人话。没走 /tags/meta 下发是**范围限制**（本轮只动 SourcesView 与
// JobsDrawer），不是设计选择——完整的健康度下发（含 skipped）建议二期跟
// status_tags 一起做。若两边漂移，以 core/models.py 为准。
export const HEALTH_LABELS = {
  ok: "可用",
  dead: "失效",
  auth: "需验证",
  gfw: "需翻墙",
  no_search: "不可搜",
  timeout: "超时",
  error: "异常",
  skipped: "跳过",
};

//: 认不出的取值原样返回：宁可显示 "xxx" 也不要显示 undefined
export const healthLabel = (health) => HEALTH_LABELS[health] || health || "未知";

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
