import test from "node:test";
import assert from "node:assert/strict";
import { assignRenameBadges, renamedSourceName } from "./renameBadges.js";

test("递增角标按右侧选中顺序分配指定字符", () => {
  assert.deepEqual(assignRenameBadges(["first", "second", "third"], "incremental"), ["ᵃ", "ᵇ", "ᶜ"]);
});

test("手工角标逐项指定，空值表示该源不加角标", () => {
  assert.deepEqual(assignRenameBadges(["first", "second", "third"], "manual", {
    first: "¹",
    second: "",
    third: "ᵇ",
  }), ["¹", "", "ᵇ"]);
});

test("名称后缀去除首尾空格并安全处理空名称", () => {
  assert.equal(renamedSourceName("书旗小说", "  版本 ", "ᵃ"), "书旗小说版本ᵃ");
  assert.equal(renamedSourceName(null, " ", "²"), "²");
});
