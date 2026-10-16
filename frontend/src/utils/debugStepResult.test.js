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

test("missing observations are omitted entirely, with the real error retained", () => {
  const actual = stepResult({ name: "search", verdict: "fail", reason: "搜索请求失败", values: ["⇒开始搜索"] });
  assert.equal(actual.total, null);
  assert.deepEqual(actual.fields, []);
  assert.equal(actual.reason, "搜索请求失败");
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
