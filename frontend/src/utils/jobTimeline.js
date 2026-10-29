import { STEP_LABELS } from "./steps.js";
// 执行时间线的事件文案（jvm-batch-timeline）——**唯一的一份**。
//
// 后端给的是骨架事件（ts + kind + 字段），这里负责说成人话：主行只展示必要的执行方式
// 与失败原因，不编造后端没给的数字（比如预计剩余时间）。tone: info | warn | error，前端据此上色。
//
// `ai_*` 不在这里取词：那一轮的字段名与四态是 AI 补全弹窗的读者，取词在
// `utils/agentRun`（唯一一份），这里只分派。
import { agentEventLine } from "./agentRun.js";

// 「这一块走的是哪条引擎」的取词（**唯一一份**）：块开始/结束/失败三处都说它。
// 各写一遍时，分歧正好落在不报错的地方——同一块开头说 Gradle、结尾说常驻引擎。
// 值域由后端给（`backend/jobs/jvm_exec.py` 的 execution_mode：daemon / gradle）。
function modeLabel(mode) {
  if (mode === "daemon") return "常驻引擎";
  if (mode === "gradle") return "Gradle";
  return "";
}

/** AI 补全这一轮的事件取词在 `utils/agentRun`（**唯一一份**，弹窗与这里共用）。
 *
 * 这里只做分派：`ai_*` 落 default 时任务中心会把 `ai_try` / `ai_family` 这些码
 * 原样打到界面上——那不是「技术细节」，是读者看不懂的四行字。 */
function aiLine(ev) {
  return agentEventLine(ev);
}

const RENDER_UPGRADE_KINDS = new Set([
  "ai_render_upgraded", "ai_render_unsettled", "ai_render_skipped",
  "source_webview_applied", "source_webview_rejected", "source_webview_skipped",
]);
function isRenderUpgrade(kind) {
  return RENDER_UPGRADE_KINDS.has(String(kind || ""));
}

/** 渲染升级那三条的取词（材料层动作，不是一轮补全的逐字段过程）。 */
function renderUpgradeLine(ev) {
  const sec = (v) => (v != null ? `${v} 秒` : "");
  switch (ev.kind) {
    case "ai_render_upgraded": {
      // 耗时是这次升级花的；`settled` 是「页面填数据填完了没」——这份材料能不能当
      // 内容看的关键事实（实测 25 张图里 22 张还是 loading 图的那一份）。
      const cost = ev.cost_sec != null ? `（${sec(ev.cost_sec)}）` : "";
      if (ev.settled === false) {
        return { text: `这一页要渲染，渲染后数据还在填${cost}——交回的可能是半成品`, tone: "warn" };
      }
      return { text: `这一页要渲染，已自动渲染${cost}`, tone: "info" };
    }
    case "source_webview_applied":
      return { text: `已自动给 ${ev.path || "URL 规则"} 加上 webView 选项（复核通过）`, tone: "info" };
    case "source_webview_rejected":
      return { text: `试过给 ${ev.path || "URL 规则"} 加 webView，复核没过，已退回：${ev.reason || "原因未记录"}`, tone: "warn" };
    case "source_webview_skipped":
      return { text: `没有自动改源：${ev.reason || "没有可挂的位置"}`, tone: "info" };
    case "ai_render_unsettled":
      return { text: String(ev.reason || "页面没稳定，材料可能是半成品"), tone: "warn" };
    default:
      return { text: `没法自动渲染：${ev.reason || "未知原因"}`, tone: "warn" };
  }
}

