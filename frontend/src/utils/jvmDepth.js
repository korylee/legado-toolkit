// JVM 校验深度的文案与代价。**耗时是实测值不是估算**（lessons §四十七：性能结论必须实测）：
// 搜索档全量 3774 条 17 分钟；目录/正文段每源多 2-4 个请求，实测 100 条抽样里
// 正文段中位耗时 1.5s/源（p90 5.3s），且慢站会撞 App 自己的 60s 读超时。
//
// **两处要用它**（设置 → JVM 校验的参数表单、书源列表的全量校验弹框），所以只留一份：
// 各写一份必然漂，而漂的表现是「同一个深度在两处写的时间不一样」（AGENTS #10）。
const DEPTH_TEXT = {
  search: { label: "搜索档（最快）", cost: "全量约 17 分钟（实测）" },
  toc: { label: "搜索 + 目录", cost: "每源多 2 个请求，全量预计 30 分钟以上" },
  content: { label: "搜索 + 目录 + 正文（最准）", cost: "每源再多 1 个请求，全量预计 1 小时以上" },
};

const DEPTH_HINT = {
  search: "只看「搜得到吗」：最快，但验证不出目录/正文是否可用",
  toc: "多验一层目录页能不能解析出章节（对「目录在独立页上」的源最有价值）",
  content: "再抓一章正文——最接近「这本书我能读吗」，也最慢",
};

/** 选项来自后端下发的枚举；后端没给（旧版）时退回三档默认文案，保证界面不空
 *  ——但**值本身**永远以后端为准（AGENTS #8：枚举只在后端定义）。 */
export function depthOptions(limits) {
  const vals = (limits && limits.jvm_depth) || Object.keys(DEPTH_TEXT);
  return vals.map((v) => ({ value: v, label: (DEPTH_TEXT[v] || {}).label || v }));
}

export function depthLabel(value) {
  return (DEPTH_TEXT[value] || {}).label || value;
}

export function depthHintText(value) {
  return DEPTH_HINT[value] || "";
}

export function depthCost(value) {
  return (DEPTH_TEXT[value] || {}).cost || "全量跑批";
}
