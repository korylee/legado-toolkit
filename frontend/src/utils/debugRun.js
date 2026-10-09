// 在途运行的渲染层：**后端只给相位码与事实**（观测帧里的
// phase / elapsed_ms / lane_holder，见 `backend/api/job_timeline`），中文句子在这里出
// ——与 `debugDecision` 同一条约定：码在一处、词在一处，别在后端拼句子。
//
// **批量校验与调试共用这一份**：两边问的是同一件事（还没开始 / 谁占着引擎 / 等了多久）。
//
// 相位码与它们的含义：
//   queued   等引擎许可（`lane` 被别人占着时，真正的原因在 `lane_holder` 上）
//   starting 已拿到许可，工作已交给引擎线程
// 引擎里跑规则那一段**没有相位码**：到点接一句「引擎执行中」，照实转圈。
// **不编进度、不做 ETA**——那一段的耗时只由引擎自己知道。
//
// 调试侧现在还能看到**正在跑哪一步**（引擎逐条 flush 的事件进了事件账本，见
// `useDebugSession.liveEvents`），那是另一条线，不混进这一行状态词。

//: 相位码 → 状态词。空码（不在途）不取词。
export const PHASE_LABELS = {
  queued: "等待引擎空闲",
  starting: "正在拉起引擎",
};

//: 引擎空闲之后那一段的说法。**它不在相位码里**：引擎里跑规则那一段，后端的观测口
//: 读不到（ndjson 要等进程退出才解析），所以由界面在 starting 持续一段时间后接上
//: （门槛见下面 PHASE_HANDOFF_MS）。
export const RUNNING_LABEL = "引擎执行中";

//: starting → 引擎执行中 的交接时长（毫秒）。依据：拿到许可后写参数文件、落 manifest、
//: 交给启动器都是毫秒级；真正长的是引擎自己那一段。这个数**只是换词的门槛**，
//: 不是进度估计，所以宁可取小——长时间停在「正在拉起引擎」才是误导。
export const PHASE_HANDOFF_MS = 1500;

//: `lane_now`/`lane_holder` 是 lane 的**持有者类型**（调试档 "debug"、批量档 "batch"）。
//: 只有「别人占着」才值得说；自己占着（debug）不说。
const HOLDER_LABELS = { batch: "批量校验", debug: "另一次调试" };

/**
 * 把一次观测帧里的运行事实翻成运行条上那一行。
 *
 * @param {Object|null} snapshot 帧里的（phase / elapsed_ms / phase_ms / lane_holder / lane_waiting）
 * @param {number} [localMs] 本地秒表的毫秒数——帧还没到时用它兜底
 * @returns {{phase: string, text: string, elapsedSec: number, waiting: boolean}}
 */
export function debugRunState(snapshot, localMs = 0) {
  const snap = snapshot && typeof snapshot === "object" ? snapshot : {};
  const phase = String(snap.phase || "");
  const elapsedMs = Math.max(Number(snap.elapsed_ms) || 0, Number(localMs) || 0);
  const elapsedSec = Math.floor(elapsedMs / 1000);
  const waiting = phase === "queued";
  const holder = String(snap.lane_holder || "");

  if (phase === "queued") {
    const who = HOLDER_LABELS[holder] || "";
    const behind = Number(snap.lane_waiting) || 0;
    const text = who
      ? `${who}正在占用本机引擎，已等 ${elapsedSec} 秒`
      : `等待本机引擎，已等 ${elapsedSec} 秒`;
    return { phase, text: behind > 0 ? `${text}（前面还排着 ${behind} 个）` : text,
             elapsedSec, waiting };
  }
  if (phase === "starting") {
    // 交接判据用 **phase_ms**（当前相位持续了多久），不是总已等时长——
    // 前面可能排了很久的队，拿总时长判会让「拉起中」一出现就立刻跳过去
    if ((Number(snap.phase_ms) || 0) < PHASE_HANDOFF_MS) {
      return { phase, text: `正在拉起引擎，已等 ${elapsedSec} 秒`, elapsedSec, waiting: true };
    }
    return { phase: "running", text: `${RUNNING_LABEL}，已等 ${elapsedSec} 秒`,
             elapsedSec, waiting: false };
  }
  // 不在途：后端已经没有这次运行的登记（跑完了 / 服务重启过）。界面按本地秒表照实说，
  // 不假装仍在等，也不谎称失败
  return { phase: "", text: `已等待 ${elapsedSec} 秒`, elapsedSec, waiting: false };
}
