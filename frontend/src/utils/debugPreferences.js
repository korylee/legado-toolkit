// 调试入口的本地偏好：只记住操作习惯，不记规则、Cookie 或登录态。
const STORAGE_KEY = "legado.debugPreferences.v1";

export function normalizePreferenceUrl(url) {
  const text = String(url || "").trim();
  if (!text) return "";
  try {
    const u = new URL(text);
    u.hash = "";
    u.hostname = u.hostname.toLowerCase();
    return u.href.replace(/\/$/, "");
  } catch (e) {
    return text.replace(/\/$/, "").toLowerCase();
  }
}

function readAll() {
  try { return JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}"); }
  catch (e) { return {}; }
}

export function readDebugPreferences(url) {
  const key = normalizePreferenceUrl(url);
  if (!key) return null;
  const all = readAll();
  return all[key] && typeof all[key] === "object" ? all[key] : null;
}

export function writeDebugPreferences(url, prefs) {
  const key = normalizePreferenceUrl(url);
  if (!key) return;
  try {
    const all = readAll();
    all[key] = {
      target: String(prefs.target || "search"),
      query: String(prefs.query || ""),
      channel: String(prefs.channel || "jvm"),
      cacheMode: String(prefs.cacheMode || "auto"),
    };
    localStorage.setItem(STORAGE_KEY, JSON.stringify(all));
  } catch (e) { /* 隐私模式或存储不可用时不影响调试 */ }
}

export const DEBUG_PREFERENCES_STORAGE_KEY = STORAGE_KEY;
