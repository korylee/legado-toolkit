// 调试候选的展示口径：算法、AI 和点选候选共用。
// 这里只整理展示事实，不判断规则是否正确；最终结论仍来自本机真实引擎。

const INTENT_LABELS = {
  list: "列表项",
  link: "链接",
  text: "正文文本",
  media: "媒体地址",
};

const KIND_LABELS = {
  siblings: "同层集合",
  self: "当前元素",
  container: "父容器",
  path: "结构路径",
};

export function candidateIntentLabel(kind) {
  return INTENT_LABELS[String(kind || "")] || "目标值";
}

export function candidateKindLabel(kind) {
  return KIND_LABELS[String(kind || "")] || "候选规则";
}

export function candidateExamples(candidate = {}) {
  const samples = Array.isArray(candidate.samples) ? candidate.samples : [];
  return samples.filter((sample) => String(sample ?? "").trim()).slice(0, 3);
}

export function candidateView(candidate = {}, { intent = "", status = "" } = {}) {
  const examples = candidateExamples(candidate);
  const count = Number(candidate.count ?? candidate.hits ?? 0);
  const unique = Number(candidate.uniq ?? candidate.unique ?? count);
  const duplicate = Math.max(0, count - unique);
  return {
    intent: candidateIntentLabel(intent || candidate.intent),
    kind: candidateKindLabel(candidate.kind),
    rule: String(candidate.rule || candidate.legado || candidate.css || ""),
    examples,
    count: Number.isFinite(count) ? count : 0,
    unique: Number.isFinite(unique) ? unique : 0,
    duplicate,
    status: status || candidate.status || (candidate.verified ? "verified" : "pending"),
    reason: String(candidate.note || candidate.why || candidate.reason || ""),
  };
}
