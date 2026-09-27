// debugKeys 的断言：形态错 = App 静默无响应（最难排查的那类失败）。
// 幂等去前缀、发现页回落 exploreUrl、留空回落搜索、重跑 key 保原文。

import assert from "node:assert/strict";
import test from "node:test";

import { buildKey, debugKeyOf, rerunKey } from "./debugKeys.js";

test("五类目标的 key 形态", () => {
  assert.equal(buildKey("search", "我", ""), "我");
  assert.equal(buildKey("explore", "", "http://m.site/sort/1/1.html"),
               "发现::http://m.site/sort/1/1.html");
  assert.equal(buildKey("info", "https://a.com/b/1", ""), "https://a.com/b/1");
  assert.equal(buildKey("toc", "/b/1/", ""), "++/b/1/");
  assert.equal(buildKey("content", "/b/1/c1.html", ""), "--/b/1/c1.html");
});

test("手抄的前缀幂等去重，只去一次", () => {
  assert.equal(buildKey("toc", "++/b/1/", ""), "++/b/1/");
  assert.equal(buildKey("content", "--/b/1/c1", ""), "--/b/1/c1");
  // 四个减号只剥一层：剥两层会把真想要的「--作为 URL 一部分」的形态改掉
  assert.equal(buildKey("content", "----/b/1/c1", ""), "----/b/1/c1");
});

test("留空回落：搜索用默认词，详情/目录/正文回落搜索入口", () => {
  assert.equal(debugKeyOf("search", "", ""), "我");
  assert.equal(debugKeyOf("toc", "", ""), "我");
  assert.equal(debugKeyOf("content", "", ""), "我");
  assert.equal(debugKeyOf("info", "", ""), "我");
  assert.equal(debugKeyOf("explore", "", ""), "", "发现没配置就是空——调用方据此挡住");
});

test("rerunKey 用 URL 原文（含请求选项），幂等去已有前缀", () => {
  assert.equal(rerunKey("content", "/b/1/c1.html,{\"webView\":true}"),
               "--/b/1/c1.html,{\"webView\":true}");
  assert.equal(rerunKey("toc", "++/b/1/"), "++/b/1/");
  assert.equal(rerunKey("bookUrl", "https://a.com/b/1"), "https://a.com/b/1");
  assert.equal(rerunKey("explore", "http://m.site/sort/1/1.html"),
               "发现::http://m.site/sort/1/1.html");
});

test("rerunKey 搜索段返回 null（走整链入口），URL 为空返回空串", () => {
  assert.equal(rerunKey("search", "https://a.com/x"), null);
  assert.equal(rerunKey("content", ""), "");
  assert.equal(rerunKey("toc", "  "), "");
});
