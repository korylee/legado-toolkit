// 调试会话状态（ux-debug-session 第二期）：**每份调试状态只有一个写者**。
//
// 模块级单例（同 useMobile 先例，不引 pinia）：调试状态不再散在编辑弹框与
// 抽屉两个组件里靠 props/emit 对接——那正是「草稿/应用」两层与 rerunning
// 跨组件传递的根源。组件只消费这里的 refs 与动作；写者只有本文件。
//
// `startRun` 是两条通道（本机引擎 / 连 App）共用的唯一入口：秒表、AbortController、
// 预检、推送确认、结果落位、对比基线滚动都在里面。界面反应（开抽屉、摊开失败
// 步骤）留给调用方——那是「这次会话」的事，不是「这次运行」的事。
//
// `AbortController` 只断**前端的等**：后端 lane 那一次仍会跑完（lane/锁的设计），
// 所以按钮叫「取消等待」不叫「取消」——名实相符比客气重要。lane 是串行的
// （JVM 与批量校验共用一条），取消后马上重新调试要排队等它让出来，文案必须说到这层。
import { ref, computed, watch } from "vue";
import { ElMessageBox } from "element-plus";
import { appDebug, appPreflight, jvmDebug } from "../api/rules";
import { getJobDetail, listJobs } from "../api/jobs";
import { useJobs } from "../composables/useJobs";
import { asRunStatus } from "../utils/runStream";
import { jvmReadiness } from "../api/jvm.js";
import { getSettings } from "../api/settings";
import { nextCompareState } from "../utils/debugCompare";
import { readDebugPreferences, writeDebugPreferences } from "../utils/debugPreferences";

// 目标与缓存档：UI 配置的唯一一份（弹框与工作台页共用）。hint 只回答「这格
// 填什么」；详情/目录/正文留空会退回搜索入口，placeholder 写「可留空」就够
export const DEBUG_TARGETS = [
  { value: "search", label: "搜索", hint: "关键词，如 我的" },
  { value: "explore", label: "发现", hint: "留空则用配置里的 exploreUrl" },
  { value: "info", label: "详情", hint: "详情页 URL，可留空" },
  { value: "toc", label: "目录", hint: "目录页 URL，可留空" },
  { value: "content", label: "正文", hint: "正文页 URL，可留空" },
];
// 三个值对应 core.fetch 的 CACHE_*，后端按同一份枚举校验
export const DEBUG_CACHE_MODES = [
  { value: "auto", label: "用缓存" },
  { value: "only", label: "只读缓存" },
  { value: "refresh", label: "忽略缓存" },
];
// 「从此步重跑」的步骤名 → 调试目标。bookUrl 与「详情」是同一步的两个名字
// （步骤名是后端 verify 链的词表，target 是调试入口的词表）。search 不在表里：
// 搜索段重跑就是整链入口，调用方直接走常规启动。key 的拼装在后端
// `core/debug_keys`——App 的 key 语法只有那一份，前端不再各抄一遍
export const STEP_TARGETS = { explore: "explore", bookUrl: "info", toc: "toc", content: "content" };

// ---------------------------------------------------------------- 结果与对比

const result = ref(null);
const resultRevision = ref(null);
let activeResultRevision = null;
const compare = ref({ prev: null, history: [] });
// 新结果到来时滚动对比基线；错误体不覆盖基线（判据在 utils/debugCompare，有测试）
watch(result, (cur, old) => {
  compare.value = nextCompareState(compare.value, old, cur);
});

// 源草稿由 SourceWorkspace 管理；session 只保存运行参数和结果。

// ---------------------------------------------------------------- 运行在途

const running = ref(false);
let ticker = 0;
let abort = null;
let runPending = null;
let stopWatch = null;
//: 「不再等待」的出口（等结果那一步）。见 `waitForJob`
let cancelPending = null;
const CANCEL_WAIT_NOTE = "已不再等待；后端那一次仍会跑完，结果留在那次调试的记录里";

