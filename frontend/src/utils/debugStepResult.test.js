import assert from "node:assert/strict";
import test from "node:test";
import { stepResult } from "./debugStepResult.js";

test("search shows observed total and first-book fields, not event-line count or search URL", () => {
  const actual = stepResult({ name: "search", verdict: "pass", url: "https://site/search?q=x", values: [
    "⇒开始搜索关键字:x", "┌获取书籍列表", "└列表大小:20",
    "┌获取书名", "└甲书", "┌获取作者", "└乙",
    "┌获取字数", "└120万", "◇书籍总数:20", "︽搜索页解析完成",
  ] });
  assert.equal(actual.total, 20);
  assert.deepEqual(actual.fields, [
    { label: "书名", value: "甲书" },
    { label: "作者", value: "乙" },
    { label: "字数", value: "120万" },
  ]);
});

test("multi-line values concatenate and a lone └ is an explicit empty", () => {
  const actual = stepResult({ name: "content", values: [
    "┌获取正文内容", "└", "　　《甲》第一段", "第二段",
  ] });
  assert.deepEqual(actual.fields, [{ label: "正文样本", value: "《甲》第一段\n第二段" }]);
});

test("structured result wins over event parsing; full book list totals replace ◇ lines", () => {
  const actual = stepResult({ name: "search", verdict: "pass", values: [
    "◇书籍总数:20",
  ], structured: {
    total: 20, truncated: false, items: [
      { name: "甲书", author: "作者A", kind: "都市", word_count: "120万",
        latest_chapter: "第10章", book_url: "/book/1" },
    ],
  } });
  assert.equal(actual.total, 20);
  const labels = Object.fromEntries(actual.fields.map((f) => [f.label, f.value]));
  assert.equal(labels["书名"], "甲书");
  assert.deepEqual(actual.fields.filter((f) => f.url).map((f) => f.label),
    ["详情页"]);
});

test("no structured falls back to event-line parsing as before", () => {
  const actual = stepResult({ name: "search", verdict: "pass", url: "https://site/s", values: [
    "┌获取书名", "└甲书", "◇书籍总数:20",
  ] });
  assert.equal(actual.total, 20);
  assert.deepEqual(actual.fields.map((f) => f.label), ["书名"]);
});

test("missing observations are omitted entirely, with the real error retained", () => {
  const actual = stepResult({ name: "search", verdict: "fail", reason: "搜索请求失败", values: ["⇒开始搜索"] });
  assert.equal(actual.total, null);
  assert.deepEqual(actual.fields, []);
  assert.equal(actual.reason, "搜索请求失败");
});

test("content facts come from the event stream: chars, first line, pages, next-page links", () => {
  const actual = stepResult({
    name: "content",
    values: [
      "┌获取章节名称", "└第 1 章",
      "┌获取正文内容", "└", "　　第一段正文", "第二段正文",
      "┌获取正文下一页链接", "└https://s/2.html，https://s/3.html",
      "◇本章总页数:3", "︽正文页解析完成",
    ],
  });
  assert.equal(actual.content.hasText, true);
  assert.equal(actual.content.chars, "第一段正文\n第二段正文".length);
  assert.equal(actual.content.firstLine, "第一段正文");
  assert.equal(actual.content.pages, 3);
  assert.deepEqual(actual.content.nextLinks,
    ["https://s/2.html", "https://s/3.html"]);
  // 其它步没有这份事实：正文的产出不冒充别步的
  assert.equal(stepResult({ name: "toc", values: ["◇目录总数:3"] }).content, null);
});

test("content prefers the event text over the engine sample, and says when it is a sample", () => {
  const sample = "甲".repeat(20);
  const fromEvents = stepResult({
    name: "content", values: ["┌获取正文内容", "└", "甲".repeat(30)],
    structured: { content: sample },
  });
  assert.equal(fromEvents.content.chars, 30);
  assert.equal(fromEvents.content.sampleChars, 20);
  assert.ok(fromEvents.content.sampleChars < fromEvents.content.chars);

  // 事件里没记正文（连 App 之外的情形）才回落样本：它**就是**样本，不许被读成全文
  const onlySample = stepResult({
    name: "content", values: ["︽正文页解析完成"], structured: { content: sample },
  });
  assert.equal(onlySample.content.hasText, true);
  assert.equal(onlySample.content.chars, 20);
  assert.equal(onlySample.content.pages, null);
});

test("detail step exposes the fields BookInfo records; toc samples come from ◇ lines", () => {
  const detail = stepResult({ name: "bookUrl", url: "https://site/book/1", values: [
    "┌获取书名", "└甲", "┌获取简介", "└\n多行简介", "┌获取封面链接", "└/img/a.jpg",
    "┌获取目录链接", "└/book/1",
  ] });
  assert.deepEqual(detail.fields, [
    { label: "书名", value: "甲" },
    { label: "简介", value: "多行简介" },
    { label: "封面", value: "/img/a.jpg", url: true },
    { label: "目录链接", value: "/book/1", url: true },
  ]);
  const toc = stepResult({ name: "toc", values: ["◇目录总数:108", "◇章节名称:第1页"] });
  assert.equal(toc.total, 108);
  assert.equal(toc.fields.length, 0);
});
