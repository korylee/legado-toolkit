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
// 所以按钮叫「取消等待」不叫「取消」——名实相符比客气重要。
import { ref, computed, watch } from "vue";
import { ElMessageBox } from "element-plus";
import { jvmDebug, appDebug, appPreflight } from "../api/rules";
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
const compare = ref({ prev: null, history: [] });
// 新结果到来时滚动对比基线；错误体不覆盖基线（判据在 utils/debugCompare，有测试）
watch(result, (cur, old) => {
  compare.value = nextCompareState(compare.value, old, cur);
});

// 会话源（工作台的编辑载体）：列表页经 getDetail 填充、编辑弹框在跳工作台前
// 填**表单快照**（深拷贝——工作台里的规则编辑不穿透弹框）。不落库，保存走
// 各自的 saveSource
const source = ref(null);
const savedSourceSnapshot = ref("");
const sourceDirty = computed(() => {
  if (!source.value || !savedSourceSnapshot.value) return false;
  return JSON.stringify(source.value) !== savedSourceSnapshot.value;
});

function markSourceSaved() {
  savedSourceSnapshot.value = source.value ? JSON.stringify(source.value) : "";
}

function setSource(s, options = {}) {
  source.value = s ? JSON.parse(JSON.stringify(s)) : null;
  if (options.saved !== false) markSourceSaved();
  const prefs = readDebugPreferences((source.value || {}).bookSourceUrl);
  if (!prefs) return;
  if (prefs.target) target.value = prefs.target;
  if (prefs.query !== undefined) query.value = prefs.query;
  if (prefs.channel) channel.value = prefs.channel;
  if (prefs.cacheMode) cacheMode.value = prefs.cacheMode;
}

function updateSourceField(field, value) {
  const parts = String(field || "").split(".");
  if (parts.length === 1) {
    if (parts[0] !== "bookSourceUrl" || !source.value) return false;
    source.value.bookSourceUrl = value;
    clearPreflight();
    return true;
  }
  if (parts.length !== 2) return false;
  const [group, key] = parts;
  if (!source.value?.[group]) return false;
  source.value[group][key] = value;
  invalidatePreflight();
  return true;
}

// ---------------------------------------------------------------- 运行在途

const running = ref(false);
const elapsed = ref(0);
let ticker = 0;
let abort = null;
let runPending = null;
const CANCEL_WAIT_NOTE = "已取消等待；后端那一次调试仍会跑完，只是不再等它";

function errorMessage(e) {
  if (e && e.message) return String(e.message);
  if (e && e.detail) return String(e.detail);
  if (e && e.error) return String(e.error);
  return String(e || "未知错误");
}

function abortNote(e) {
  return e && e.name === "AbortError" ? CANCEL_WAIT_NOTE : errorMessage(e);
}

function cancelRun() {
  if (abort) abort.abort();
}

function beginWait() {
  running.value = true;
  elapsed.value = 0;
  abort = new AbortController();
  ticker = window.setInterval(() => {
    elapsed.value += 1;
  }, 1000);
}

function endWait() {
  window.clearInterval(ticker);
  abort = null;
  running.value = false;
}

// ---------------------------------------------------------------- 通道与入口

//: 调试通道：**默认本机引擎**（App 的源码跑在本机，不填 IP、不推送）。连 App 那条
//: 留着——登录态、网络出口、WebView 都在手机上，那是它不可替代的地方
const channel = ref("jvm");
const target = ref("search");
const query = ref("");
const cacheMode = ref("auto");

watch([source, target, query, channel, cacheMode], () => {
  const url = (source.value || {}).bookSourceUrl;
  if (!url) return;
  writeDebugPreferences(url, {
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
function storeResult(value) {
  result.value = value == null ? { error: "调试没有返回结果" } : value;
  return result.value;
}

function setResult(value) {
  return storeResult(value);
}

function clearResult() {
  result.value = null;
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
                            confirmPush: askPush }) {
  // 一次运行使用固定快照；用户在等待期间切换通道、App 地址或缓存档，
  // 不应把「预检的是 A、实际跑的是 B」拼成一条结论。
  const runChannel = channel.value;
  const runHost = String(host.value || "").trim();
  const runCacheMode = cacheMode.value;
  const runSourceSnapshot = runSource && typeof runSource === "object"
    ? JSON.parse(JSON.stringify(runSource))
    : runSource;
  beginWait();
  pushed.value = "";
  try {
    if (runChannel === "jvm") {
      const r = await jvmDebug(runSourceSnapshot, runTarget, runQuery, null, "",
        runCacheMode, abort ? abort.signal : null);
      return storeResult(r);
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
    const r = await appDebug(runSourceSnapshot, runTarget, runQuery, runHost, 0,
      needPush, runCacheMode, abort ? abort.signal : null);
    const stored = storeResult(r);
    if (!stored.error) {
      pushed.value = needPush ? pf.state : "";
      preflightState.value = { state: "ready", error: "", detail: "" };
    }
    return stored;
  } catch (e) {
    return storeResult({ error: abortNote(e), detail: e && e.detail ? e.detail : "" });
  } finally {
    endWait();
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
    source,
    sourceDirty,
    markSourceSaved,
    setSource,
    updateSourceField,
    result,
    compare,
    setResult,
    clearResult,
    resetDebugState,
    clearPreflight,
    invalidatePreflight,
    running,
    elapsed,
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
    abortNote,
    confirmPush,
    loadEnvironment,
    runPreflight,
  };
}
