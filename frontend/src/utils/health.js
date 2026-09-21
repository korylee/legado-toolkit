// 健康度的显示文案。
//
// **真源是后端 core/models.py 的 HEALTH_NAMES**（core/models.py 里有注释明确
// 不许两边各存一份枚举，/sources/tags/meta 就是为此存在的）。
//
// 2026-09 档位重设计后这份副本与后端**已完全一致**（以前 gfw 用 🌐 而后端写 🔒，
// 那处差异随改名一起消掉了：现在六档的 emoji 各不相同）。
//
// 没走 /tags/meta 下发是**范围限制**（本轮只动 SourcesView 与 JobsDrawer），
// 不是设计选择——完整的健康度下发建议二期跟 status_tags 一起做。
// 若两边漂移，以 core/models.py 为准。
//
// **6 个状态一个不能少**：统计条的 chip、表格的标签都从这份派生。少一个的后果
// 不是少个标签，而是那种源的计数在整个统计条上都不出现（见 SourcesView）。
//
// 「未校验」（没有校验记录）**不在这个表里**：它不是一种状态，是数据缺失
// （health IS NULL，后端筛选取值 "none"）。SourcesView 单独用一个灰 chip 显示它。
export const HEALTH_LABELS = {
  ok: "✅可用",
  dead: "❌已失效",
  auth: "🔒需登录",
  gfw: "🌐需翻墙",
  pending: "❓待验证",
};

/**
 * 健康度的取值清单（统计条 chip、两处「健康度」下拉、批量校验的范围提示共用）。
 *
 * **从 HEALTH_LABELS 派生，别另抄一份名字**；顺序就是界面上的顺序。
 * 少一档的后果不是少个选项，而是**那种源在统计条上一个都数不到**——各 chip 之和
 * 小于总数，看着像凭空少了一批源，而下钻不到就没法批量处理（历史上这里确实有过
 * 两份各只列 4 档的副本）。
 */
export const HEALTH_OPTIONS = ["ok", "auth", "gfw", "pending", "dead"]
  .map((value) => ({ value, label: HEALTH_LABELS[value] }));

//: 结论是**谁判的**（`checks.engine`）。取值定义在 core/models.Engine，这里是
//: 显示层副本（同 HEALTH_LABELS 的关系）；新增取值时两处一起改。
export const ENGINE_LABELS = {
  local: "本地引擎",
  jvm: "本机引擎",
  device: "真机",
};

//: 认不出的取值原样返回：宁可显示 "xxx" 也不要显示 undefined
export const healthLabel = (health) => HEALTH_LABELS[health] || health || "未知";
export const engineLabel = (engine) => ENGINE_LABELS[engine] || engine || "未记录";

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