// ---------------------------------------------------------------- 等待观测
//
// 调试是**任务**（`kind="jvm_debug"`）：状态、阶段、已等秒数、谁占着引擎都来自那一条
// 观测流（`useJobs.watchJob` + `utils/runStream.asRunStatus`）。后端只给码与事实，
// 中文句子在 `utils/debugRun` 出——**与批量校验共用同一份取词**。不做进度、不做 ETA。
//
// 本地秒表只负责"帧与帧之间让数字继续走"：`debugRunState` 取
// `max(后端已等, 本地已等)`，所以刷新/重开页面之后前端也不需要记住起点。
const runId = ref("");
//: 帧里的观测快照（phase / elapsed_ms / phase_ms / lane_holder / lane_waiting）。
//: **算出来而不是存起来**：它就是那条流当前状态的一个投影，存一份就会有两份真相。
const runStatus = computed(() => {
  const frame = frameOf(runId.value);
  return frame && frame.job ? asRunStatus(frame.job) : null;
});
//: 秒表的毫秒精度副本。相位交接判据与「上次用时」都要毫秒——两者由**同一次 tick**
//: 一起推进，不另起一个表
const elapsedMs = ref(0);
let waitStartedAt = 0;
//: 上一次运行**结束**的墙钟时刻与用时。用途只有一个：抽屉关着的时候跑完的那次，
//: 重开时界面要能说出「刚才那次已经跑完了」——否则它看起来像从没跑过
//: （`null` = 还没有过结果）
const lastRunAt = ref(null);
const lastRunMs = ref(0);

const { watchJob } = useJobs();

function errorMessage(e) {
  if (e && e.message) return String(e.message);
  if (e && e.detail) return String(e.detail);
  if (e && e.error) return String(e.error);
  return String(e || "未知错误");
}

/** 「不再等待」：关掉那条流，后端那一次照跑完（结果留在任务行里）。 */
function cancelRun() {
  // 提交还没回来时，abort 掉那次请求；已经拿到任务号之后，中断的是"等结果"这一步
  if (abort) abort.abort();
  if (cancelPending) cancelPending();
}

/**
 * 等一个调试任务跑完，返回它的结果体。
 *
 * 状态与失败原因从流里来（`state.job.error` 已含中止原因）；**结果体**走一次
 * `getJobDetail`——那是 `steps/pages`，不给流推。**取消等待只关流、不杀任务**：
 * 引擎那一次没有可恢复的中间态，掐掉等于白跑。
 */
function waitForJob(jobId) {
  runId.value = jobId;
  return new Promise((resolve) => {
    const stop = watchJob(jobId, async (state) => {
      if (stopWatch === stop) stopWatch = null;
      cancelPending = null;
      stop();
      const status = (state && state.job && state.job.status) || "";
      if (status !== "done") {
        resolve({
          error: (state && state.job && state.job.error)
            || (status === "cancelled" ? CANCEL_WAIT_NOTE : errorMessage(status || "任务没有结果")),
          cancelled: status === "cancelled",
          status,
        });
        return;
      }
      try {
        const detail = await getJobDetail(jobId);
        resolve(detail.result || { error: "调试没有返回结果" });
      } catch (e) {
        resolve({ error: "调试结果读取失败：" + errorMessage(e) });
      }
    });
    stopWatch = stop;
    //: 这一等的"不看了"出口。**没有它，「不再等待」在提交返回之后就是个空按钮**
    //: （旧实现靠 abort 掉那个同步长轮询，现在等的是流）
    cancelPending = () => {
      cancelPending = null;
      if (stopWatch) { stopWatch(); stopWatch = null; }
      resolve({ error: CANCEL_WAIT_NOTE, cancelled: true, status: "detached" });
    };
  });
}

