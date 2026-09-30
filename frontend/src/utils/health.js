// 健康度的显示文案——**真源是后端 core/models.py 的 HEALTH_NAMES**，经
// `/api/sources/tags/meta` 下发（tags.js 的 fetchTagMeta 存进 healthNames）。
// 前端那份逐字副本（含 emoji）已删：两边各写一遍必然漂，gfw 档的图标就漂过一次。
//
// 档位没加载时（meta 还没回来 / 挂了）healthLabel 原样返回取值、下拉为空——
// 来源页的挂载流程会先 await ensureTagMeta()，正常情况下用户看不到这个窗口；
// meta 加载失败不缓存，下次调用会重试（同 tags.js 的自愈口径）。
//
// 「未校验」（没有校验记录）**不在档位表里**：它不是一种状态，是数据缺失
// （health IS NULL，后端筛选取值 "none"）。SourcesView 单独用一个灰 chip 显示它。
import { computed } from "vue";

import { healthNames } from "./tags";

export const HEALTH_LABELS = computed(() =>
  Object.fromEntries(healthNames.value.map((x) => [x.value, x.label])));

/** 健康度的取值清单（统计条 chip、两处「健康度」下拉共用）；顺序即界面顺序，
 *  由后端 HEALTH_NAMES 的定义序决定。少一档的后果不是少个选项，而是**那种源
 *  在统计条上一个都数不到**——各 chip 之和小于总数，看着像凭空少了一批源。 */
export const HEALTH_OPTIONS = computed(() =>
  healthNames.value.map((x) => ({ value: x.value, label: x.label })));

//: 结论是**谁判的**（`checks.engine`）。取值定义在 core/models.Engine，这里是
//: 显示层副本（没有中文词表要对，只有取值要对齐）；新增取值时两处一起改。
export const ENGINE_LABELS = {
  jvm: "本机引擎",
  device: "真机",
};

//: 认不出的取值原样返回：宁可显示 "xxx" 也不要显示 undefined
export const healthLabel = (health) => HEALTH_LABELS.value[health] || health || "未知";
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
