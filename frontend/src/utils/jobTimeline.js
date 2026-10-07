// 执行时间线的事件文案（jvm-batch-timeline）——**唯一的一份**。
//
// 后端给的是骨架事件（ts + kind + 字段），这里负责说成人话：主行只展示必要的执行方式
// 与失败原因，不编造后端没给的数字（比如预计剩余时间）。tone: info | warn | error，前端据此上色。

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
      return { text: at(` 开始（${ev.count ?? "?"} 条）`), tone: "info" };
    }
    case "resumed":
      return { text: at(" 上次已完成，本次不重跑"), tone: "info" };
    case "chunk_stalled":
      return { text: at(" 疑似卡在慢源（输出停滞），隔离重跑剩余源"), tone: "warn" };
    case "chunk_done": {
      const mode = ev.mode === "validate_daemon" ? "常驻引擎" : ev.mode === "gradle" ? "Gradle" : "";
      const suffix = mode ? ` · ${mode}` : "";
      return { text: at(` 完成：${ev.count ?? "?"} 条${ev.cost_sec != null ? " · " + sec(ev.cost_sec) : ""}${suffix}`), tone: "info" };
    }
    case "chunk_failed": {
      const mode = ev.mode === "validate_daemon" ? "常驻引擎" : ev.mode === "gradle" ? "Gradle" : "";
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

// 失败源一行：名字优先，理由收尾——「哪条源、为什么」一眼可读
export function formatFailureLine(item) {
  if (!item) return { title: "", detail: "" };
  const who = item.name || item.url || "（无名）";
  const state = item.state && item.state !== "ok" ? `（${item.state}）` : "";
  return { title: who, detail: `${state}${item.reason || ""}`.trim() };
}
