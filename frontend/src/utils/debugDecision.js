// 首屏五格决策的**渲染层**：把后端判据的输出翻成可直接渲染的视图模型。
//
// **这里不判任何东西**（规矩见 AGENTS #24），只做两件事：
//   ① 按缺口码取中文句子——文案留在前端这一侧（`tests/test_copy.py` 扫的就是这里），
//      所以后端只出码与动作种类，句子和码的对应由 `tests/test_agent_plan.py` 逐项钉住；
//   ② 把 `fix` / `probe` 的动作种类映射到宿主已有的 emit（不新造动作通道）。
import { buildStepSummary } from "./debugEvidence.js";
import { LAYER_INFO } from "./layers.js";

const layerFixTodo = (layer) => (LAYER_INFO[layer] || {}).action || "对照「整页源码」改规则";

/**
 * 缺口码 → 现状句 + 下一步 + 档位。**键必须与后端 `core/agent_plan.GAP_CODES` 一致**：
 * 少一个键，那条缺口在界面上就只剩一句兜底话（契约测试会拦住）。
 *
 * 档位也写在这张表里：以前另有一份 `INFO_GAPS`，两张表要同时改，漏一处只是颜色变了、
 * 没有任何测试会发现。现在一个码只有一行。
 */
export const GAP_TEXT = {
  rule_missing: { tone: "warn", text: (c) => ({
    reason: "这条源没配「" + c.label + "」规则",
    todo: c.stepName === "content" && !c.hasPage
      ? "小说源没配正文规则时，App 会把章节链接当正文，必须补一条"
      : "补一条规则后重新调试",
  }) },
  no_engine: { tone: "warn", text: () => ({
    reason: "这一步还没有真实引擎结果",
    todo: "先跑一次调试：本机引擎或连 App",
  }) },
  page_not_recorded: { tone: "warn", text: () => ({
    reason: "App 没为这一步记录页面",
    todo: "只能看 App 事件流，本地调试不了这一步",
  }) },
  no_url: { tone: "warn", text: (c) => ({
    reason: "App 没有请求这一步的页面，也没有可用的章节链接",
    todo: c.stepName === "content"
      ? "正文规则为空时 App 拿章节链接当正文，不请求新页面；"
        + "连章节链接都没有说明上一步没走通"
      : "先确认这一步的规则是否为空，补上后重新调试",
  }) },
  page_fetch_missing: { tone: "warn", text: (c) => ({
    reason: c.note || "这一步的页面没抓回来",
    todo: "没有页面就无法本地复盘，先按 App 的结果判断",
  }) },
  // 本机引擎的能力边界：动作是**换通道**，不是改规则（与 `core.agent_plan._PROBE_GAPS` 同族）
  webview_unsupported: { tone: "info", text: () => ({
    reason: "这一段 App 走了 WebView 的能力边界，本机引擎跑不了它，拿不到材料",
    todo: "换通道取证：连 App 再观测一次，再判断规则要不要改",
  }) },
  no_wanted_nodes: { tone: "warn", text: (c) => ({
    reason: "这一页没有「" + c.label + "」这类节点。链接 " + c.stats.links + " 个，图片 "
      + c.stats.images + " 个，其中带 src 的只有 " + c.stats.images_with_src + " 个",
    todo: c.layerFixTodo,
  }) },
  runtime_missing: { tone: "warn", text: (c) => ({
    reason: c.layer === "L5"
      ? "本机引擎缺少登录态或运行环境材料，当前不能判定"
      : "本机引擎没拿到渲染 / 解密后的运行时材料",
    todo: c.layerFixTodo,
  }) },
  no_hit: { tone: "warn", text: (c) => ({
    reason: "规则在这份页面上一条都没选中",
    todo: c.wantKind === "link"
      ? "页面里有 " + c.stats.links + " 个链接，可对照「整页源码」里的真实 class/id 改选择器"
      : "对照「整页源码」里的真实 class/id 改选择器",
  }) },
  fail_content: { tone: "info", text: (c) => ({
    reason: "取到了 " + c.valuesCount + " 条，但判定不达标：" + c.detail,
    todo: "问题在取到的内容而不是选择器，别在这里反复改选择器",
  }) },
  stale: { tone: "info", text: () => ({
    reason: "规则改过，这一步的结论已过期",
    todo: "重新调试本步，拿新结论再判断",
  }) },
  unknown: { tone: "info", text: (c) => ({
    reason: c.stateReason || "这一步暂时无法判定",
    todo: "看事件流与证据；确实缺材料时换通道再取",
  }) },
};

//: AI 那块的说明：材料不对（material_mismatch）与规则为空时**整块不渲染**——
//: 理由是现状行已经说过的那件事，不再重复一遍。
export const AI_HINTS = {
  no_model: "没配模型，无法使用 AI 提议。到「设置 → 模型」添加。",
  login_wall: "登录页：抓到的内容与 App 不同，改规则请用「连 App 调试」",
  no_page: "这一步没抓到页面",
  stale: "规则改过，先重新调试本步再让 AI 提规则",
  no_plan: "取不到下一步计划（本地接口不可用），先看现状与证据",
};

