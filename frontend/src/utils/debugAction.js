// DebugAction 的纯协议层：把五格决策、候选和取证意图规范成同一形状。
// 这里只描述动作与可执行性，不调用 API、不写草稿、不判定规则是否有效。
import { FIELD_OF_STEP } from "./steps.js";
import { parseEvidenceRef } from "./debugEvidenceNavigation.js";

export const ACTION_KINDS = Object.freeze([
  "edit_rule", "run_debug", "rerun", "apply_candidate", "apply_and_rerun", "probe",
  "open_evidence",]);

function text(value) {
  return String(value == null ? "" : value).trim();
}

function baseAction(kind, context = {}) {
  return {
    kind,
    step: text(context.step),
    sourceUrl: text(context.sourceUrl),
    draftRevision: context.draftRevision == null ? null : context.draftRevision,
    origin: text(context.origin) || "manual",
    evidenceRefs: Array.isArray(context.evidenceRefs) ? [...context.evidenceRefs] : [],
    requiresConfirmation: !!context.requiresConfirmation,
    requiresVerification: context.requiresVerification !== false,
  };
}

export function actionFromFix(fix, context = {}) {
  if (!fix || !fix.kind) return null;
  const action = baseAction(fix.kind, {
    ...context,
    step: context.step || fix.step,
  });
  if (fix.kind === "edit_rule") {
    action.target = text(fix.target);
    action.field = text(fix.field) || FIELD_OF_STEP[action.step] || "";
    //: 这一步没有单字段可改（`explore` 就是）：要改的是源自身的元数据，
    //: 出口是编辑视图——没有它，判据给的 `edit_rule` 在界面上就是点了没用的按钮。
    if (!action.field) action.fallback = "basic_editor";
    if (fix.layer) action.layer = text(fix.layer);
  } else if (fix.kind === "apply_candidate") {
    action.rule = text(fix.rule || fix.candidate);
    action.candidateId = text(fix.candidateId);
  } else if (fix.kind === "rerun" || fix.kind === "run_debug") {
    action.target = text(fix.target);
    action.query = text(fix.query);
  } else {
    return null;
  }
  return action;
}

export function actionFromProbe(probe, context = {}) {
  if (!probe || !probe.kind || !["app", "jvm"].includes(probe.kind)) return null;
  return {
    ...baseAction("probe", {
      ...context,
      requiresConfirmation: probe.kind === "app",
      requiresVerification: false,
    }),
    channel: probe.kind,
  };
}

export function actionFromCandidate(candidate, context = {}) {
  const rule = text(candidate && (candidate.rule || candidate.legado || candidate.css));
  if (!rule) return null;
  return {
    ...baseAction(context.rerun ? "apply_and_rerun" : "apply_candidate", {
      ...context,
      requiresConfirmation: context.origin === "agent",
      requiresVerification: context.verified !== true,
    }),
    field: text(context.field) || FIELD_OF_STEP[text(context.step)] || "",
    rule,
    candidateId: text(context.candidateId),
  };
}

export function actionFromRerun(step, context = {}) {
  const name = text(step && (step.name || step.step || step));
  return baseAction("rerun", { ...context, step: name });
}

export function actionFromEvidence(ref, context = {}) {
  const parsed = parseEvidenceRef(ref);
  if (!parsed) return null;
  return {
    kind: "open_evidence",
    step: parsed.step,
    sourceUrl: text(context.sourceUrl),
    draftRevision: context.draftRevision == null ? null : context.draftRevision,
    origin: text(context.origin) || "manual",
    evidenceRefs: [parsed.ref],
    evidenceRef: parsed.ref,
    tab: parsed.tab,
    label: parsed.label,
    requiresConfirmation: false,
    requiresVerification: false,
  };
}

/**
 * 只检查当前动作能否执行，不改变动作的意义，也不判断规则是否有效。
 * 返回值是稳定的机器码，文案由调用方决定。
 */
export function actionAvailability(action, context = {}) {
  if (!action || !ACTION_KINDS.includes(action.kind)) {
    return { enabled: false, reasonCode: "unknown_action" };
  }
  if (context.running) return { enabled: false, reasonCode: "run_in_progress" };
  //: 要发一次真实调试的动作必须有源地址。**只在上下文明确给了空值时判缺**：
  //: 没给这个事实（undefined）不等于没有（AGENTS #12），否则会把「不知道」说成「源没有地址」。
  if (["rerun", "run_debug", "apply_and_rerun"].includes(action.kind)
      && context.sourceUrl !== undefined && !text(context.sourceUrl)) {
    return { enabled: false, reasonCode: "source_missing" };
  }
  if (["rerun", "apply_and_rerun"].includes(action.kind)
      && context.canRerun === false) {
    return { enabled: false, reasonCode: "rerun_unavailable" };
  }
  if (action.kind === "edit_rule" && !text(action.field)
      && action.fallback !== "basic_editor") {
    return { enabled: false, reasonCode: "rule_field_missing" };
  }
  if (["apply_candidate", "apply_and_rerun"].includes(action.kind)
      && (!text(action.field) || !text(action.rule))) {
    return { enabled: false, reasonCode: "candidate_incomplete" };
  }
  //: 候选是在**某一版草稿**上算出来的（baseAction 记了那一版）。草稿已经改过还去应用，
  //: 写进去的规则与它当时依据的页面/规则不再对应——这正是「候选过期」。
  //: **只管候选**：重调本来就该在改完规则之后跑，不能用同一条卡住它。
  if (["apply_candidate", "apply_and_rerun"].includes(action.kind)
      && action.draftRevision != null && context.draftRevision != null
      && action.draftRevision !== context.draftRevision) {
    return { enabled: false, reasonCode: "candidate_stale" };
  }
  if (action.kind === "probe" && action.channel === "app" && context.appAvailable === false) {
    return { enabled: false, reasonCode: "app_unavailable" };
  }
  if (action.kind === "open_evidence" && !text(action.evidenceRef)) {
    return { enabled: false, reasonCode: "evidence_missing" };
  }
  return { enabled: true, reasonCode: "" };
}

export function actionWithAvailability(action, context = {}) {
  const availability = actionAvailability(action, context);
  return action ? { ...action, ...availability } : null;
}