/**
 * 重新挂上那次还在跑的调试（刷新页面、关掉再打开、另一个标签页都算）。
 *
 * 有任务行才挂得上——这正是调试进 jobs 表的收益之一：观测登记放在进程内存里时，
 * 一次刷新就等于"那次调试从界面上消失了"。
 */
async function resumeRunning() {
  if (running.value) return runId.value;
  let row = null;
  try {
    const page = await listJobs({ kind: "jvm_debug", status: "active" });
    row = ((page && page.items) || [])[0] || null;
  } catch (e) {
    return "";
  }
  if (!row || !row.id) return "";
  beginWait();
  try {
    storeResult(await waitForJob(row.id));
    return row.id;
  } finally {
    endWait();
    lastRunMs.value = elapsedMs.value;
    lastRunAt.value = Date.now();
  }
}

//: 当前这次调试**正在产生**的过程事件（引擎逐条 flush 的那份，已经被搬进事件账本）。
//: 与结果里的 `events` 同源同形（`{t, text}`），区别只是"现在就能看"——这正是
//: 同步长轮询时代缺的那个产物：等待期不再是空等待。
const liveEvents = computed(() => {
  const frame = frameOf(runId.value);
  return ((frame && frame.events) || [])
    .filter((ev) => ev.kind === "debug")
    .map((ev) => ({ ts: ev.t, text: ev.text }));
});

function beginWait() {
  running.value = true;
  elapsedMs.value = 0;
  waitStartedAt = Date.now();
  abort = new AbortController();
  ticker = window.setInterval(() => {
    // 由**钟**算，不由 tick 次数累加：间隔被节流/挂起时读数才仍然是真实的已等时长
    elapsedMs.value = Date.now() - waitStartedAt;
  }, 1000);
}

function endWait() {
  window.clearInterval(ticker);
  abort = null;
  cancelPending = null;
  running.value = false;
  // 收尾时补一次读数：最后一次 tick 与结束之间可能差半秒，用来显示「上次用时」
  elapsedMs.value = waitStartedAt ? Date.now() - waitStartedAt : elapsedMs.value;
}

// ---------------------------------------------------------------- 通道与入口

//: 调试通道：**默认本机引擎**（App 的源码跑在本机，不填 IP、不推送）。连 App 那条
//: 留着——登录态、网络出口、WebView 都在手机上，那是它不可替代的地方
const channel = ref("jvm");
const target = ref("search");
const query = ref("");
const cacheMode = ref("auto");

const activeSourceUrl = ref("");

function setActiveSourceUrl(url) {
  activeSourceUrl.value = String(url || "");
  const prefs = readDebugPreferences(activeSourceUrl.value);
  if (!prefs) return;
  if (prefs.target) target.value = prefs.target;
  if (prefs.query !== undefined) query.value = prefs.query;
  if (prefs.channel) channel.value = prefs.channel;
  if (prefs.cacheMode) cacheMode.value = prefs.cacheMode;
}

watch([target, query, channel, cacheMode], () => {
  if (!activeSourceUrl.value) return;
  writeDebugPreferences(activeSourceUrl.value, {
    target: target.value,
    query: query.value,
    channel: channel.value,
    cacheMode: cacheMode.value,
  });
});

// App 的 IP 存 localStorage：它基本不变，每次打开弹窗重填一遍没有意义。
// 键名带 legado 前缀，避免同域下与别的应用串味。隐私模式等 localStorage
// 不可用的场景静默降级成空值
const APP_HOST_STORAGE_KEY = "legado.appHost";
const host = ref(
  (() => {
    try {
      return localStorage.getItem(APP_HOST_STORAGE_KEY) || "";
    } catch (e) {
      return "";
    }
  })(),
);
// ---------------------------------------------------------------- 环境（本机引擎）

