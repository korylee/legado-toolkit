// JVM 校验深度的文案与代价。**耗时是实测值不是估算**（lessons §四十七：性能结论必须实测）：
// 搜索档全量 3774 条 17 分钟（2026-09-30 实测，当时每块都是 Gradle 起停）；目录/正文段
// 每源多 2-4 个请求，实测 100 条抽样里正文段中位耗时 1.5s/源（p90 5.3s），且慢站会撞
// App 自己的 60s 读超时。
// 2026-10-02 小样本实测（12 源 × 7 轮，搜索档）：Gradle 每块固定开销约 17 秒（同形块
// 跨模式差分 16.3–17.2s 三次一致）、daemon 冷启动 prepare 约 7 秒（历史锚 10.7s）、
// 热复用起停约 0。与上面的全量历史数**不同口径，不可相加折算**；起停数只写在
// CheckJvmForm 的范围提示里（一处），这里不改全量行。
//
// **只有一处用它**（书源列表的校验弹框 → `CheckJvmForm.vue`，设置页没有挡位控件），
// 但仍只留一份：文案与代价是**同一份口径**，将来加第二个入口不该顺手抄一遍（AGENTS #10）。
const DEPTH_TEXT = {
  search: { label: "搜索档（最快）", cost: "全量约 17 分钟（9-30 实测）" },
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
