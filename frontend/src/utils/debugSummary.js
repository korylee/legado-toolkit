// 调试摘要的纯前端投影：只把已有运行事实、`agent_plan` 决策和草稿状态
// 组装成首屏视图，不在这里重新判断缺口。后端判据仍唯一来自 `core/agent_plan`，
// 中文缺口句子与动作词仍来自 `debugDecision`。

// 证据引用到 Tab 的导航只做展示排序，不重新生成缺口。
import { evidenceExpected, recommendedEvidence } from "./debugEvidenceNavigation.js";

function text(value) {
  return String(value == null ? "" : value).trim();
}

function runStatusOf({ running, result, stale }) {
  if (running) return "running";
  if (!result) return "idle";
  if (result.error) return result.cancelled ? "cancelled" : "error";
  if (stale) return "stale";
  return "completed";
}

function statusLabel(status) {
  return {
    idle: "尚未调试",
    running: "正在调试",
    completed: "调试已完成",
    stale: "结果已过期",
    error: "调试失败",
    cancelled: "已停止等待",
  }[status] || "调试状态未知";
}

function actionView(action, type) {
  if (!action || !action.kind) return null;
  return {
    ...action,
    type,
    enabled: true,
  };
}

function evidenceRefs({ step, page, events }) {
  const name = text(step?.name);
  if (!name) return [];
  const refs = [`step:${name}`];
  if (page) refs.push(`page:${name}`);
  if (text(step && step.matched_html)) refs.push(`matched:${name}`);
  if (Array.isArray(step && step.values) && step.values.length) refs.push(`values:${name}`);
  if (Array.isArray(events) && events.length) refs.push(`events:${name}`);
  return refs;
}

/**
 * 组装调试首屏摘要。
 *
 * `decision` 已经由 `buildDecision` 从后端 plan 和引擎事实生成；本函数只做
 * 状态投影与展示排序，不能根据 values/page 自己生成 gap。主动作优先采用
 * decision.fix；只有没有可改动作时才把取证 decision.probe 提到主动作位。
 */
export function buildDebugSummary(input = {}) {
  const {
    running = false,
    result = null,
    decision = null,
    step = {},
    stepLabel = "",
    page = null,
    events = [],
    stale = false,
    resultStale = false,
    channel = "",
    target = "",
    targetLabel = "",
    query = "",
    runInputAvailable = null,
    cacheLabel = "",
    elapsedMs = 0,
    lastRunMs = 0,
    lastRunAt = null,
    runLine = null,
    draftRevision = null,
    resultRevision = null,
    planReady = true,
  } = input;

  const effectiveStale = !!stale || !!resultStale
    || (draftRevision != null && resultRevision != null
      && draftRevision !== resultRevision);
  const status = runStatusOf({ running, result, stale: effectiveStale });
  const primaryAction = actionView(
    decision && (decision.fix || decision.probe),
    decision && decision.fix ? "fix" : "probe",
  );
  const probeAction = actionView(
    decision && decision.fix ? decision.probe : null,
    "probe",
  );
  const reason = running
    ? text(runLine && runLine.text) || "正在等待调试结果"
    : text(result && result.error)
      || (effectiveStale ? "规则已修改，本次调试结果不是当前草稿的验证结果"
        : text(decision && decision.gap && decision.gap.reason)
          || text(decision && decision.state && decision.state.reason)
          || "尚无可显示的调试结论");

  const run = {
    status,
    statusLabel: statusLabel(status),
    channel: text(channel),
    target: text(target),
    targetLabel: text(targetLabel),
    query: text(query),
    inputAvailable: runInputAvailable,
    cacheLabel: text(cacheLabel),
    elapsedMs: Number(elapsedMs) || 0,
    lastRunMs: Number(lastRunMs) || 0,
    lastRunAt: lastRunAt || null,
    line: text(runLine && runLine.text),
  };

  const refs = evidenceRefs({ step, page, events });
  const gapCode = decision && decision.gap && decision.gap.code;
  const evidenceActions = recommendedEvidence(
    gapCode,
    refs,
    { draftRevision, sourceUrl: "" },
  );

  return {
    status,
    statusLabel: run.statusLabel,
    reason,
    run,
    source: {
      draftRevision,
      resultRevision,
      stale: effectiveStale,
    },
    step: {
      name: text(step && step.name),
      label: text(stepLabel) || text(step && step.name),
      verdict: text(decision && decision.state && decision.state.verdict),
    },
    primaryAction,
    probeAction,
    deferred: (decision && decision.deferred_gaps) || [],
    evidenceRefs: refs,
    evidenceActions,
    evidenceHint: evidenceExpected(gapCode) && !evidenceActions.length
      ? "当前没有可跳转的相关材料"
      : "",
    planReady: !!planReady,
  };
}