const env = ref(null);
const envLoading = ref(false);
const envTitle = computed(() => {
  if (envLoading.value) return "正在检查本机引擎环境…";
  return env.value?.ok ? "本机引擎可用" : "本机引擎不可用";
});
// 调试预算口径（debug.timeout）：默认值只在后端 settings_store（AGENTS #8），
// 这里读来显示；请求不传 timeout，由后端取同一份
const budget = ref(null);

let environmentPending = null;

function loadEnvironment() {
  if (environmentPending) return environmentPending;
  envLoading.value = true;
  const pending = (async () => {
    const settingsPending = getSettings()
      .then((s) => {
        budget.value =
          (s.values.debug || {}).timeout ?? s.defaults?.debug?.timeout ?? null;
      })
      .catch(() => {});
    try {
      env.value = await jvmReadiness();
    } catch (e) {
      env.value = {
        ok: false,
        checks: [{ name: "环境就绪接口", hint: "环境检查失败：" + errorMessage(e) }],
      };
    } finally {
      await settingsPending;
      envLoading.value = false;
    }
    return env.value;
  })();
  const shared = pending.finally(() => {
    if (environmentPending === shared) environmentPending = null;
  });
  environmentPending = shared;
  return shared;
}

// ---------------------------------------------------------------- 连 App

// 预检结果：null=还没测过 / {state: ready|missing|unreachable, error}
const preflightState = ref(null);
const checking = ref(false);
// 本次调试前做了什么推送："" | "missing"（新建）| "stale"（覆盖旧规则）
const pushed = ref("");

// 在途的预检请求。输入框失焦和「连 App 调试」会问同一个问题——合成一次：
// 并发会让「检测中…」在第一个先回来时提前熄灭
let preflightPending = null;
let preflightGeneration = 0;

// 只读预检：连不连得上、App 里有没有这个源、是不是旧版本。结果写进
// preflightState（卡片与调试流程都读它），不弹 toast——标签就在输入框旁
function preflightKey(runSource, host_) {
  return JSON.stringify([String(host_ || "").trim(), runSource || null]);
}

function cancelledError() {
  const error = new Error(CANCEL_WAIT_NOTE);
  error.name = "AbortError";
  return error;
}

async function waitForPreflight(promise, signal) {
  if (signal?.aborted) throw cancelledError();
  const state = await promise;
  if (signal?.aborted) throw cancelledError();
  return state;
}

async function runPreflight(runSource, host_, signal = null) {
  const key = preflightKey(runSource, host_);
  if (preflightPending && preflightPending.key === key) {
    return waitForPreflight(preflightPending.promise, signal);
  }

  const generation = ++preflightGeneration;
  const promise = (async () => {
    checking.value = true;
    let state;
    try {
      // 用源对象：它的 bookSourceUrl 是导入原文，App 按精确字符串匹配；
      // 整份传过去才能和 App 里那份比对规则（core/app_debug.py:preflight）
      state = await appPreflight(runSource, host_, 0);
    } catch (e) {
      if (e && e.name === "AbortError") throw e;
      state = {
        state: "unreachable",
        error: errorMessage(e),
        detail: e && e.detail ? String(e.detail) : "",
      };
    } finally {
      // 取消等待也要关掉输入框旁的「检测中…」；迟到请求只负责返回，不能
      // 越权清掉后来那一次预检。
      if (generation === preflightGeneration) {
        checking.value = false;
        preflightPending = null;
      }
    }
    // 输入变了以后，旧请求的迟到结果不能覆盖当前源 / 当前 App 的状态。
    if (generation === preflightGeneration) {
      preflightState.value = state;
      if (state.detail) console.warn("[app-debug] 预检失败:", state.detail);
    }
    return state;
  })();
  preflightPending = { key, promise };
  return waitForPreflight(promise, signal);
}

