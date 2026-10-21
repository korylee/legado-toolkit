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

//: 分页总页数：正文取数收尾时上游打的两行之一（单条链接→串行跟随 / 多条→并发抓取）
const PAGES_RE = /^(?:◇本章总页数|◇并发解析正文,总页数)[:：](\d+)$/;

/**
 * 正文的事实（只有 content 步有）：几个字、几页、首段长什么样、分页链接有几条。
 *
 * **以事件流为准**：上游 `contentDebug` 走 `needSave=false`，正文全文就打在
 * `┌获取正文内容` 的下一行；`structured.content` 是引擎另算的一份样本（Kotlin 侧
 * 有上限）。两份不一致时以事件为准，并把「样本只到 N 字符」带出来——样本不许被
 * 读成全文（同上一条注释的纪律：截断要自报）。
 */
function contentFacts(values, structured) {
  const observed = observedField(values, "正文内容");
  const sample = text(structured && structured.content);
  const fullText = observed !== null && observed ? observed : sample;
  let pages = null;
  for (const line of values) {
    const hit = text(line).match(PAGES_RE);
    if (hit) {
      pages = Number(hit[1]);
      break;
    }
  }
  const links = text(observedField(values, "正文下一页链接"));
  return {
    hasText: !!fullText,
    chars: fullText.length,
    pages,
    firstLine: (fullText.split("\n").map((line) => line.trim()).find(Boolean) || "")
      .slice(0, 120),
    fullText,
    sampleChars: sample.length,
    nextLinks: links ? links.split(/[，,\s]+/).filter(Boolean) : [],
  };
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

// 结构化结果的字段映射（Kotlin StructuredResult 的 items/详情字段名）。
// 有 structured 时优先用它——那是引擎按同一套解析器算的，字段集固定、搜索段是
// 完整书目；没有（连 App 通道 / 引擎没回填）才回落事件行解析。
// 列表项瘦身后（Kotlin 侧只留定位字段）没有 intro/cover_url，这里也删——
// 消费的字段集与生产侧对齐，别造「引擎没给的字段」。
const STRUCTURED_FIELDS = {
  search: [
    { key: "name", label: "书名" },
    { key: "author", label: "作者" },
    { key: "kind", label: "分类" },
    { key: "word_count", label: "字数" },
    { key: "latest_chapter", label: "最新章节" },
    { key: "book_url", label: "详情页", url: true },
  ],
  explore: [
    { key: "name", label: "书名" },
    { key: "author", label: "作者" },
    { key: "kind", label: "分类" },
    { key: "book_url", label: "详情页", url: true },
  ],
  bookUrl: [
    { key: "name", label: "书名" },
    { key: "author", label: "作者" },
    { key: "kind", label: "分类" },
    { key: "word_count", label: "字数" },
    { key: "last_chapter", label: "最新章节" },
    { key: "intro", label: "简介" },
    { key: "cover_url", label: "封面", url: true },
    { key: "toc_url", label: "目录链接", url: true },
  ],
};

/** 结构化条目 → 摘要字段。值为空/缺失的条目照旧省略，不造「未取到」。 */
function structuredFields(name, structured) {
  if (name === "toc" && Array.isArray(structured.chapter_list)) {
    const first = structured.chapter_list[0];
    return first ? [{ label: "章节样本", value: String(first) }] : [];
  }
  if (name === "content") {
    const value = text(structured.content);
    return value ? [{ label: "正文样本", value }] : [];
  }
  const defs = STRUCTURED_FIELDS[name];
  if (!defs) return [];
  const item = Array.isArray(structured.items) ? structured.items[0] : structured;
  if (!item) return [];
  return defs.map(({ key, label, url }) => {
    const value = text(item[key]);
    if (!value) return null; // 引擎取到的就是空，与事件里单独一行 └ 同义
    return url === true ? { label, value, url } : { label, value };
  }).filter(Boolean);
}

// 完整列表材料：structured.items（搜索/发现）与 chapter_list（目录）是引擎同一解析器
// 算出的完整条目（Kotlin LIST_LIMIT 上限 20），摘要字段只显示首条——全量在这里给，
// 手工编辑与「第二样本复核」看的是它。字段集同 STRUCTURED_FIELDS 的消费口径。
const LIST_COLUMNS = {
  search: [
    { key: "name", label: "书名" },
    { key: "author", label: "作者" },
    { key: "kind", label: "分类" },
    { key: "latest_chapter", label: "最新章节" },
    { key: "book_url", label: "详情页", url: true },
  ],
  explore: [
    { key: "name", label: "书名" },
    { key: "author", label: "作者" },
    { key: "kind", label: "分类" },
    { key: "book_url", label: "详情页", url: true },
  ],
};

/**
 * structured 里的完整列表 → `{columns, rows, total, truncated}`，没有可显示的列表
 * 返回 null（正文样本、详情字段已进摘要，不重复）。truncated = 引擎侧条数超过
 * 20 上限（total 是解析出的完整数，与摘要共用一个来源）。
 */
export function structuredList(step = {}) {
  const name = text(step.name);
  const structured = step.structured && typeof step.structured === "object"
    ? step.structured : null;
  if (!structured) return null;
  if (name === "toc" && Array.isArray(structured.chapter_list)
      && structured.chapter_list.length) {
    return {
      columns: [{ key: "name", label: "章节" }],
      rows: structured.chapter_list.map((item) => ({ name: text(item) })),
      total: Number.isFinite(structured.total) ? structured.total : null,
      truncated: structured.truncated === true,
    };
  }
  const columns = LIST_COLUMNS[name];
  if (!columns || !Array.isArray(structured.items) || !structured.items.length) {
    return null;
  }
  return {
    columns,
    rows: structured.items.map((item) =>
      Object.fromEntries(columns.map(({ key }) => [key, text(item[key])]))),
    total: Number.isFinite(structured.total) ? structured.total : null,
    truncated: structured.truncated === true,
  };
}

export function stepResult(step = {}) {
  const name = text(step.name);
  const values = Array.isArray(step.values) ? step.values : [];
  const structured = step.structured && typeof step.structured === "object"
    ? step.structured : null;
  // 条数：structured.total 是引擎解析出的完整结果数；没有才回落事件里的 ◇ 行
  const total = structured && Number.isFinite(structured.total)
    ? structured.total
    : (["search", "explore", "toc"].includes(name) ? observedCount(name, values) : null);
  const fields = structured
    ? structuredFields(name, structured)
    : (FIELDS[name] || []).map(({ event, label, url }) => {
      const value = observedField(values, event);
      if (value === null) return null; // 事件里没记
      return { label, value, url };
    }).filter(Boolean).map(({ label, value, url }) => url === true ? { label, value, url } : { label, value });
  return {
    name,
    total,
    totalLabel: name === "toc" ? "目录总数" : "书籍总数",
    fields,
    // 正文的产出是**一次提取的结果**（几个字、几页、首段），字段值只是输入面那一侧
    content: name === "content" ? contentFacts(values, structured) : null,
    reason: text(step.reason || (step.verdict === "fail" ? step.detail : "")),
    verdict: text(step.verdict) || "unknown",
  };
}
