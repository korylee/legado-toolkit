// htmlView 的断言（纯函数，不碰 DOM；node --test 直接跑）。
//
// 为什么要这些：这块东西只在抽屉里显示，坏了没人报错——只会「看起来就是这样」。
// 它要动的是空白与实体，恰恰是用户接下来写规则/正则的依据，所以正反两面都钉住：
// 该折行的折行、该保留的一个字都不许少。
import assert from "node:assert/strict";
import test from "node:test";

import { decodeEntities, formatHtml } from "./htmlView.js";

test("具名与数字实体都展开", () => {
  assert.equal(decodeEntities("a&nbsp;b"), "a\u00a0b");
  assert.equal(decodeEntities("a&#160;b"), "a\u00a0b");
  assert.equal(decodeEntities("a&#xA0;b"), "a\u00a0b");
  assert.equal(decodeEntities("&amp;&lt;&gt;&quot;&apos;"), "&<>\"'");
  assert.equal(decodeEntities("&hellip;&mdash;&ldquo;中&rdquo;"), "…—“中”");
});

test("认不出的实体原样留着（别猜）", () => {
  assert.equal(decodeEntities("&foo;&nbsp"), "&foo;&nbsp");
  assert.equal(decodeEntities("&#0;&#x110000;"), "&#0;&#x110000;");
});

test("嵌套按层缩进，空标签不占层", () => {
  const out = formatHtml("<div><p>甲</p><br><p>乙</p></div>");
  assert.equal(out, [
    "<div>",
    "  <p>甲</p>",
    "  <br>",
    "  <p>乙</p>",
    "</div>",
  ].join("\n"));
});

test("文字里的连续空白折成一个空格，标签之间的空白丢掉", () => {
  const out = formatHtml("<div>\n   <p>  甲   乙  </p>\n</div>");
  assert.equal(out, "<div>\n  <p>甲 乙</p>\n</div>");
});

test("属性里的 > 不结束标签", () => {
  // 只有文本的元素挤在一行，标签里的 `>` 不算结束
  assert.equal(formatHtml('<a title="a>b" href="/x">甲</a>'),
    '<a title="a>b" href="/x">甲</a>');
});

test("script / style 的内容原样留着，也不做实体展开", () => {
  const src = '<div><script>if (a &amp;&amp; b) { x < 1 }</script><style>a>b{}</style></div>';
  const out = formatHtml(src);
  assert.ok(out.includes("if (a &amp;&amp; b) { x < 1 }"), out);
  assert.ok(out.includes("a>b{}"), out);
});

test("pre 的内容整段留着（空白有意义）", () => {
  const out = formatHtml("<div><pre>  甲\n    乙</pre></div>");
  assert.ok(out.includes("  甲\n    乙"), out);
});

test("注释与 DOCTYPE 各占一行，不进层级", () => {
  const out = formatHtml("<!DOCTYPE html><!-- a > b --><p>甲</p>");
  assert.equal(out, "<!DOCTYPE html>\n<!-- a > b -->\n<p>甲</p>");
});

test("不是标签的 < 当文本（别把 `a < b` 当标签）", () => {
  const out = formatHtml("<p>1 < 2 > 0</p>");
  assert.equal(out, "<p>\n  1 < 2 > 0\n</p>");
});

test("decodeEntities: false 时只折行不展开", () => {
  assert.ok(formatHtml("<p>a&nbsp;b</p>", { decodeEntities: false }).includes("a&nbsp;b"));
});

test("纯文本（没有标签）原样返回", () => {
  assert.equal(formatHtml("  甲&nbsp;乙  "), "甲\u00a0乙");
  assert.equal(formatHtml(""), "");
});

test("不重排标签本身：大小写、单引号、属性顺序照旧", () => {
  const out = formatHtml("<DIV CLASS='x' id=y><SPAN>甲</SPAN></DIV>");
  assert.equal(out, "<DIV CLASS='x' id=y>\n  <SPAN>甲</SPAN>\n</DIV>");
});