// 推送前确认。「调试」和「往 App 里写数据」是两件事——把后者做成前者的隐式
// 副作用，用户点的是调试、App 里的源却被改了。stale 那档更是把「你调试的是
// App 那份，不是你正在改的」摆到用户面前
async function confirmPush(state) {
  const text =
    state === "missing"
      ? "App 里没有这个源。要先推送到 App 再调试吗？"
      : "App 里是旧版本。直接调试会跑 App 里那份规则，不是你正在改的。" +
        "要先推过去覆盖它再调试吗？";
  return ElMessageBox.confirm(text, "推送并调试", {
    confirmButtonText: "推送并调试",
    cancelButtonText: "取消",
    type: "warning",
  })
    .then(() => true)
    .catch(() => false);
}

// App IP 持久化就在会话内做：存 trim 后的值，清空删键——下次打开是干净的
// 空值，「连 App 调试」会正常给出「请先填 App 的 IP」。写不进去不影响本次
watch(host, (value, oldValue) => {
  const h = String(value || "").trim();
  try {
    if (h) localStorage.setItem(APP_HOST_STORAGE_KEY, h);
    else localStorage.removeItem(APP_HOST_STORAGE_KEY);
  } catch (e) {
    /* 同上 */
  }
  // 预检和推送都绑定 App 实例；换地址后，旧状态不能继续冒充当前实例。
  if (oldValue !== undefined && h !== String(oldValue || "").trim()) {
    clearPreflight();
    pushed.value = "";
  }
});

// 结果落位只在这里统一：保留后端错误体里的 detail / 原始字段，不能把用户可见原因
// 削成只有一行 error；null 也要给出明确原因，不能让界面伪装成「尚未调试」。
function storeResult(value, revision = null) {
  result.value = value == null ? { error: "调试没有返回结果" } : value;
  resultRevision.value = revision == null ? activeResultRevision : revision;
  return result.value;
}

function setResult(value, revision = null) {
  // 生成链路塞进来的首屏证据对应刚 commit 的那版草稿：不显式给 revision 会拿
  // 到上一次运行的旧版次，调试页立刻误报「规则已修改」
  return storeResult(value, revision);
}

function clearResult() {
  result.value = null;
  resultRevision.value = null;
  compare.value = { prev: null, history: [] };
}

function clearPreflight() {
  preflightGeneration += 1;
  preflightPending = null;
  checking.value = false;
  preflightState.value = null;
  pushed.value = "";
}

function invalidatePreflight() {
  // missing/stale/unreachable 对本地规则改动仍成立；但在途请求和 ready
  // 都绑定旧输入，必须作废。推送状态更严格：它绑定整份已推送快照，规则
  // 一改就不能继续显示「已推送」。
  if (preflightPending || checking.value || (preflightState.value || {}).state === "ready") {
    clearPreflight();
  }
  pushed.value = "";
}

function resetDebugState() {
  clearResult();
  clearPreflight();
  pushed.value = "";
}

// ---------------------------------------------------------------- 一次运行

/**
 * 跑一次调试（两条通道唯一的入口）。
 *
 * @param {Object}   p
 * @param {Object}   p.source      源对象（bookSourceUrl 必须是导入原文）
 * @param {string}   p.target      调试目标（DEBUG_TARGETS / STEP_TARGETS 的取值）
 * @param {string}   p.query       用户输入（关键词或 URL 原文；key 由后端 core.debug_keys 拼装）
 * @param {Function} [p.confirmPush] 连 App 且要推送时的确认回调（返回是否同意）；
 *                                 不给就视为拒绝——推送必须过用户
 * @returns {Object} 结果体（含 error 时调用方自行呈现）
 */
