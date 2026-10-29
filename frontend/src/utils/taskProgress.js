// 任务进度卡：四段的**状态判定**（唯一一份）。
//
// 抽成纯函数是为了钉住一个真踩过的坑：**跑完后最后一段永远显示「等待」**。
// 原因是任务行的 `progress` 语义是「正在跑第几段」（0 基），跑完停在下标 3 而不会
// 变成 4——拿它判最后一段就恒为「等待」（实测 2026-10-28：正文已通过却显示等待）。
//
// 判据：**有结果体的段结论就用它**（引擎验收的事实），没有才按 `progress` 推
// 「已过 / 进行中」。跑中和跑完两种数据源分开，不要混。

//: 每段状态的一句话（后端只给状态码，取词在这侧）
export const STAGE_STATE_TEXT = { ok: "通过", fail: "没过", running: "进行中", idle: "等待" };

/**
 * 四段各自的显示状态。
 *
 * @param {string[]} names       段名清单（后端 `STAGES` 的顺序）
 * @param {object}   view        进度事实
 * @param {number}   view.progress    正在跑第几段（0 基；**跑完不会等于段数**）
 * @param {boolean}  view.running     还在跑吗
 * @param {boolean}  view.preparing   在准备引擎（四段之外的相位，段卡片应全为等待）
 * @param {Array}    view.finalStages 结果体的 `stages`（`[{stage, ok, reason}]`）
 * @returns {Array<{key, idx, state, text}>}
 */
export function stageStates(names, view = {}) {
  const list = Array.isArray(names) ? names : [];
  const finals = Array.isArray(view.finalStages) ? view.finalStages : [];
  const progress = Number(view.progress || 0);
  const running = !!view.running;
  const preparing = !!view.preparing;
  return list.map((name, i) => {
    const fin = finals.find((s) => s && s.stage === name);
    let state = "idle";
    if (fin) state = fin.ok ? "ok" : "fail";
    else if (running && !preparing) {
      if (i < progress) state = "ok";
      else if (i === progress) state = "running";
    }
    return { key: name, idx: i + 1, state, text: STAGE_STATE_TEXT[state] || "" };
  });
}
