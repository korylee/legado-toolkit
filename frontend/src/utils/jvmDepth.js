// JVM 校验深度的**选项文案**（下拉里那三行字）。实测资料留在这里当依据
// （lessons §四十七：性能结论必须实测）：搜索档全量 3774 条 17 分钟；目录/正文段每源多
// 2-4 个请求，实测 100 条抽样里正文段中位耗时 1.5s/源（p90 5.3s），且慢站会撞 App 自己的
// 60s 读超时。
//
// **耗时数字不进界面**：同一份代码换一批源、换台机器就不成立，而且「目录多几个请求」
// 那句在「这一批要等多久」上没有可操作性——标签已经把「最快 / 最准」的权衡说完了。
// 只有书源列表的校验弹框用这份（`CheckJvmForm.vue`）；将来加第二个入口也不该顺手抄一遍
// （AGENTS #10）。
const DEPTH_TEXT = {
  search: { label: "搜索档（最快）" },
  toc: { label: "搜索 + 目录" },
  content: { label: "搜索 + 目录 + 正文（最准）" },
};

/** 选项来自后端下发的枚举；后端没给（旧版）时退回三档默认文案，保证界面不空
 *  ——但**值本身**永远以后端为准（AGENTS #8：枚举只在后端定义）。 */
export function depthOptions(limits) {
  const vals = (limits && limits.jvm_depth) || Object.keys(DEPTH_TEXT);
  return vals.map((v) => ({ value: v, label: (DEPTH_TEXT[v] || {}).label || v }));
}