async function executeRun({ source: runSource, target: runTarget, query: runQuery,
                             confirmPush: askPush, draftRevision = null }) {
  // 一次运行使用固定快照；用户在等待期间切换通道、App 地址或缓存档，
  // 不应把「预检的是 A、实际跑的是 B」拼成一条结论。
  const runChannel = channel.value;
  const runHost = String(host.value || "").trim();
  const runCacheMode = cacheMode.value;
  activeResultRevision = draftRevision;
  const runSourceSnapshot = runSource && typeof runSource === "object"
    ? JSON.parse(JSON.stringify(runSource))
    : runSource;
  // 上一次运行的观测句柄在提交之后才有（本机引擎那条）：它是任务号。
  // 连 App 走的是设备 WS，没有任务行，等待期只有本地秒表。
  runId.value = "";
  beginWait();
  pushed.value = "";
  try {
    if (runChannel === "jvm") {
      const submitted = await jvmDebug({ source: runSourceSnapshot, target: runTarget,
        query: runQuery, cache: runCacheMode, signal: abort?.signal });
      const jobId = (submitted && submitted.job_id) || "";
      if (!jobId) {
        return storeResult({ error: "调试任务没有拿到任务号，请重试" });
      }
      // 结果体走 `getJobDetail`（那是 steps/pages），状态与失败原因走那条流
      return storeResult(await waitForJob(jobId));
    }
    // 连 App：先预检——调试 WS 对 App 库里查不到的 tag 静默无响应，而且它跑的
    // 始终是 **App 里那份规则**，预检把「是旧版本」一并判掉
    const pf = await runPreflight(runSourceSnapshot, runHost,
      abort ? abort.signal : null);
    if (pf.state === "unreachable") {
      return storeResult({ error: pf.error || "连不上 App", detail: pf.detail || "" });
    }
    const needPush = pf.state !== "ready";
    if (needPush && !(askPush ? await askPush(pf.state) : false)) {
      return null; // 用户取消推送：保留原结果，但不让调用方误当成这次运行的结果
    }
    const r = await appDebug({ source: runSourceSnapshot, target: runTarget,
      query: runQuery, host: runHost, port: 0, push: needPush,
      cache: runCacheMode, signal: abort?.signal });
    const stored = storeResult(r);
    if (!stored.error) {
      pushed.value = needPush ? pf.state : "";
      preflightState.value = { state: "ready", error: "", detail: "" };
    }
    return stored;
  } catch (e) {
    // 取消等待不是失败：单独标记，界面按提示而不是错误渲染（结果体里 error 仍要有值，
    // 调用方「结果体含 error 时自行呈现」的约定不变）
    const cancelled = !!(e && e.name === "AbortError");
    return storeResult({
      error: cancelled ? CANCEL_WAIT_NOTE : errorMessage(e),
      detail: e && e.detail ? e.detail : "",
      cancelled,
    });
  } finally {
    // 先收秒表再记用时：`endWait` 会补最后一次读数（最后一次 tick 到结束之间
    // 可能差将近一秒），顺序反了「上次用时」就会永远短一截
    endWait();
    lastRunMs.value = elapsedMs.value;
    lastRunAt.value = Date.now();
  }
}

// UI 已经在 running 时禁用重跑；这里再收一层，防止两个入口在同一个
// 单例会话上同时开启两条请求，互相覆盖秒表、取消器和结果。
function startRun(params) {
  if (runPending) return runPending;
  const pending = executeRun(params);
  const shared = pending.finally(() => {
    if (runPending === shared) runPending = null;
  });
  runPending = shared;
  return shared;
}

export function useDebugSession() {
  return {
    // 状态
    result,
    resultRevision,
    compare,
    setResult,
    clearResult,
    resetDebugState,
    clearPreflight,
    invalidatePreflight,
    running,
    elapsedMs,
    runId,
    runStatus,
    liveEvents,
    lastRunAt,
    lastRunMs,
    budget,
    channel,
    target,
    query,
    cacheMode,
    host,
    env,
    envLoading,
    envTitle,
    preflightState,
    checking,
    pushed,
    // 动作
    startRun,
    cancelRun,
    resumeRunning,
    confirmPush,
    loadEnvironment,
    runPreflight,
    setActiveSourceUrl,
  };
}
