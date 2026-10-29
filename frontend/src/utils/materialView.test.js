import assert from "node:assert/strict";
import test from "node:test";
import { findHits, segmentsOf, viewWindow } from "./materialView.js";

test("分块窗口按原文字符数截，hasMore 只在真有剩余时为真", () => {
  assert.deepEqual(viewWindow("abcdef", 3), { head: "abc", hasMore: true });
  assert.deepEqual(viewWindow("abc", 3), { head: "abc", hasMore: false });
  assert.deepEqual(viewWindow("", 10), { head: "", hasMore: false });
  assert.deepEqual(viewWindow(null, 10), { head: "", hasMore: false });
});

test("恰好占满上限不算截断（否则计数器会说「命中过多」）", () => {
  const exact = findHits("aa", "a", 2);
  assert.deepEqual(exact.offsets, [0, 1]);
  assert.equal(exact.truncated, false);

  const over = findHits("aaa", "a", 2);
  assert.deepEqual(over.offsets, [0, 1]);
  assert.equal(over.truncated, true);
});

test("切片跳过窗口外的位置与重叠命中，序号仍按命中序列", () => {
  // "abab"：命中在 0 与 2
  const segs = segmentsOf("abab", [0, 2], "ab");
  assert.deepEqual(segs, [
    { text: "ab", hit: true, index: 0 },
    { text: "ab", hit: true, index: 1 },
  ]);
  // 落到文本外的命中被丢掉，剩下的序号保持原样（跳转按钮按序号定位）
  const outOfWindow = segmentsOf("ab", [0, 5], "ab");
  assert.deepEqual(outOfWindow, [{ text: "ab", hit: true, index: 0 }]);
  // 没有命中词 → 整段普通文本
  assert.deepEqual(segmentsOf("ab", [], ""), [{ text: "ab", hit: false, index: -1 }]);
});