// 跨页选中的账：`selected` 是 **URL 集合**（唯一真相），表格只渲染当前页。
//
// 为什么单独放纯函数：这里的规则是「当前页的勾选怎么并进/移出集合」，而踩过的坑全在
// **触发时机**上——`selection-change` 在「翻页替换数据」时也会发（载荷里分不出是不是用户
// 操作），拿它重算会把当前页的选中全删掉：页 1 全选 → 去页 2 → 回页 1，勾选没了。所以
// 组件必须把这两个函数挂在 **只由用户点击触发** 的事件上（`@select` / `@select-all`），
// 而「翻页要做什么」只有一件事：按集合把当前页勾回去（读，不写）。

/** 单行勾/取消：只动这一条 URL，别的页一个都不碰。 */
export function toggleSelected(selected, url, on) {
  const rest = selected.filter((u) => u !== url);
  return on ? [...rest, url] : rest;
}

/**
 * 表头全选 / 取消全选：**当前页以表格为准，其他页保持原样**。
 *
 * `pageUrls` 是当前页渲染的 URL，`pickedUrls` 是事件给的当前页勾选结果（取消全选时为空）。
 * 先剔掉当前页、再并进 picked，所以「选中全部 800 条」之后点取消全选只会减掉当前页那一页，
 * 不会像 `selected = picked` 那样把没渲染的行无声丢掉（本轮修的就是这类静默丢失）。
 * 结尾的 Set 兜去重：同一个 URL 只能算一条，否则批量条上的数字会虚高。
 */
export function mergePageSelection(selected, pageUrls, pickedUrls) {
  const page = new Set(pageUrls);
  return [...new Set([...selected.filter((u) => !page.has(u)), ...pickedUrls])];
}

/**
 * 选中的源里有几条**不在当前筛选域内**（`domainUrls` = 该筛选条件返回的全部 URL，不分页）。
 *
 * 为什么要点这个数：选中集合是跨页、跨筛选累积的，而「已选 N 条」在任何筛选下都长得一样
 * ——用户看不出自己接下来会操作哪些源。两个真实场景都靠它解释：
 * ① 在「未校验」下选 50 条 → 批量校验改了状态 → 刷新后它们离开该域；
 * ② 在「未校验」下选 50 条 → 切到「可用」→ 那 50 条一条都不在眼前。
 */
export function countOutOfDomain(selected, domainUrls) {
  const domain = new Set(domainUrls);
  return selected.filter((u) => !domain.has(u)).length;
}

/** 只保留当前筛选域内的选中（批量条上「只保留当前筛选内的」那个正门）。 */
export function keepInDomain(selected, domainUrls) {
  const domain = new Set(domainUrls);
  return selected.filter((u) => domain.has(u));
}
