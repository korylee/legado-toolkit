// 运行级 WebView 能力事实的断言：怎么聚合、怎么归因、标题怎么按阶段分。
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  unsupportedForStep, webviewUnsupportedTitle, webviewUnsupportedView,
} from "./webviewCapability.js";

test("debug 与 matched 两个阶段都保留", () => {
  const view = webviewUnsupportedView({
    source: "jvm",
    webview_unsupported: [
      { code: "unsupported_is_rule", url: "https://a.test", phase: "debug" },
      { code: "unsupported_html_only", url: "", phase: "matched" },
    ],
  });
  assert.equal(view.length, 2);
  assert.equal(view[0].text, "本机调试暂不支持 isRule 注入路径");
  assert.equal(view[1].phase, "matched");
  assert.deepEqual(view[1].urls, []);
});

test("同一条边界撞多次只出一条，url 去重、空 url 报次数", () => {
  const view = webviewUnsupportedView({
    source: "jvm",
    webview_unsupported: [
      { code: "unsupported_html_only", url: "", phase: "debug" },
      { code: "unsupported_html_only", url: "", phase: "debug" },
      { code: "unsupported_is_rule", url: "https://a.test", phase: "debug" },
      { code: "unsupported_is_rule", url: "https://a.test", phase: "debug" },
      { code: "unsupported_is_rule", url: "https://b.test", phase: "debug" },
    ],
  });
  assert.equal(view.length, 2, "同码同阶段必须聚合成一条");
  const htmlOnly = view.find((item) => item.code === "unsupported_html_only");
  assert.equal(htmlOnly.count, 2);
  assert.deepEqual(htmlOnly.urls, []);
  assert.equal(htmlOnly.where, "2 处");
  const isRule = view.find((item) => item.code === "unsupported_is_rule");
  assert.equal(isRule.count, 3);
  assert.deepEqual(isRule.urls, ["https://a.test", "https://b.test"], "地址要去重");
  assert.equal(isRule.where, "https://a.test、https://b.test");
});

test("能力缺口不是 App 结果，也不接受旧布尔形状", () => {
  assert.equal(webviewUnsupportedView({
    source: "app",
    webview_unsupported: [{ code: "unsupported_is_rule", url: "x", phase: "debug" }],
  }), null);
  assert.equal(webviewUnsupportedView({ source: "jvm", webview_unsupported: true }), null);
  assert.equal(webviewUnsupportedView({
    source: "jvm",
    webview_unsupported: [{ code: "unknown", url: "x", phase: "debug" }],
  })[0].text, "本机调试有一项 WebView 能力未覆盖（unknown）",
  "码表对不上时必须露出原始码，否则谁都查不下去");
});

test("只有能归因到这一步的缺口才算这一步的事实", () => {
  const result = {
    source: "jvm",
    webview_unsupported: [
      { code: "unsupported_is_rule", url: "https://a.test", phase: "debug" },
      { code: "unsupported_html_only", url: "", phase: "matched" },
    ],
  };
  assert.equal(unsupportedForStep(result, "https://a.test"), true);
  assert.equal(unsupportedForStep(result, "https://other.test"), false, "别的地址不算");
  assert.equal(unsupportedForStep(result, ""), false, "归因不了就不说（读不到 ≠ 有）");
  assert.equal(unsupportedForStep(result, "https://a.test#x"), false, "地址要逐字相同");
});

test("标题按阶段分：主链撞边界比命中回填严重", () => {
  assert.equal(webviewUnsupportedTitle([{ phase: "debug" }]), "调试主链有未覆盖的 WebView 能力");
  assert.equal(webviewUnsupportedTitle([{ phase: "matched" }]), "命中回填有未覆盖的 WebView 能力");
  assert.equal(webviewUnsupportedTitle([{ phase: "matched" }, { phase: "debug" }]),
               "调试主链有未覆盖的 WebView 能力");
});
