// 保存前的清洗，**唯一一份**（壳顶栏的保存动作是唯一调用点）。
// 历史上这段逻辑在编辑弹窗与调试工作台各写一遍，清洗口径一漂，
// 两条保存路径落库的书源就不一致——现在调用点也只有一处。
//
// bookSourceType：Legado 按它分派正文解析，合法取值只有 0/1/2/3；
// 脏值（""/[]/"2"/4）进 App 会直接 IllegalStateException，一律归 0（文本）。
// enabledExplore：由配置推导，不由用户手工维护（AGENTS #13）——
// 没配发现规则的源开着开关，App 的发现页里就是一个点了没反应的死项；
// 「配了但不想显示」是用户在编辑表单里写的 enabledExplore=false，这里只遵从不重判。

export function normalizeForSave(source) {
  const s = JSON.parse(JSON.stringify(source || {}));
  s.bookSourceType = Number(s.bookSourceType);
  if (![0, 1, 2, 3].includes(s.bookSourceType)) s.bookSourceType = 0;
  const hasExploreConfig = !!(String(s.exploreUrl || "").trim()
    || Object.keys(s.ruleExplore || {}).length);
  s.enabledExplore = hasExploreConfig && !!s.enabledExplore;
  return s;
}
