// layers 的断言：**源声明那半 + 合并**（页面那半的判据在 Python，见 tests/test_page_layer.py）。
//
// 这里只钉两件事：① 源自己声明的能力按**段**归属（`webJs` 只说正文那段、`webView` 只算它挂的
// 那条 URL 规则）；② 页面结论（后端给的）与声明结论**合一次**的顺序，以及每条结论都带证据。
import assert from "node:assert/strict";
import test from "node:test";

import { classifyLayer, declaredEvidence, declaredLayer } from "./layers.js";

const PAGE_L1 = { layer: "L1", evidence: [], stats: { links: 3 }, has_wanted: true };
const PAGE_L3 = { layer: "L3", evidence: [{ why: "长 base64 赋值", snippet: "var p='…'", line: 3 }],
                  stats: { links: 0 }, has_wanted: false };

//: 本仓「口袋漫画」的源声明（实测切片）：webJs 读页面全局 `params`、正文规则是 `@js:`
const KOUDAI = {
  ruleContent: {
    content: "@js:var a = String(result).split(String.fromCharCode(10)), o = [];",
    webJs: "result = (params && typeof params === 'object' && params.chapter_images) ? '' : '';",
  },
};

test("webJs 读页面全局对象 → 这一段的声明层是 L3", () => {
  assert.equal(declaredLayer(KOUDAI, "content"), "L3");
  const ev = declaredEvidence(KOUDAI, "content");
  assert.ok(ev.some((e) => e.why === "webJs 读的是页面全局对象"), JSON.stringify(ev));
});

test("webJs 只要求渲染（不读全局）→ L2", () => {
  const src = { ruleContent: { webJs: "result = document.querySelector('.x').outerHTML;" } };
  assert.equal(declaredLayer(src, "content"), "L2");
});

test("源声明按段归属：正文的 webJs 不算到搜索页头上", () => {
  // 不按段归属的话，口袋漫画的搜索页（结果明明在原文里）会被判成 L2/L3，
  // 而界面上那是「候选面板被藏掉」——用户只会觉得工具坏了
  assert.equal(declaredLayer(KOUDAI, "search"), "");
  assert.equal(classifyLayer(PAGE_L1, KOUDAI, { step: "search" }).layer, "L1");
});

test("URL 上的 webView 只算它所属的那一段", () => {
  const src = { ruleToc: { chapterUrl: 'a@href,{"webView":true}' } };
  assert.equal(declaredLayer(src, "toc"), "L2");
  assert.equal(declaredLayer(src, "search"), "");
});

test("合并：页面结论与声明结论按动作优先级合一次", () => {
  // 页面说 L1，但源声明要 webJs 读全局 → L3（数据在全局对象里，选择器取不到）
  assert.equal(classifyLayer(PAGE_L1, KOUDAI, { step: "content" }).layer, "L3");
  // 页面的加密痕迹压过一切（除了登录墙）
  assert.equal(classifyLayer(PAGE_L3, {}, { step: "content" }).layer, "L3");
  // 页面 L1 + 声明 webView → L2
  assert.equal(classifyLayer(PAGE_L1, { searchUrl: 'u,{"webView":true}' }, { step: "search" }).layer,
    "L2");
  // 两边都没有：L1
  assert.equal(classifyLayer(PAGE_L1, {}, { step: "search" }).layer, "L1");
});

test("登录墙要「页面有词 + 源声明了登录方式」两半齐", () => {
  const page = { layer: "", evidence: [], stats: {}, login_marker: "请登录" };
  assert.equal(classifyLayer(page, { loginUrl: "https://a.com/login" }, {}).layer, "L5");
  // 源没声明 → 不判 L5（光看词会把「请登录后评论」判成墙）
  assert.notEqual(classifyLayer(page, {}, {}).layer, "L5");
});

test("判不了时把原因带出来，不硬猜", () => {
  const r = classifyLayer({ layer: "", evidence: [], stats: {}, unsure: "没给「这一步要什么」，判不了 L1" }, {}, {});
  assert.equal(r.layer, "");
  assert.ok(r.unsure, "要说清为什么判不了");
  assert.equal(r.info, null);
});

test("证据两边拼起来；层名与动作来自同一张表", () => {
  const r = classifyLayer(PAGE_L3, KOUDAI, { step: "content" });
  assert.ok(r.evidence.length >= 3, JSON.stringify(r.evidence.map((e) => e.why)));
  assert.equal(r.info.name, "L3 加密负载");
  assert.ok(r.info.action);
});

test("页面统计与「有没有目标」直接透传后端结论", () => {
  const r = classifyLayer(PAGE_L3, {}, { step: "content" });
  assert.equal(r.page.stats.links, 0);
  assert.equal(r.page.hasWanted, false);
});
