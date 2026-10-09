// 执行时间线的事件文案（jvm-batch-timeline）——**唯一的一份**。
//
// 后端给的是骨架事件（ts + kind + 字段），这里负责说成人话：主行只展示必要的执行方式
// 与失败原因，不编造后端没给的数字（比如预计剩余时间）。tone: info | warn | error，前端据此上色。

// 「这一块走的是哪条引擎」的取词（**唯一一份**）：块开始/结束/失败三处都说它。
// 各写一遍时，分歧正好落在不报错的地方——同一块开头说 Gradle、结尾说常驻引擎。
// 值域由后端给（`backend/jobs/jvm_exec.py` 的 execution_mode：validate_daemon / gradle）。
function modeLabel(mode) {
  if (mode === "validate_daemon") return "常驻引擎";
  if (mode === "gradle") return "Gradle";
  return "";
}

export function formatTimelineEvent(ev) {
  if (!ev || !ev.kind) return { text: "", tone: "info" };
  const n = ev.index != null ? Number(ev.index) + 1 : null;
  const at = (suffix) => (n != null ? `第 ${n} 块` + suffix : suffix);
  const sec = (v) => (v != null ? `${v} 秒` : "");
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
