import test from "node:test";
import assert from "node:assert/strict";
import {
  DEBUG_PREFERENCES_STORAGE_KEY,
  normalizePreferenceUrl,
  readDebugPreferences,
  writeDebugPreferences,
} from "./debugPreferences.js";

test("调试偏好使用归一化源 URL", () => {
  assert.equal(normalizePreferenceUrl(" HTTPS://Example.COM/source/ "), "https://example.com/source");
  assert.equal(normalizePreferenceUrl(""), "");
});

test("调试偏好钉住存储键和默认值", () => {
  const values = new Map();
  globalThis.localStorage = {
    getItem: (key) => values.get(key) || null,
    setItem: (key, value) => values.set(key, String(value)),
  };

  writeDebugPreferences("https://Example.COM/source/", { query: "书名" });

  assert.equal(DEBUG_PREFERENCES_STORAGE_KEY, "legado.debugPreferences.v1");
  assert.deepEqual(readDebugPreferences("https://example.com/source"), {
    target: "search",
    query: "书名",
    channel: "jvm",
    cacheMode: "auto",
  });

  delete globalThis.localStorage;
});
