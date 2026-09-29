import assert from "node:assert/strict";
import test from "node:test";

import { debugOutletFor } from "./debugOutlets.js";

test("本机 unknown 的动态层提供 App 真机复查出口", () => {
  for (const layer of ["L2", "L3", "L4", "L5"]) {
    const outlet = debugOutletFor({ channel: "jvm", verdict: "unknown", layer });
    assert.equal(outlet.kind, "app");
    assert.equal(outlet.label, "连 App 调试（真机复查）");
  }
});

test("明确失败不自动改说成真机问题", () => {
  assert.equal(debugOutletFor({ channel: "jvm", verdict: "fail", layer: "L2" }), null);
  assert.equal(debugOutletFor({ channel: "jvm", verdict: "fail", layer: "L1" }), null);
});

test("App 结果、L1 unknown 和未定层都不制造出口", () => {
  assert.equal(debugOutletFor({ channel: "app", verdict: "unknown", layer: "L2" }), null);
  assert.equal(debugOutletFor({ channel: "jvm", verdict: "unknown", layer: "L1" }), null);
  assert.equal(debugOutletFor({ channel: "jvm", verdict: "unknown", layer: "" }), null);
});