/** 解决（改源）的按钮词。层修法复用 `layers.js` 那张表的 `fix`，不另写一份。 */
export function fixView(fix) {
  if (!fix || !fix.kind) return null;
  if (fix.kind === "run_debug") return { kind: fix.kind, label: "去跑一次调试" };
  if (fix.kind === "rerun") return { kind: fix.kind, label: "重新调试本步" };
  if (fix.kind === "apply_candidate") {
    return { kind: fix.kind, label: "用候选规则", rule: String(fix.rule || "") };
  }
  if (fix.kind === "edit_rule") {
    if (fix.target === "rule") return { kind: fix.kind, label: "去补规则" };
    if (fix.target === "selector") return { kind: fix.kind, label: "改选择器" };
    if (fix.target === "login") return { kind: fix.kind, label: "配置登录 / 会话" };
    if (fix.target === "layer") {
      return { kind: fix.kind, label: (LAYER_INFO[fix.layer] || {}).fix || "改规则" };
    }
    return null;
  }
  return null;
}

/** 取证（换通道观测）的按钮词：**它不是解决方案**，所以词里必须带「取材料」。 */
export function probeView(probe) {
  if (!probe || !probe.kind) return null;
  if (probe.kind === "app") return { kind: "app", label: "连 App 取运行时材料" };
  if (probe.kind === "jvm") return { kind: "jvm", label: "换本机引擎取材料" };
  return null;
}

/**
 * 缺口码 → 视图。
 *
 * `ctx` 的形状（12 条句子的全部输入，改 `buildDecision` 时对照这里）：
 * `{ label, wantKind, stepName, hasPage, stats, note, detail, valuesCount,
 *    layer, layerFixTodo, stateReason }`
 */
function gapView(gap, ctx) {
  const code = String((gap && gap.code) || "");
  const entry = GAP_TEXT[code];
  const words = entry ? entry.text(ctx)
    : { reason: "这一步的判定没有可执行的原因", todo: "看证据与事件流" };
  return {
    code,
    level: entry && entry.tone === "info" ? "info" : "warn",
    reason: words.reason,
    todo: words.todo,
  };
}

function aiView(plan) {
  const ai = (plan && plan.ai) || {};
  const reasonCode = String(ai.reason_code || (plan ? "" : "no_plan"));
  return {
    eligible: !!ai.eligible,
    label: "让 AI 提规则",
    reason_code: reasonCode,
    material_kind: String(ai.material_kind || ""),
    hint: AI_HINTS[reasonCode] || "",
  };
}

function coreValueView(step) {
  const values = Array.isArray(step.values) ? step.values : [];
  const shown = values.map((value) => String(value == null ? "" : value).trim())
    .filter(Boolean).slice(0, 2).map((value) => (
      value.length > 180 ? value.slice(0, 180) + "…" : value
    ));
  return {
    values: shown,
    hint: shown.length ? "" : "本次通道未返回核心值",
  };
}

/**
 * 组装首屏视图模型。
 *
 * `plan` 是 `/rules/agent-plan` 的返回（判据）；`have` 与 `state` 走 `debugEvidence`
 * 那一份（证据来源只有一处判据）。**取不到 plan 时不自作判据**：只渲染现状与证据，
 * 并说明计划接口不可用——两套判据正是这条链路要消掉的东西。
 */
export function buildDecision(input = {}) {
  const {
    plan = null, step = {}, want = null, layer = null, stats = null,
    quality = null, channel = "", page = null, stale = false,
  } = input;
  const summary = buildStepSummary({ channel, step, page, layer, quality, stale });
  const coreValue = coreValueView(step);
  const layerKey = String((layer && layer.layer) || "");
  const notes = Array.isArray(step.notes) ? step.notes : [];
  const ctx = {
    label: (want && want.label) || step.name || "",
    wantKind: (want && want.kind) || "",
    stepName: String(step.name || ""),
    hasPage: !!page,
    stats: stats || {},
    note: String(notes.find((n) => String(n).includes("页面抓取失败")) || ""),
    detail: String(step.detail || ""),
    valuesCount: (step.values || []).length,
    layer: layerKey,
    layerFixTodo: layerFixTodo(layerKey),
    stateReason: summary.reason,
  };
  const gaps = (plan && plan.deferred_gaps) || [];
  return {
    want,
    core_values: coreValue.values,
    core_value_hint: coreValue.hint,
    layer: {
      key: layerKey,
      name: (layer && layer.info && layer.info.name) || "",
      action: (layer && layer.info && layer.info.action) || "",
      unsure: (layer && layer.unsure) || "",
      evidence: (layer && layer.evidence) || [],
    },
    state: {
      verdict: summary.verdict,
      verdictLabel: summary.verdictLabel,
      statusKey: stale ? "stale"
        : ({ pass: "pass", fail: "fail" }[summary.verdict] || "unknown"),
      stale: !!stale,
      reason: summary.reason,
    },
    have: summary.sources,
    boundaries: summary.boundaries,
    gap: plan && plan.gap ? gapView(plan.gap, ctx) : null,
    deferred_gaps: gaps.map((g) => gapView(g, ctx)),
    fix: plan ? fixView(plan.fix) : null,
    probe: plan ? probeView(plan.probe) : null,
    ai: aiView(plan),
    notes,
    plan_ready: !!plan,
  };
}

/**
 * 喂给 AI 提议的「这一步怎么了」：**与首屏同一份结论**（含其余缺口）。
 * 原来这里喂的是组件自己那份诊断——两套判据会漂，且首屏删掉它之后就没人维护了。
 */
export function decisionLines(decision) {
  if (!decision) return [];
  const out = [];
  if (decision.gap) out.push(decision.gap.reason + "（" + decision.gap.todo + "）");
  for (const g of decision.deferred_gaps || []) {
    out.push(g.reason + "（" + g.todo + "）");
  }
  return out;
}
