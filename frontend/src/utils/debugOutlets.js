// 调试结果的本机处理出口：只为本机引擎尚未取得足够材料的步骤提供下一步。
// 不重算层，也不把明确的规则失败改说成环境问题；判据由调用方传入。
// 真机复检不是本机调试主流程的一部分，由用户主动从高级入口触发。

/**
 * @param {Object} input
 * @param {string} input.channel 当前运行通道：jvm / app
 * @param {string} input.verdict 当前步骤结论：pass / fail / unknown
 * @param {string} input.layer 当前页面层级：L1-L5
 * @returns {{ kind: string, label: string, reason: string } | null}
 */
export function debugOutletFor({ channel, verdict, layer }) {
  if (channel !== "jvm" || verdict !== "unknown" || !layer || layer === "L1") {
    return null;
  }
  return {
    kind: "diagnosis",
    label: "查看本机缺口",
    reason: layer === "L5"
      ? "本机引擎缺少登录态或运行环境材料，当前不能判定"
      : "本机引擎尚未取得渲染、解密或脚本执行后的运行时材料",
  };
}