export function formatTimelineEvent(ev) {
  if (!ev || !ev.kind) return { text: "", tone: "info" };
  // AI 补全那一轮（agent_loop）：字段名与原因在 agentRun 取词，这里不落 default
  // 渲染升级那条**也**是 `ai_` 前缀，但它不是「一轮补全的逐字段过程」——它是任务在
  // 材料层做的一个动作，取词在本文件。先认这三个，别掉进 agentEventLine 的 default。
  if (isRenderUpgrade(ev.kind)) return renderUpgradeLine(ev);
  if (String(ev.kind).startsWith("ai_")) return aiLine(ev);
  const n = ev.index != null ? Number(ev.index) + 1 : null;
  const at = (suffix) => (n != null ? `第 ${n} 块` + suffix : suffix);
  const sec = (v) => (v != null ? `${v} 秒` : "");
  // 一键生成（generate_source）：四段流水线，每段是「调试 → AI 补全 → 验收」三个动作。
  // 段名取 `STEP_LABELS`（唯一那份），不在这里再抄一张。
  if (String(ev.kind).startsWith("stage_") || ev.kind === "generate_started"
      || ev.kind === "generate_done" || ev.kind === "stage_skipped") {
    const stage = ev.stage ? STEP_LABELS[ev.stage] || ev.stage : "";
    const head = stage ? stage + " · " : "";
    switch (ev.kind) {
      case "generate_started":
        return { text: `开始生成：${ev.keyword ? "关键词「" + ev.keyword + "」" : ev.url || ""}`, tone: "info" };
      case "stage_started":
        return { text: `${head}开始`, tone: "info" };
      case "stage_debug_started":
        return { text: `${head}调试页面`, tone: "info" };
      case "stage_debug_done":
        return { text: `${head}调试完成${ev.cost_sec != null ? "（" + sec(ev.cost_sec) + "）" : ""}`, tone: "info" };
      case "stage_ai_started":
        return { text: `${head}AI 补全 ${(ev.fields || []).length} 个字段`, tone: "info" };
      case "stage_ai_done":
        return { text: `${head}补全完成${ev.written != null ? "（写入 " + ev.written + " 条）" : ""}`, tone: "info" };
      case "stage_rules_applied":
        return { text: `${head}规则已写入，准备验收`, tone: "info" };
      case "stage_verified":
        return { text: `${head}通过`, tone: "info" };
      case "stage_failed":
        return { text: `${head}没过${ev.reason ? "：" + ev.reason : ""}`, tone: "error" };
      case "stage_retry":
        return { text: `${head}第 ${ev.round || 1} 轮重试${ev.reason ? "：" + ev.reason : ""}`, tone: "warn" };
      case "stage_skipped":
        return { text: `${head}跳过${ev.reason ? "：" + ev.reason : ""}`, tone: "warn" };
      case "generate_done":
        return { text: ev.passed ? "生成完成：四段全部通过" : `生成中止${ev.stop ? "：" + ev.stop : ""}`,
                 tone: ev.passed ? "info" : "error" };
      default:
        break;
    }
  }
  switch (ev.kind) {
    case "batch_started":
      return { text: `开始校验：${ev.sources ?? "?"} 条源，分 ${ev.chunks ?? "?"} 块`, tone: "info" };
    case "single_started":
      return { text: "开始校验（单条）", tone: "info" };
    case "startup_stage": {
      // 只列后端真会发的阶段（见 backend/jobs/jvm_exec.py 的 _append_event）；
      // 认不出的阶段原样透出，不伪装成已知项
      const labels = {
        queue_wait: "任务排队",
        daemon_request: "常驻引擎执行",
      };
      const label = labels[ev.stage] || ev.stage || "启动阶段";
      const status = ev.status === "failed" ? "失败" : "完成";
      const cost = ev.cost_sec != null ? `（${sec(ev.cost_sec)}）` : "";
      const reason = ev.reason ? `：${ev.reason}` : "";
      return { text: `${label}${status}${cost}${reason}`, tone: ev.status === "failed" ? "error" : "info" };
    }
    case "prepare": {
      if (ev.outcome === "ready") return { text: "校验引擎已就绪（热复用）", tone: "info" };
      if (ev.outcome === "started") return { text: `校验引擎已就绪（首次启动 ${sec(ev.cost_sec)}）`, tone: "info" };
      if (ev.outcome === "busy") return { text: `校验引擎忙，本批未准备${ev.reason ? "：" + ev.reason : ""}`, tone: "warn" };
      return { text: `校验引擎启动失败${ev.reason ? "：" + ev.reason : ""}`, tone: "error" };
    }
    case "engine_preparing":
      return { text: "准备校验引擎（冷启动约 40 秒，之后就绪）", tone: "info" };
    case "engine_prepared": {
      // 四档与 `jvm_daemon.prepare` 一致：ready 秒回、started 是这次真点火
      const cost = ev.cost_sec != null ? `（${sec(ev.cost_sec)}）` : "";
      if (ev.outcome === "ready") return { text: `校验引擎已就绪${cost}`, tone: "info" };
      if (ev.outcome === "started") return { text: `校验引擎已启动${cost}`, tone: "info" };
      if (ev.outcome === "busy") {
        return { text: `校验引擎忙，本次未预热${ev.reason ? "：" + ev.reason : ""}`, tone: "warn" };
      }
      return { text: `校验引擎准备失败${ev.reason ? "：" + ev.reason : ""}`, tone: "error" };
    }
    case "waiting_engine":
      return { text: `等待校验引擎空闲：已等 ${sec(ev.elapsed_sec) || "?"}`, tone: "warn" };
    case "recovered":
      return { text: "校验引擎恢复，后续块重新使用", tone: "info" };
    case "chunk_started": {
      // 引擎名在这里就报：块里慢下来的原因（走的是 Gradle 还是常驻引擎）跑的时候就要看得见，
      // 而不是等这一块结束——`mode` 是后端 chunk_started 自带的事实，不是前端推的
      const mode = modeLabel(ev.mode);
      return { text: at(` 开始（${ev.count ?? "?"} 条${mode ? " · " + mode : ""}）`), tone: "info" };
    }
    case "resumed":
      return { text: at(" 上次已完成，本次不重跑"), tone: "info" };
    case "chunk_stalled":
      return { text: at(" 疑似卡在慢源（输出停滞），隔离重跑剩余源"), tone: "warn" };
    case "chunk_done": {
      const mode = modeLabel(ev.mode);
      const suffix = mode ? ` · ${mode}` : "";
      return { text: at(` 完成：${ev.count ?? "?"} 条${ev.cost_sec != null ? " · " + sec(ev.cost_sec) : ""}${suffix}`), tone: "info" };
    }
    case "chunk_failed": {
      const mode = modeLabel(ev.mode);
      return { text: at(` 失败${mode ? "（" + mode + "）" : ""}${ev.reason ? "：" + ev.reason : ""}`), tone: "error" };
    }
    case "done":
      return { text: `校验完成：共 ${ev.count ?? "?"} 条${ev.cost_sec != null ? " · " + sec(ev.cost_sec) : ""}`, tone: "info" };
    case "failed":
      return { text: `校验中止${ev.reason ? "：" + ev.reason : ""}`, tone: "error" };
    case "cancelled":
      return { text: "已取消，剩余块未启动", tone: "warn" };
    default:
      return { text: String(ev.kind), tone: "info" };
  }
}

