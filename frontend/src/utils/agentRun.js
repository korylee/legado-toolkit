// AI 补全这一轮的事实 → 界面词与逐字段状态。**唯一一份**（对标 utils/jobTimeline）。
//
// 后端只给码与事实（`core/agent_loop` 的 `ai_family` / `ai_try` / `ai_accepted` /
// `ai_failed` / `ai_family_failed` / `ai_done`），这里负责说成人话并归并成一行一字段。
// 两处读者：AI 补全弹窗（逐字段四态）与写回工作台的输入框。过程行与落框原来都散在
// DebugWorkbench 里，弹窗要第二份时就会被抄成两份——分歧正好落在不报错的那一侧。
// 带扩展名是刻意的：本件被 `agentRun.test.js` 直接用 `node --test` 加载，Node 的
// ESM 解析不补扩展名（Vite 两种都认）——省掉它，测试会死在 import 解析而不是断言。
import { RULE_FIELD_DEFS, AUTOFILL_RULE_FIELDS, STEP_LABELS } from "./steps.js";

//: 状态码（渲染与「再补哪些字段」都只认这几个值，不认文案）
export const AGENT_STATES = ["proposed", "accepted", "failed", "pending"];

/** 后端字段键（FILL_SPECS 蛇形键）→ 中文名：「书名」。
 *
 * 词表只有 `RULE_FIELD_DEFS` 一份：按它的 `key`（= FILL_SPECS 的蛇形键）认，取标签
 * 末段——弹窗表头已经说了是哪一步，字段名再带一次步名就是复述。认不出的键**原样
 * 透出**，不编。
 *
 * **不带 step 查是安全的，因为重复的键只有 `name`**（搜索步与详情步各一行），而两处
 * 标签末段都是「书名」。将来若某个重复键的两步标签末段不再相同，这里必须改成按 step
 * 过滤——否则会显示另一步的叫法，且不报错。
 */
export function fieldLabelOf(field) {
  const key = String(field || "");
  for (const def of RULE_FIELD_DEFS) {
    if (def.key !== key) continue;
    const parts = String(def.label || "").split(" · ");
    return parts[parts.length - 1] || key;
  }
  return key;
}

/** 后端字段键 → 写入草稿的规则字段路径（`name` → `ruleBookInfo.name`）。 */
export function ruleFieldOf(step, field) {
  return (AUTOFILL_RULE_FIELDS[step] || {})[field] || "";
}

/** 规则字段路径 → 后端字段键（写入落点表 `AUTOFILL_RULE_FIELDS` 的唯一反查）。 */
export function autofillKeyOf(step, ruleField) {
  const map = AUTOFILL_RULE_FIELDS[step] || {};
  return Object.keys(map).find((k) => map[k] === ruleField) || "";
}

/** 事件账本里的**过程行**取词（`formatTimelineEvent` 的 `ai_*` 分支共用这一份）。
 *
 * 只认 `core/agent_loop` 真会发的码；认不出的原样透出，不伪装成已知项。
 * `tone` 与 jobTimeline 同域：info | warn | error。
 */
export function agentEventLine(ev = {}) {
  const kind = String((ev && ev.kind) || "");
  switch (kind) {
    case "ai_started": {
      const fields = (ev.fields || []).map(fieldLabelOf).join("、");
      const step = STEP_LABELS[ev.step] || ev.step || "";
      const where = step ? `（${step}）` : "";
      return fields
        ? { text: `AI 补全开始${where}：${fields}`, tone: "info" }
        : { text: `AI 补全开始${where}：这一步全部字段`, tone: "info" };
    }
    case "ai_family": {
      const fields = (ev.fields || []).map(fieldLabelOf).join("、");
      return { text: `正在提议：${fields}`, tone: "info" };
    }
    case "ai_family_failed":
      return { text: `模型调用失败：${ev.reason || "原因未记录"}`, tone: "error" };
    case "ai_try": {
      const who = fieldLabelOf(ev.field);
      const count = Number(ev.count || 0);
      return ev.ok
        ? { text: `${who}：这条规则取到 ${count} 条`, tone: "info" }
        : { text: `${who}：这条规则取不到（${ev.error || "原因未记录"}）`, tone: "warn" };
    }
    case "ai_accepted":
      return { text: `${fieldLabelOf(ev.field)}：已写入`, tone: "info" };
    case "ai_failed":
      return { text: `${fieldLabelOf(ev.field)}：没有可用的提议（${ev.reason || "原因未记录"}）`,
        tone: "error" };
    case "ai_done": {
      const parts = [`写入 ${ev.written ?? "?"} 条`, `模型 ${ev.model_calls ?? "?"} 次`,
        `验证 ${ev.rule_tries ?? "?"} 次`];
      return { text: `AI 补全结束：${parts.join(" · ")}`, tone: ev.stop ? "warn" : "info" };
    }
    default:
      return { text: kind, tone: "info" };
  }
}

