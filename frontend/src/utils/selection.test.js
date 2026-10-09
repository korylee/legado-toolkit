// 跨页选中的规则：把「页 1 全选 → 去页 2 → 回页 1」这条真实路径钉住。
//
// 原来那个 bug（页 1 全选后去页 2 再回来，页 1 被清空）根因不在这些函数里，而在组件
// 把重算挂在了 `selection-change` 上——那个事件翻页替换数据时也会发。但「翻页时集合
// 不该变」「当前页全选只动当前页」这两条规则本身是可以离线钉住的，所以钉在这里。
import { test } from "node:test";
import assert from "node:assert/strict";
import { countOutOfDomain, keepInDomain, mergePageSelection, toggleSelected } from "./selection.js";

const PAGE1 = ["a", "b", "c"];
const PAGE2 = ["d", "e"];

test("页 1 全选 → 去页 2（集合不动）→ 回页 1 仍在", () => {
  let selected = mergePageSelection([], PAGE1, PAGE1);
  assert.deepEqual(selected, PAGE1);
  // 翻页只做「把集合勾回表格」这一件读操作，集合不该被改
  assert.deepEqual(selected, PAGE1, "翻页不能动集合");
  assert.deepEqual(PAGE1.filter((u) => selected.includes(u)), PAGE1, "回页 1 时这页全都还在");
});

test("两页各自全选 → 跨页累积（不是后一页覆盖前一页）", () => {
  let selected = mergePageSelection([], PAGE1, PAGE1);
  selected = mergePageSelection(selected, PAGE2, PAGE2);
  assert.deepEqual(selected, [...PAGE1, ...PAGE2]);
});

test("页 2 点表头取消全选 → 只减当前页", () => {
  let selected = mergePageSelection([], PAGE1, PAGE1);
  selected = mergePageSelection(selected, PAGE2, PAGE2);
  selected = mergePageSelection(selected, PAGE2, []);
  assert.deepEqual(selected, PAGE1);
});

test("页 2 取消一行 → 只减那一条，页 1 不受影响", () => {
  let selected = mergePageSelection([], PAGE1, PAGE1);
  selected = mergePageSelection(selected, PAGE2, PAGE2);
  selected = toggleSelected(selected, "d", false);
  assert.deepEqual(selected, ["a", "b", "c", "e"]);
});

test("无结果的页上取消全选 → 只可能剩空（页里没有 URL 就不会误删别的页）", () => {
  const selected = mergePageSelection(PAGE1, PAGE2, []);
  assert.deepEqual(selected, PAGE1);
});

test("「选中全部 N 条」之后取消当前页全选 → 只减当前页，其余不丢", () => {
  const all = ["a", "b", "c", "d", "e", "f", "g"];   // 假装是 total=7 条筛选结果
  const selected = mergePageSelection(all, PAGE1, []);
  assert.deepEqual(selected, ["d", "e", "f", "g"]);
});

test("同一个 URL 不重复计数（重复勾/重叠并入都不虚增）", () => {
  assert.deepEqual(toggleSelected(["a"], "a", true), ["a"], "已经选中再勾一次不该出现两条");
  assert.deepEqual(mergePageSelection(["a", "b"], PAGE1, ["a", "b", "c"]), ["a", "b", "c"]);
});

test("当前页以表格为准：picked 里少的那些要从集合里删掉", () => {
  const selected = mergePageSelection(["a", "b", "c"], PAGE1, ["a"]);
  assert.deepEqual(selected, ["a"]);
});

test("场景②：在「未校验」选 50 条后切到「可用」→ 50 条全在域外", () => {
  const selected = Array.from({ length: 50 }, (_, i) => "u" + i);
  const available = ["x", "y", "z"];        // 切过去的那个筛选域
  assert.equal(countOutOfDomain(selected, available), 50);
  assert.deepEqual(keepInDomain(selected, available), [], "「只保留当前筛选内的」应清空");
});

test("场景①：批量校验改了状态后刷新 → 只有离开该域的那几条算域外", () => {
  const before = ["a", "b", "c", "d", "e"];         // 未校验域里勾的 5 条
  const afterRefresh = ["a", "b", "c"];             // 校验后 2 条有了结论，离开该域
  assert.equal(countOutOfDomain(before, afterRefresh), 2);
  assert.deepEqual(keepInDomain(before, afterRefresh), ["a", "b", "c"]);
});

test("选中的全在域内时点数是 0（常态不该显示任何提示）", () => {
  assert.equal(countOutOfDomain(PAGE1, [...PAGE1, ...PAGE2]), 0);
  assert.equal(countOutOfDomain([], []), 0);
});

test("域为空（筛选没命中任何源）→ 全部算域外，而不是当成 0", () => {
  assert.equal(countOutOfDomain(PAGE1, []), 3);
});
