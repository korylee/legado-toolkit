// 本步实测摘要：从**事件原文**里取有据可查的统计与字段值，给规则框下方的
// 「本步实测」区展示。事件行不是字段值列表——条数只认 `◇X总数` / `└列表大小`，
// 字段值只认 `┌获取X` 的紧邻 `└` 行（多行值拼接；单独一行 `└` 是引擎明确记下的空值）。
// 上游记账边界（改判据先看那边的 Debug.log）：搜索/发现段只有**首条书**记全字段
// （BookList.getSearchItem 的 log=index==0），详情段每字段都记（BookInfo），
// 目录段只有 `◇章节名称/章节链接` 一行样本（BookChapterList），
// 正文段有章节名与正文全文（BookContent）。取不到就不显示，不造「未取到」。

function text(value) {
  return String(value == null ? "" : value).trim();
}

/**
 * `┌获取X` 之后第一个 `└` 开头的行即值。多行值（`└\n正文全文`）继续吞到下一行
 * 直到下一个事件记号（┌/◇/≡/⇒/︾/︽）或结尾。单独一行 `└` = 空值，返回 ""。
 */
function observedField(values, event) {
  for (let i = 0; i < values.length; i += 1) {
    if (text(values[i]) !== `┌获取${event}`) continue;
    const first = text(values[i + 1] || "");
    if (!first.startsWith("└")) return "";
    let value = first.slice(1);
    let j = i + 2;
    for (; j < values.length; j += 1) {
      const line = text(values[j]);
      if (/^[┌◇≡⇒︾︽]/.test(line)) break;
      value += "\n" + line;
    }
    return value.trim();
  }
  return null; // null = 事件里根本没记这个字段
}

function observedCount(step, values) {
  const expected = step === "toc" ? /^(?:◇目录总数|└列表大小)[:：](\d+)$/
    : /^(?:◇书籍总数|└列表大小)[:：](\d+)$/;
  for (const line of values) {
    const matched = text(line).match(expected);
    if (matched) return Number(matched[1]);
  }
  return null;
}

// 字段表：event 是上游 `┌获取X` 的 X，url 为真时渲染成链接（值按 step.url 绝对化）。
const FIELDS = {
  search: [
    { event: "书名", label: "书名" },
    { event: "作者", label: "作者" },
    { event: "分类", label: "分类" },
    { event: "字数", label: "字数" },
    { event: "最新章节", label: "最新章节" },
    { event: "简介", label: "简介" },
    { event: "封面链接", label: "封面", url: true },
    { event: "详情页链接", label: "详情页", url: true },
  ],
  explore: [
    { event: "书名", label: "书名" },
    { event: "作者", label: "作者" },
    { event: "分类", label: "分类" },
    { event: "封面链接", label: "封面", url: true },
    { event: "详情页链接", label: "详情页", url: true },
  ],
  bookUrl: [
    { event: "书名", label: "书名" },
    { event: "作者", label: "作者" },
    { event: "分类", label: "分类" },
    { event: "字数", label: "字数" },
    { event: "最新章节", label: "最新章节" },
    { event: "简介", label: "简介" },
    { event: "封面链接", label: "封面", url: true },
    { event: "目录链接", label: "目录链接", url: true },
  ],
  toc: [
    { event: "章节名称", label: "章节样本" },
    { event: "章节链接", label: "章节链接", url: true },
  ],
  content: [
    { event: "章节名称", label: "章节" },
    { event: "正文内容", label: "正文样本" },
  ],
};

const URL_LABELS = { bookUrl: "目录链接" };

export function stepResult(step = {}) {
  const name = text(step.name);
  const values = Array.isArray(step.values) ? step.values : [];
  const fields = (FIELDS[name] || []).map(({ event, label, url }) => {
    const value = observedField(values, event);
    if (value === null) return null; // 事件里没记
    return { label, value, url };
  }).filter(Boolean).map(({ label, value, url }) => url === true ? { label, value, url } : { label, value });
  const total = ["search", "explore", "toc"].includes(name)
    ? observedCount(name, values) : null;
  return {
    name,
    total,
    totalLabel: name === "toc" ? "目录总数" : "书籍总数",
    fields,
    urlFieldLabels: (URL_LABELS[name] || "").split(",").filter(Boolean),
    reason: text(step.reason || (step.verdict === "fail" ? step.detail : "")),
    verdict: text(step.verdict) || "unknown",
  };
}
