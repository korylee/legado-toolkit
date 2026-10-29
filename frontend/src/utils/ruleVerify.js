// 快验**一条字段规则**：调用 + 事实成形（唯一一份）。
//
// 为什么抽出来：编辑页要能就地验一个字段（像调试页那样），而调试页已经有一份实现。
// 抄第二份的后果不是报错，是两边对同一条规则给出不同结论——快验里最容易漂的正是
// 「快验测不了」与「这条规则取不到」的分辨（方向相反，比没有结论更坏，AGENTS #4）。
//
// **本件只管「调一次、把应答变成事实」**：状态（loading / 竞态代次 / 空闲计时）留在
// 各视图自己的手里——那些是它们自己的交互，不是这条规则的判据。
import { ruleTry } from "../api/rules";
import { UNFAITHFUL_TEXT } from "./debugMaterial";
import { autofillKeyOf } from "./agentRun";

/**
 * 验一条规则，返回**字段事实**：`{count, values, error}`（永不抛）。
 *
 * `error` 非空即「没通过」，正文就是原因，调用方原样显示——**不要**把它压成
 * 「取不到」：快验答不了这条（材料种类 × 规则形态不匹配）与规则真的没选中，
 * 是两个方向相反的结论。
 *
 * @param {object} p
 * @param {string} p.jobId  本机引擎那次调试的任务号（材料挂在它的结果体上）
 * @param {string} p.step   步骤名（`search` / `bookUrl` / `toc` / `content`）
 * @param {string} p.field  规则字段路径（如 `ruleSearch.bookList`）
 * @param {string} p.rule   要验的规则原文
 */
export async function verifyFieldRule({ jobId, step, field, rule } = {}) {
  const text = String(rule || "").trim();
  if (!text) {
    // 空规则点下去什么都不跑：不给原因就是静默失败
    return { count: 0, values: [], error: UNFAITHFUL_TEXT.rule_empty };
  }
  if (!jobId) {
    // 连 App 调试没有任务行（材料在后端），快验只在本机引擎通道可用
    return { count: 0, values: [], error: "快验要用本机引擎那次调试的页面材料" };
  }
  try {
    const out = await ruleTry({
      job_id: jobId,
      step,
      field: autofillKeyOf(step, field),
      rule: text,
    });
    // 快验答不了这条：说清「测不了」，不冒充「取不到」
    if (out && out.unfaithful) {
      return { count: 0, values: [],
               error: UNFAITHFUL_TEXT[out.reason_code] || String(out.reason_code || "快验测不了这条规则") };
    }
    return {
      count: Number((out && out.count) || 0),
      values: ((out && out.values) || []).map((v) => String(v)),
      error: out && out.ok ? "" : String((out && out.error) || "未知原因"),
    };
  } catch (e) {
    return { count: 0, values: [], error: String((e && e.message) || e) };
  }
}