/** 一轮的事件账本 → 逐字段状态（`{field, label, state, tries, count, error, rule}`）。
 *
 * 状态判据（**只有这一份**）：
 *   accepted  `ai_accepted` 落到这个字段——规则写进去了；
 *   failed    `ai_failed`，或者 `ai_try` 过但没被接受——都是「这条取不到」；
 *   proposed  有 `ai_try` 但还没有结论；
 *   pending   一个事件都没提到——还没轮到它。
 * 没有事件的字段不编状态：`fields` 由调用方给（首屏要显示全部字段），没人提过就是
 * pending，而不是从「没有失败」推出来的 accepted。
 */
export function foldAgentFields(events, fields) {
  const wanted = (Array.isArray(fields) ? fields : []).map(String);
  const order = wanted.length ? wanted : [];
  const byField = new Map(order.map((f) => [f, {
    field: f, label: fieldLabelOf(f), state: "pending",
    tries: 0, count: 0, error: "", rule: "",
  }]));
  const row = (field) => {
    const key = String(field || "");
    if (!key) return null;
    if (!byField.has(key)) {
      // 事件里出现了调用方没列出的字段：照实收下（宁多不丢），排在后面
      byField.set(key, { field: key, label: fieldLabelOf(key), state: "pending",
        tries: 0, count: 0, error: "", rule: "" });
    }
    return byField.get(key);
  };
  for (const ev of Array.isArray(events) ? events : []) {
    if (!ev || !String(ev.kind || "").startsWith("ai_")) continue;
    const target = row(ev.field);
    if (!target) continue;
    if (ev.kind === "ai_try") {
      target.tries += 1;
      if (ev.ok) {
        target.count = Number(ev.count || 0);
        // 自查取到了，但还没被接受：可能是后面那条候选
        if (target.state === "pending") target.state = "proposed";
      } else {
        target.error = String(ev.error || "");
      }
    } else if (ev.kind === "ai_accepted") {
      target.state = "accepted";
      target.rule = String(ev.rule || target.rule);
      target.count = Number(ev.count || target.count || 0);
      target.error = "";
    } else if (ev.kind === "ai_failed") {
      target.state = "failed";
      target.error = String(ev.reason || "");
    }
  }
  return [...byField.values()];
}

/** 结果体兜底：事件漏了（流接晚了）也要给出同样的四态，外加**再补哪些字段**。
 *
 * 与 `foldAgentFields` 同一套状态判据，输入从事件换成 `run_loop` 的返回行：
 * `rule` + `try_result.ok` = accepted；`ai_failed` / `reason` = failed。
 * 规则文本也带出来——调用方据此把验过的提议落进草稿。
 */
export function resultAgentFields(result, fields) {
  const out = foldAgentFields([], fields);
  const index = new Map(out.map((r) => [r.field, r]));
  for (const row of (result && result.fields) || []) {
    const target = index.get(String(row.field || ""));
    if (!target) continue;
    const checked = row.try_result || null;
    const passed = !!(checked && checked.ok && row.rule);
    if (passed) {
      target.state = "accepted";
      target.rule = String(row.rule);
      target.count = Number(checked.count || 0);
      target.error = "";
    } else if (row.rule || row.ai_failed || row.reason) {
      // 有规则却没验过（或验了没选中）、或者根本没出规则：都是「这一行没补上」。
      // 单独一个 `rule` 不能算补上——落进输入框的必须是验过的（同 agent_loop 的纪律）
      target.state = "failed";
      target.rule = "";
      target.error = String(row.reason || (checked && checked.error) || "这条规则没选中");
    }
  }
  return out;
}

/** 这一轮该「再补」的字段（还没取到、也没有正在跑）：空数组 = 不渲染那个按钮。 */
export function retryFields(rows) {
  return (Array.isArray(rows) ? rows : [])
    .filter((r) => r && (r.state === "failed" || r.state === "pending") && r.field)
    .map((r) => r.field);
}

/** 预算触顶与整体停止原因（`ai_done.stop` 与结果体的 `budget` 是同一件事的两处证据）。 */
export function agentBudgetNotes(result) {
  const budget = (result && result.budget) || {};
  const parts = [];
  if (budget.model_stop) parts.push(String(budget.model_stop));
  if (budget.model_limit_reached) parts.push("模型提议次数已用完");
  if (budget.try_limit_reached) parts.push("验证次数已用完");
  return parts;
}
