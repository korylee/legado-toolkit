// evidenceRefs 的纯前端导航映射：只决定“打开哪份已有材料”，不生成缺口或结论。
// 证据引用是逻辑 ID，后续 Agent 与后端证据服务也可以复用同一形状。

const REF_RE = /^(step|page|matched|values|events):(.+)$/;

const TAB_BY_KIND = Object.freeze({
  page: "page",
  matched: "matched",
  values: "values",
  events: "events",
});

const LABEL_BY_KIND = Object.freeze({
  page: "看整页源码",
  matched: "看命中源码",
  values: "看提取结果",
  events: "看调试事件",
});

const GAP_PREFERENCE = Object.freeze({
  no_hit: ["matched", "page"],
  fail_content: ["values"],
  page_not_recorded: ["events"],
  page_fetch_missing: ["page", "events"],
  runtime_missing: ["events"],
  unknown: ["events", "page"],
});

function text(value) {
  return String(value == null ? "" : value).trim();
}

export function parseEvidenceRef(ref) {
  const value = text(ref);
  const match = REF_RE.exec(value);
  if (!match) return null;
  const kind = match[1];
  const step = match[2];
  return {
    ref: value,
    kind,
    step,
    tab: TAB_BY_KIND[kind] || "",
    label: LABEL_BY_KIND[kind] || "定位当前步骤",
  };
}

export function evidenceAction(ref, context = {}) {
  const parsed = typeof ref === "string" ? parseEvidenceRef(ref) : ref;
  if (!parsed || !parsed.ref || !parsed.step) return null;
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
 * 依据后端已经给出的 gap code，选择当前已有引用中的推荐材料。
 * 这是展示排序，不是缺口判定；没有推荐材料时只返回空数组。
 */
export function recommendedEvidence(gapCode, refs = [], context = {}) {
  const parsed = (Array.isArray(refs) ? refs : [])
    .map(parseEvidenceRef)
    .filter(Boolean);
  if (!parsed.length) return [];
  const preferred = GAP_PREFERENCE[text(gapCode)] || [];
  const ordered = [];
  for (const kind of preferred) {
    ordered.push(...parsed.filter((item) => item.kind === kind));
  }
  ordered.push(...parsed.filter((item) => !preferred.includes(item.kind)));
  const seen = new Set();
  return ordered
    .filter((item) => {
      if (seen.has(item.ref) || item.kind === "step") return false;
      seen.add(item.ref);
      return true;
    })
    .slice(0, 3)
    .map((item) => evidenceAction(item, context));
}

export function evidenceExpected(gapCode) {
  return Object.prototype.hasOwnProperty.call(GAP_PREFERENCE, text(gapCode));
}

export { GAP_PREFERENCE, LABEL_BY_KIND, TAB_BY_KIND };
