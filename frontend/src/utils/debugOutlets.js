// 调试结果的产品出口：只判断「当前结论是否需要换到已有的 App 通道复检」。
// 这里不重算层，也不把明确的规则失败改说成环境问题；判据由调用方传入。

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
    kind: "app",
    label: "连 App 调试（真机复查）",
    reason: layer === "L5"
      ? "这一步需要登录态或真实网络出口，本机引擎无法判定"
      : "这一步的数据要渲染、解密或执行脚本后才有，本机引擎取不到",
  };
}
