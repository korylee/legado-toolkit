// selector 的断言（纯函数 + 一个极小的假 DOM；node --test 直接跑）。
//
// 为什么连「去重 K」都要钉：**「命中 1228」与「命中 1198」在界面上都只是个数字**，
// 能分开它们的是去重后条数与占比（TODO §九-2 的判据）。这三个数是这一批的主要产出，
// 算错了不会报错，只会让人以为选对了。
import assert from "node:assert/strict";
import test from "node:test";

import { candidateSpecs, classesOf, cssToLegado, elementSpec, selectorCandidates }
  from "./selector.js";

// ---- 极小的假 DOM：只实现被测到的那几个访问器（node 里没有 document）----

function fakeEl(tag, { id = "", cls = "", children = [], kids = [] } = {}) {
  const el = {
    tagName: tag.toUpperCase(),
    children: kids,
    parentElement: null,
    getAttribute(name) {
      if (name === "id") return id;
      if (name === "class") return cls;
      return null;
    },
    outerHTML: "<" + tag + ">" + (id || cls) + "</" + tag + ">",
  };
  for (const k of kids) k.parentElement = el;
  el.childNodes = children;      // 用不到，留着免得访问器缺失时炸出误导性的错
  return el;
}

function fakeDoc(selectorMap, links = 4) {
  return {
    documentElement: fakeEl("html"),
    querySelectorAll(sel) {
      if (sel === "a[href]") return new Array(links).fill(0).map(() => fakeEl("a"));
      return selectorMap[sel] || [];
    },
  };
}

test("CSS → Legado 的 @ 链写法", () => {
  assert.equal(cssToLegado("#grid li"), "id.grid@tag.li");
  assert.equal(cssToLegado(".row .card"), "class.row@class.card");
  assert.equal(cssToLegado("div"), "tag.div");
  assert.equal(cssToLegado(".a.b"), "class.a.b", "两个类名是同一个元素的复合选择器");
  // 表达不了的（子选择器 / 伪类 / 属性）老老实实返回空串：那条只能按 CSS 写
  assert.equal(cssToLegado("#x > li"), "");
  assert.equal(cssToLegado("#x > li:nth-of-type(2)"), "");
  assert.equal(cssToLegado("[property=og:name]"), "");
});

test("class 过滤：丢掉 Tailwind 变体与纯布局类", () => {
  assert.deepEqual(classesOf("cute-card  w-full hover:bg-gray-100 mt-2"), ["cute-card"]);
  assert.deepEqual(classesOf(""), []);
});

test("同层的一批排在前面，且是本仓口袋漫画那条形状", () => {
  const spec = { tag: "li", id: "", cls: "chapter-item",
                 ancestors: [{ tag: "ul", id: "chapter-grid", cls: "", nthOfType: 3 }] };
  const cs = candidateSpecs(spec);
  const sibling = cs.find((c) => c.css === "#chapter-grid li");
  assert.ok(sibling, JSON.stringify(cs));
  assert.equal(sibling.legado, "id.chapter-grid@tag.li");
  // 容器与「这一组」都在候选里（九-2 的「容器与字段成对给」）
  assert.ok(cs.some((c) => c.css === "#chapter-grid"), "容器那条也要有");
  // 结构路径带 nth-of-type（父就是那个 id 锚点）
  assert.ok(cs.some((c) => c.css === "#chapter-grid > li:nth-of-type(3)"), JSON.stringify(cs));
});

test("没 id 时退到类名；结构路径也就没有了（不给一堆没用的候选）", () => {
  const spec = { tag: "a", id: "", cls: "block", ancestors: [{ tag: "div", id: "", cls: "card", nthOfType: 1 }] };
  const cs = candidateSpecs(spec);
  assert.ok(cs.some((c) => c.css === ".card a"), JSON.stringify(cs));
  assert.ok(!cs.some((c) => c.kind === "path"), "没有 id 锚点就别硬造结构路径");
});

test("候选去重：同一个 css 只出现一次", () => {
  const spec = { tag: "li", id: "only", cls: "", ancestors: [] };
  const cs = candidateSpecs(spec);
  const seen = cs.map((c) => c.css);
  assert.equal(new Set(seen).size, seen.length, JSON.stringify(seen));
});

test("实测三个数：命中 / 去重 / 占链接比（1228 与 1198 就靠它分）", () => {
  const lis = [];
  for (let i = 0; i < 4; i += 1) {
    // 第 3 条与第 1 条内容相同 → 去重后少一个
    lis.push(fakeEl("li", { cls: i === 2 ? "dup-first" : "item-" + i }));
  }
  lis[2].outerHTML = lis[0].outerHTML;
  const ul = fakeEl("ul", { id: "grid", kids: lis });
  const doc = fakeDoc({ "#grid li": lis, "#grid": [ul] }, 8);
  const cs = selectorCandidates(doc, lis[0]);
  const sib = cs.find((c) => c.css === "#grid li");
  assert.equal(sib.hits, 4);
  assert.equal(sib.uniq, 3, "有一条重复，去重后应该是 3");
  assert.ok(Math.abs(sib.ratio - 0.5) < 1e-6, "4 个命中 / 8 个链接 → 0.5，实测 " + sib.ratio);
});

test("elementSpec：把 DOM 读成结构切片（含同标签兄弟序号）", () => {
  const li2 = fakeEl("li", { cls: "b" });
  const ul = fakeEl("ul", { id: "grid", kids: [fakeEl("li", { cls: "a" }), li2] });
  const spec = elementSpec(li2);
  assert.equal(spec.tag, "li");
  assert.equal(spec.ancestors[0].id, "grid");
  assert.equal(spec.ancestors[0].nthOfType, 2, "同标签兄弟里的序号");
});