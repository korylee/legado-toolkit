// layers 的断言（纯函数，不碰 DOM；node --test 直接跑）。
//
// 定层的价值全在「**证据行**」上——没有证据的结论用户没法反驳，也就没法信。所以
// 每条用例都连着证据一起断言：判出哪一层 + 至少有一条能指回去的痕迹。
import assert from "node:assert/strict";
import test from "node:test";

import { classifyLayer, hasWanted, pageStats } from "./layers.js";

//: 静态搜索页（形状取自实测的 samsbook 页面：书名在链接里、没有 JS/加密痕迹 → L1）。
//: **自带一份而不是读仓库里的 fixture**：这个文件要能单独跑（纯函数不依赖仓库布局）
const STATIC_PAGE = [
  "<html><head><title>绍宋-搜索结果</title></head><body>",
  "<div class='row'><dl><dt><a href='/book/0/282/'><img src='/i/282.jpg'></a></dt>",
  "<dd><h3><a href='/book/0/282/'>[历史]绍宋</a></h3></dd></dl></div>",
  "<nav><a href='/top/'>排行</a></nav></body></html>",
].join("");

//: 本仓「口袋漫画」的源声明（`data/candidates.json` 里那条的实测切片）：
//: `ruleContent.webJs` 非空、脚本读页面全局 `params`、正文规则是 `@js:`
const KOUDAI = {
  bookSourceName: "口袋漫画",
  bookSourceUrl: "https://www.koudaimh.com",
  ruleContent: {
    content: "@js:var a = String(result).split(String.fromCharCode(10)), o = [];",
    webJs: "result = (params && typeof params === 'object' && params.chapter_images) "
      + "? params.chapter_images.join(String.fromCharCode(10)) : '';",
  },
};

test("口袋漫画这类源判 L3，且证据 ≥3 条（判官是源自己声明的能力）", () => {
  const r = classifyLayer("<html><body><div id='chapter-images'></div></body></html>", KOUDAI);
  assert.equal(r.layer, "L3");
  assert.ok(r.evidence.length >= 3, "证据不足 3 条：" + JSON.stringify(r.evidence));
  // 证据要能指回源码：每条都有 why 与 snippet
  for (const e of r.evidence) {
    assert.ok(e.why && e.snippet, JSON.stringify(e));
  }
  // L2 与 L3 的分界就在这一条上：L2 只要求渲染，L3 得读页面 JS 建出来的对象
  assert.ok(r.evidence.some((e) => e.why === "webJs 读的是页面全局对象"),
    "少了「读全局对象」这条证据，L3 与 L2 就分不开了：" + JSON.stringify(r.evidence.map((e) => e.why)));
});

test("静态页判 L1，不误报", () => {
  const r = classifyLayer(STATIC_PAGE, {}, { want: { kind: "list" } });
  assert.equal(r.layer, "L1");
  assert.equal(r.page.hasWanted, true);
});

test("没有目标定义时不判 L1（那是硬猜）", () => {
  const r = classifyLayer(STATIC_PAGE, {});
  assert.equal(r.layer, "");
  assert.ok(r.unsure, "要说清为什么判不了");
});

test("页面自带加密痕迹 → L3，且证据是**页面上的行**", () => {
  const payload = "A".repeat(300);
  const page = "<html><body><script>var p = '" + payload + "';</script>"
    + "<div id='list'></div></body></html>";
  const r = classifyLayer(page, {});
  assert.equal(r.layer, "L3");
  assert.ok(r.evidence.some((e) => e.why === "长 base64 赋值"));
  assert.ok(r.evidence.some((e) => e.line >= 1), "页面证据要带行号");
});

test("xhr_mode / createObjectURL 也算 L3 的痕迹", () => {
  for (const mark of ["xhr_mode", "createObjectURL"]) {
    const r = classifyLayer("<html><body><script>var o = { " + mark + ": 1 };</script></body></html>", {});
    assert.equal(r.layer, "L3", mark);
  }
});

test("有接口痕迹且原文里没有目标 → L4", () => {
  const page = "<html><body><div id='app'></div>"
    + "<script>fetch('/api/books').then(r => r.json())</script></body></html>";
  const r = classifyLayer(page, {}, { want: { kind: "list" } });
  assert.equal(r.layer, "L4");
  assert.ok(r.evidence.some((e) => e.why === "fetch(" || e.why === "/api/"));
});

test("源声明了登录方式 + 页面出现登录词 → L5；没声明就不算", () => {
  const page = "<html><body>请登录后继续</body></html>";
  const declared = classifyLayer(page, { loginUrl: "https://a.com/login" });
  assert.equal(declared.layer, "L5");
  // 同一个页面，源没声明登录方式 → 不判 L5（光看词会把「请登录后评论」判成墙）
  const undeclared = classifyLayer(page, {});
  assert.notEqual(undeclared.layer, "L5");
});

test("webJs 只要求渲染（不读全局）→ L2", () => {
  const src = { ruleContent: { webJs: "result = document.querySelector('.x').outerHTML;" } };
  const r = classifyLayer("<html><body><div class='x'></div></body></html>", src);
  assert.equal(r.layer, "L2");
  assert.ok(r.evidence.some((e) => e.why === "源声明了 webJs"));
});

test("URL 规则带 webView → L2，且与 Kotlin 那条判据同一个词", () => {
  const src = { searchUrl: 'https://a.com/s?q={{key}},{"webView":true}' };
  const r = classifyLayer("<html><body><p>x</p></body></html>", src);
  assert.equal(r.layer, "L2");
  assert.ok(r.evidence.some((e) => e.why === "URL 规则带 webView 标记"));
});

test("pageStats / hasWanted 的门槛", () => {
  const st = pageStats("<a href='/1'>x</a><img src='a.jpg'><img src=''>");
  assert.equal(st.links, 1);
  assert.equal(st.images, 2);
  assert.equal(st.imagesWithSrc, 1, "src 为空的占位不算");
  assert.equal(hasWanted(st, { kind: "media" }), false, "只有一张图 → 不算有正文图");
  assert.equal(hasWanted(st, { kind: "link" }), true);
  assert.equal(pageStats(""), null);
});