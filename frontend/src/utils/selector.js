// 点选式选择器（九-2a）：**给一个元素，给出可用的选择器候选**。
//
// 为什么要有它：候选面板是**算法猜**出来的（`ruleCandidates.js` 按自己的假设扫 DOM），
// 猜不出（页面结构不合假设、容器空、L2–L4）就没有第二条路。点选不需要猜：
// 用户在渲染视图里点一下，我们把这个元素的「几种写法」摆出来，并**当场算出准不准**。
//
// 四类候选（每类的用途不同，别只给一类）：
//   1. **同层兄弟集合**——点一个章节 `<li>`，`bookList`/`chapterList` 要的其实是**这一组**
//      （容器选择器 + 条目标签）。这是最常用的一条，所以排在前面。
//   2. **元素自己**——`#id` 唯一时最优先；否则类链（`.a.b`）；再不行结构路径。
//   3. **父容器**——想写 `bookList` 容器本身时用。
//   4. **结构路径**兜底——`#id > tag:nth-of-type(n)`，任何时候都指得准，但页面改版就废。
//
// 每条都给**两份写法**：`css`（用来在页面上数命中）与 `legado`（Legado 的 `@` 链写法，
// `id.x@tag.y`；某些 CSS 表达不了时为空——那种就只给 CSS，Legado 也认纯 CSS）。
// 还有三个实测口径：`hits`（命中几个节点）、`uniq`（去掉重复后几个）、`ratio`（占页面链接比）
// ——**「命中 1228」与「命中 1198」在界面上都只是个数字**，能分开它们的是去重与占比。

//: 空元素 / 不能当选择器骨架的标签
const SKIP_TAGS = new Set(["html", "body", "head", "script", "style", "title", "meta", "link"]);

/** 元素上「像选择器」的 class（丢掉 Tailwind 变体与纯布局类——含冒号的会破坏 CSS 语法）。 */
export function classesOf(cls) {
  return String(cls || "").split(/\s+/)
    .filter((c) => c && !c.includes(":") && !/^(w|h|mt|mb|ml|mr|pt|pb|pl|pr|px|py|mx|my)-/.test(c))
    .slice(0, 3);
}

const isId = (s) => !!s && /^[A-Za-z][\w-]*$/.test(s);

/** 元素自身的选择器片段：优先 id，其次第一个类名，最后是标签名。 */
export function selfCss(spec) {
  if (!spec || !spec.tag) return "";
  if (isId(spec.id)) return "#" + spec.id;
  const cls = classesOf(spec.cls);
  if (cls.length) return "." + cls[0];
  return SKIP_TAGS.has(spec.tag) ? "" : spec.tag;
}

