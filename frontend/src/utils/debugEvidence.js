// 把调试材料的来源和可信边界整理成一份可直接展示的摘要。
// 这里只做事实归类，不重新判断 verdict；结论仍以真实引擎为准。
import { nextDebugAction } from "./debugNextAction.js";

export function buildEvidenceSummary({
  channel = "",
  step = {},
  page = null,
  replay = null,
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
  if (replay) sources.push({ key: "replay", label: "本地规则投影", trust: "projection", type: "warning" });

  const boundaries = [];
  if (!isEngine) boundaries.push("没有真实引擎结果，不能据此验收规则");
  if (replay && replay.rule_error) boundaries.push("本地投影不支持这条规则：" + replay.rule_error);
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
    replay: !!replay,
    quality: quality || null,
  };
}

export function buildStepSummary({
  channel = "",
  step = {},
  page = null,
  replay = null,
  layer = null,
  quality = null,
  stale = false,
  action = null,
  inWorkbench = true,
} = {}) {
  const evidence = buildEvidenceSummary({ channel, step, page, replay, layer, quality });
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