/** 把平铺的事件流读成「块」。批量校验天然是分块的（事件自带 index），平铺成
 *  流水账就浪费了这层结构——一屏几十行里看不出「第 6 块失败了」。
 *  返回扁平数组（渲染只用一个循环）：`depth` 0 是块首与顶层事件、1 是块内后续；
 *  `chunk` 标记该行属于某个块，样式据此画左侧竖线。
 *  同一 index 被顶层事件打断后再次出现，仍算同一块（否则一块会被拆成两段）。 */
export function groupTimelineLines(lines) {
  const out = [];
  const opened = new Set();
  for (const line of Array.isArray(lines) ? lines : []) {
    if (!line) continue;
    const idx = line.index != null ? Number(line.index) : null;
    if (idx == null) {
      out.push({ key: line.seq, line, depth: 0, chunk: false });
      continue;
    }
    const first = !opened.has(idx);
    opened.add(idx);
    out.push({ key: line.seq, line, depth: first ? 0 : 1, chunk: true });
  }
  return out;
}

// 失败源一行：名字优先，理由收尾——「哪条源、为什么」一眼可读
export function formatFailureLine(item) {
  if (!item) return { title: "", detail: "" };
  const who = item.name || item.url || "（无名）";
  const state = item.state && item.state !== "ok" ? `（${item.state}）` : "";
  return { title: who, detail: `${state}${item.reason || ""}`.trim() };
}