/** CSS → Legado 的 `@` 链写法（表达不了就返回空串）。 */
export function cssToLegado(css) {
  const text = String(css || "").trim();
  if (!text || /[>~+\[\]:*]/.test(text.replace(/:[a-z-]+\(/g, ""))) return "";
  const parts = text.split(/\s+/).filter(Boolean);
  const out = [];
  for (const p of parts) {
    if (p.startsWith("#")) out.push("id." + p.slice(1));
    else if (p.startsWith(".")) out.push("class." + p.slice(1));
    else if (/^[a-zA-Z][\w-]*$/.test(p)) out.push("tag." + p);
    else return "";
  }
  return out.join("@");
}

/** 一个候选的实测：命中数 / 去重数 / 占页面链接比。 */
function measure(doc, css) {
  let nodes = [];
  try {
    nodes = [...doc.querySelectorAll(css)];
  } catch (e) {
    return null;      // 选择器不合法（理论上不会，兜一手）
  }
  if (!nodes.length) return { hits: 0, uniq: 0, ratio: 0 };
  const seen = new Set();
  for (const n of nodes) {
    // 去重口径：元素自身的 HTML。重复项（同一章被列两遍）在这一列会露出来
    seen.add(String(n.outerHTML || "").slice(0, 400));
  }
  const links = doc.querySelectorAll("a[href]").length || 1;
  return {
    hits: nodes.length,
    uniq: seen.size,
    // 占页面链接比只对「链接类」候选有意义（列表项常常就是一堆 <a>）
    ratio: nodes.length / links,
  };
}

/**
 * 元素的「结构切片」：**纯数据**，给 `candidateSpecs` 用（这样候选逻辑能在 node 里测，
 * 不需要 DOM）。
 *
 * `ancestors`：从近到远，每层 `{tag, id, cls, nthOfType}`（nthOfType = 同标签兄弟里的序号）。
 */
export function elementSpec(el) {
  if (!el || !el.tagName) return null;
  const specOf = (node) => ({
    tag: node.tagName.toLowerCase(),
    id: (node.getAttribute && node.getAttribute("id")) || "",
    cls: (node.getAttribute && node.getAttribute("class")) || "",
  });
  const me = specOf(el);
  const ancestors = [];
  let cur = el.parentElement;
  while (cur && ancestors.length < 6) {
    const s = specOf(cur);
    const sibs = [...(cur.children || [])].filter((k) => k.tagName === el.tagName);
    const nth = sibs.indexOf(el) + 1;
    ancestors.push({ ...s, nthOfType: nth > 0 ? nth : 0 });
    cur = cur.parentElement;
  }
  return { ...me, ancestors };
}

/**
 * 候选的**生成逻辑（纯函数）**：结构切片 → `[{css, legado, why, kind}]`，按「该先试哪条」排序。
 *
 * 四类 `kind`（用途不同，别只给一类）：`siblings`（同层的一批，`bookList`/`chapterList` 要的
 * 就是它）、`self`、`container`、`path`（结构路径兜底）。
 */
export function candidateSpecs(spec, opts = {}) {
  const limit = Number(opts.limit) || 6;
  if (!spec || !spec.tag) return [];
  const out = [];
  const push = (css, why, kind) => {
    if (!css || out.some((c) => c.css === css)) return;
    out.push({ css, legado: "", why, kind });
  };

  const parent = spec.ancestors[0] || null;
  const pSel = parent ? selfCss(parent) : "";
  // 1) 同层兄弟集合（列表项要的就是这一组）
  if (parent && pSel && !SKIP_TAGS.has(spec.tag)) {
    push(pSel + " " + spec.tag, "同层的一批（列表项要的是这一组）", "siblings");
    push(pSel + " > " + spec.tag, "同层的一批（限定直接子元素）", "siblings");
  }
  // 2) 元素自己
  const own = selfCss(spec);
  if (own) push(own, own.startsWith("#") ? "这个元素（id 唯一）" : "这个元素（类名）", "self");
  const cls = classesOf(spec.cls);
  if (cls.length >= 2) {
    push("." + cls.slice(0, 2).join("."), "这个元素（两个类名一起，更不容易误中）", "self");
  }
  // 3) 父容器
  if (pSel) push(pSel, "它的容器（写 bookList / chapterList 时常用）", "container");
  // 4) 结构路径兜底：最近的带 id 的祖先 + tag:nth-of-type
  const anchor = spec.ancestors.find((a) => isId(a.id));
  if (anchor) {
    const nth = parent && parent.id === anchor.id ? parent.nthOfType : 0;
    push("#" + anchor.id + " > " + spec.tag + (nth > 0 ? ":nth-of-type(" + nth + ")" : ""),
         "结构路径（页面一改版就废，兜底用）", "path");
  }

  for (const c of out) c.legado = cssToLegado(c.css);

  // 排序：唯一命中优先，其次「一组兄弟」，再次容器，最后结构路径。同分按命中数少的在前
  const rank = { self: 0, siblings: 1, container: 2, path: 3 };
  out.sort((a, b) => (rank[a.kind] - rank[b.kind]));
  return out.slice(0, limit);
}

/**
 * 给界面的入口：候选 + **实测**（命中 N / 去重 K / 占页面链接比）。
 *
 * 「命中 1228」与「命中 1198」在界面上都只是个数字，能分开它们的是去重与占比——
 * 所以三个数一起给，`uniq` 明显小于 `hits` 就说明**同一项被列了多遍**。
 */
export function selectorCandidates(doc, el, opts = {}) {
  const spec = elementSpec(el);
  if (!spec) return [];
  return candidateSpecs(spec, opts).map((c) => {
    const m = measure(doc, c.css);
    return { ...c, hits: m ? m.hits : 0, uniq: m ? m.uniq : 0, ratio: m ? m.ratio : 0 };
  });
}

/**
 * 一条取值规则的**元素部分**（给「实时高亮」用）：把 `@` 链换成 CSS，末段是裸词就丢掉。
 *
 * 为什么要丢末段：取值规则的末段是**取值动作或属性名**（`class.a@tag.b@href`），
 * 当选择器用取不到东西（AGENTS #21）。这里**不维护那张词表**——只按「裸词」（不含
 * `.`/`#`/`[`/空格）试，并**要求去掉之后真的能选中**；两档都选不中就返回 null
 * （宁可不画，也不要画一个错的集合）。
 */
export function previewCss(doc, rule) {
  const toCss = (r) => String(r || "")
    .replace(/(^|@)class\./g, "$1.")
    .replace(/(^|@)id\./g, "$1#")
    .replace(/(^|@)tag\./g, "$1")
    .replace(/@/g, " ").trim();
  const text = String(rule || "").trim();
  if (!text) return null;
  const tries = [text];
  const segs = text.split("@");
  const last = (segs[segs.length - 1] || "").trim();
  if (segs.length > 1 && /^[A-Za-z][\w-]*$/.test(last)) {
    tries.push(segs.slice(0, -1).join("@"));
  }
  for (const r of tries) {
    const css = toCss(r);
    if (!css) continue;
    const m = measure(doc, css);
    if (m && m.hits > 0) return { css, ...m };
  }
  return null;
}

/**
 * 从**取到的值**这一侧算实测（候选面板用）。与 `measure` 同一套口径，只是数的是值：
 * `uniq` 明显小于 `hits` 就说明**同一项被列了多遍**（实测那类「重复 30 条最新章节」
 * 就是这么露出来的），`ratio` 是它占页面链接的比例。
 */
export function measureValues(doc, values) {
  const list = (values || []).filter((v) => String(v || "").trim());
  const uniq = new Set(list.map((v) => String(v))).size;
  const links = (doc && doc.querySelectorAll("a[href]").length) || 1;
  return { hits: list.length, uniq, ratio: list.length / links };
}
