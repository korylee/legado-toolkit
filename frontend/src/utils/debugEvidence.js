// 把调试材料的来源和可信边界整理成一份可直接展示的摘要。
// 这里只做事实归类，不重新判断 verdict；结论仍以真实引擎为准。
import { nextDebugAction } from "./debugNextAction.js";

//: 样本取值的硬上限：一次调试的 DOM 可能很大，而它只用来「认得出形似的东西」
const SAMPLE_LIMIT = 40;
//: `input` / `title` / `href` 这些是取值动作取的东西，属性本身不是值
const _ATTR_VALUE_MAX = 200;

/**
 * 从**引擎带回的命中 DOM**里取几个值——「这一步 App 实际拿到了什么」。
 *
 * 为什么需要它：引擎那一步的 `values` 是**事件流水**（`⇒开始搜索` / `◇书籍总数:50`
 * 这类结构行与统计行），拿它当基准比出来的「对上 N 条」是巧合——实测「去读书网」
 * 上候选 `.searchresult .sone` 撞上 24 条，而基线里根本没有书名。
 * `matched_html` 是引擎用 App 自己的解析器跑规则拿到的 DOM，它里面的值与「取到的东西」
 * 才是同一类。设备通道不带这段 DOM，那时只能退回事件流水（界面照旧如实说明）。
 *
 * 纯文本扫描而不是真解析：这里的用途只是「挑几个样本给免费初筛比对」，
 * 建一棵 DOM 树不值当（`<script>`/`<style>` 内容会一起进来，但那不会让比对更准或更差）。
 */
export function matchedSampleValues(matchedHtml, limit = SAMPLE_LIMIT) {
  const text = String(matchedHtml || "");
  if (!text) return [];
  const out = [];
  const push = (value) => {
    const v = String(value || "").replace(/\s+/g, " ").trim();
    if (!v || v.length > _ATTR_VALUE_MAX || out.includes(v)) return;
    out.push(v);
  };
  // 标签之间的文本
  for (const m of text.matchAll(/>([^<]+)</g)) push(m[1]);
  // 属性值（`@href` / `@title` 这类取值动作取到的就是它）
  for (const m of text.matchAll(/\b(?:href|title|src|data-original|data-src)\s*=\s*["']([^"']*)["']/gi)) {
    push(m[1]);
  }
  return out.slice(0, Math.max(1, limit));
}

export function buildEvidenceSummary({
  channel = "",
  step = {},
  page = null,
  layer = null,
  quality = null,
} = {}) {
  const isEngine = channel === "app" || channel === "jvm";
  const sources = [];
  if (channel === "app") sources.push({ key: "app", label: "App 实测", trust: "authoritative", type: "success" });
  else if (channel === "jvm") sources.push({ key: "jvm", label: "本机引擎", trust: "authoritative", type: "primary" });
  else sources.push({ key: "none", label: "无真实引擎结果", trust: "unavailable", type: "info" });

  if (page) {
    sources.push({
      key: "page",
      label: page.origin === "engine" ? "引擎取回页面" : (page.cached ? "缓存页面" : "本次抓取页面"),
      trust: page.origin === "engine" ? "supporting" : "supporting",
      type: page.origin === "engine" ? "success" : (page.cached ? "warning" : "info"),
    });
  }

  const boundaries = [];
  if (!isEngine) boundaries.push("没有真实引擎结果，不能据此验收规则");
  if (page && page.origin !== "engine") boundaries.push("页面来自另抓或缓存，可能没有 App 的 Cookie、UA 或 WebView 状态");
  if (layer && layer.layer && layer.layer !== "L1") boundaries.push("页面属于动态或受环境影响的层级，DOM 命中不等于 App 能取到数据");
  if (step.verdict === "unknown") boundaries.push("真实引擎暂时无法判定，需补充本机材料");
  if (step.verdict === "fail") boundaries.push("真实引擎已判定失败，应优先看取值内容和失败原因");

  return {
    verdict: step.verdict || "unknown",
    sources,
    boundaries,
    hasEngine: isEngine,
    page: !!page,
    quality: quality || null,
  };
}

export function buildStepSummary({
  channel = "",
  step = {},
  page = null,
  layer = null,
  quality = null,
  stale = false,
  action = null,
  inWorkbench = true,
} = {}) {
  const evidence = buildEvidenceSummary({ channel, step, page, layer, quality });
  const verdict = evidence.verdict;
  const verdictLabel = { pass: "通过", fail: "失败", unknown: "无法判定" }[verdict] || "无法判定";
  const reason = String(step.reason || step.detail || (verdict === "pass" ? "真实引擎已取到结果" : "暂无具体原因"));
  return {
    ...evidence,
    verdictLabel,
    reason,
    stale: !!stale,
    action: action || nextDebugAction(step, {
      channel, layer: (layer && layer.layer) || "", stale, inWorkbench,
    }),
  };
}
