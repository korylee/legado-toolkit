import test from "node:test";
import assert from "node:assert/strict";
import { normalizeForSave } from "./sourceSave.js";

test("bookSourceType 脏值一律归 0，合法值原样保留", () => {
  assert.equal(normalizeForSave({ bookSourceType: "" }).bookSourceType, 0);
  assert.equal(normalizeForSave({ bookSourceType: "2" }).bookSourceType, 2);
  assert.equal(normalizeForSave({ bookSourceType: 4 }).bookSourceType, 0);
  assert.equal(normalizeForSave({ bookSourceType: 3 }).bookSourceType, 3);
});

test("enabledExplore 由配置推导：没配发现规则一律关", () => {
  assert.equal(
    normalizeForSave({ enabledExplore: true, exploreUrl: "" }).enabledExplore,
    false,
  );
  assert.equal(
    normalizeForSave({ enabledExplore: true, ruleExplore: { a: "b" } }).enabledExplore,
    true,
  );
});

test("配了发现但用户选择隐藏（enabledExplore=false）时保持隐藏", () => {
  assert.equal(
    normalizeForSave({ enabledExplore: false, exploreUrl: "/list" }).enabledExplore,
    false,
  );
});

test("normalizeForSave 不改写入参，返回独立副本", () => {
  const raw = { bookSourceType: "1", ruleExplore: { a: "b" } };
  const out = normalizeForSave(raw);
  assert.equal(raw.bookSourceType, "1");
  assert.notEqual(out.ruleExplore, raw.ruleExplore);
});
