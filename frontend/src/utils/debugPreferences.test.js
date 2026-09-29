import test from "node:test";
import assert from "node:assert/strict";
import { normalizePreferenceUrl } from "./debugPreferences.js";

test("调试偏好使用归一化源 URL", () => {
  assert.equal(normalizePreferenceUrl(" HTTPS://Example.COM/source/ "), "https://example.com/source");
  assert.equal(normalizePreferenceUrl(""), "");
});
