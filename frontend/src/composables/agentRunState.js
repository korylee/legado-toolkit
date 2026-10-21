// 一轮 AI 补全的**状态本体**：只放 ref，不放行为。
//
// 为什么单独一个文件：调试工作台与 AI 补全弹窗是两个组件，但看的是同一轮补全。状态
// 声明在这里 = 它在模块作用域，谁取用拿到的都是同一组 ref；写在 `useAgentRun()` 函数
// 体里则每次调用新建一份——后果不是报错，是「工作台提交的那一轮，弹窗里看不到」
// （2026-10-20 真发生过一次，构建与既有单测全绿）。
//
// 它同时是**叶子件**：只依赖 `vue`，不带任何相对 import。单测因此能直接加载它，
// 不必为了一条断言去跑一次打包（本仓既有模块之间的 import 不带扩展名，Node 解析
// 不了，那条路只能走打包）。
import { ref } from "vue";

//: 这一轮的任务号。空 = 没有这一轮（弹窗据它决定显示什么）
export const jobId = ref("");
export const running = ref(false);
//: 任务行（状态徽章）：流帧优先，其次详情（从任务中心打开时还没有帧——那条流是
//: 观测，不是身份；没有帧就说没有，而不是编一个「排队中」）
export const jobRow = ref({});
//: 提交时带的字段（空 = 整组）：首屏在没有事件之前就要把字段列出来
export const fields = ref([]);
//: 停止原因 / 提交失败原因（原样，不加工）
export const error = ref("");
//: 收尾摘要（`ai_done` 或结果体）
export const summary = ref("");
//: 结果体（终态拉一次；`null` = 还没拿到）
export const result = ref(null);
export const detailError = ref("");
//: 这一步（弹窗表头说它是哪一步的补全）
export const step = ref("");
//: 起这一轮的那次调试任务（重挂时要对上，见 resume）
export const debugJobId = ref("");
