// 从**我们补抓的那份 HTML** 里猜「这一步该写什么规则」。
//
// 纯函数、只读：给一份 html + 「这一步要什么」，返回一组候选
// `{rule, label, count, samples}`。填进表单由调用方决定（本模块不写任何东西）。
//
// 三条设计约束：
//   1. **用的是我们抓的 HTML，不是浏览器里的 DOM**。App 走的是自己的 WebView，
//      我们抓的是原始响应——两者可能不同（JS 注入的内容只在前者里）。所以候选取自
//      原始 HTML 才与「App 会看到什么」一致，也才和本地回放器能跑的东西一致。
//   2. **每个候选都给出「它选出了几条 + 前几个值」**。不给样本的候选等于让用户再猜一次；
//      而取到 0 条的候选要能一眼看出来。
//   3. 排序是**启发式**（各 family 函数里写清了怎么排），只影响顺序、不影响内容——
//      排错的代价是"多看一眼"，不是"给错规则"。
//
// 这一层是**确定性**的：不调模型、不发请求，只解析+查询。

//: 步骤 → 表单里那个字段的路径。填规则时用（与 `RuleDebugDrawer` 的 STEP_RULE_TAB 同源）。
export const FIELD_OF_STEP = {
  search: "ruleSearch.bookList",
  bookUrl: "ruleSearch.bookUrl",
  toc: "ruleToc.chapterList",
  content: "ruleContent.content",
};

function parse(html) {
  try {
    return new DOMParser().parseFromString(String(html || ""), "text/html");
  } catch (e) {
    return null;
  }
}

function firstClass(el) {
  const cls = (el && el.getAttribute && el.getAttribute("class")) || "";
  // 只取第一个类名：Legado 的 class 选择器是**单类**匹配，多类拼一起会选不中
  const parts = String(cls).trim().split(/\s+/).filter(Boolean);
  return parts[0] || "";
}

//: 链接像不像「详情页 / 章节页」：含数字（书号/章节号）、或 .html、或路径够深。
//: 这条只用来**排序**——真正的判据是「取到几条 + 样本长什么样」，由用户看
function looksLikeTarget(href) {
  const h = String(href || "");
  if (!h || h.startsWith("#") || h.startsWith("javascript:")) return false;
  return /\d/.test(h) || /\.html?($|\?)/i.test(h) || h.split("/").filter(Boolean).length >= 2;
}

//: 同一条规则可能来自多个元素（嵌套/多块），按 rule 去重
function uniqByRule(list) {
  const seen = new Set();
  const out = [];
  for (const c of list) {
    if (seen.has(c.rule)) continue;
    seen.add(c.rule);
    out.push(c);
  }
  return out;
}

function linkCandidates(doc, limit) {
  const anchors = [...doc.querySelectorAll("a[href]")];
  const groups = new Map();      // rule → 命中元素
  const add = (rule, el) => {
    if (!rule) return;
    const arr = groups.get(rule) || [];
    arr.push(el);
    groups.set(rule, arr);
  };
  for (const a of anchors) {
    const own = firstClass(a);
    if (own) add(`.${own}@href`, a);
    const parent = firstClass(a.parentElement);
    // 列表项的类 + 项内第一个 a 的 href：Legado 里最常见的详情/章节链接写法
    if (parent) add(`.${parent}@tag.a@href`, a);
  }
  // 兜底：全部链接（条数会很夸张，但能让用户看出"页面里到底有多少链接"）
  add("tag.a@href", anchors[0]);
  const out = [];
  for (const [rule, els] of groups) {
    const hrefs = els.map((a) => a.getAttribute("href") || "").filter(Boolean);
    const good = hrefs.filter(looksLikeTarget).length;
    const samples = hrefs.filter(looksLikeTarget).slice(0, 3);
    out.push({
      rule, kind: "link", count: hrefs.length, good,
      samples: samples.length ? samples : hrefs.slice(0, 3),
    });
  }
  return out.sort((a, b) => (b.good - a.good) || (a.count - b.count)).slice(0, limit);
}

function mediaCandidates(doc, limit) {
  const imgs = [...doc.querySelectorAll("img")];
  const groups = new Map();
  const add = (rule, el, attr) => {
    const v = el.getAttribute(attr);
    if (!v || !String(v).trim()) return;      // `<img src="">` 是 JS 注入留下的空占位
    const arr = groups.get(rule) || [];
    arr.push(String(v).trim());
    groups.set(rule, arr);
  };
  for (const img of imgs) {
    for (const attr of ["src", "data-original", "data-src"]) {
      const own = firstClass(img);
      if (own) add(`.${own}@${attr}`, img, attr);
      const parent = firstClass(img.parentElement);
      if (parent) add(`.${parent}@tag.img@${attr}`, img, attr);
      add(`tag.img@${attr}`, img, attr);
    }
  }
  return [...groups.entries()]
    .map(([rule, vals]) => ({
      rule, kind: "media", count: vals.length, good: vals.length,
      samples: vals.slice(0, 3),
    }))
    .sort((a, b) => b.count - a.count)
    .slice(0, limit);
}

function textCandidates(doc, limit) {
  // 正文块 = 纯文字最多、且标签里有 class 的那些。按文字长度排序取前几个——
  // 这是"正文提取"最经典的做法，也让用户一眼认出哪块是正文
  const out = [];
  for (const el of doc.querySelectorAll("div, article, section, td, p")) {
    const cls = firstClass(el);
    if (!cls) continue;
    const text = String(el.textContent || "").replace(/\s+/g, "");
    if (text.length < 200) continue;
    out.push({
      rule: `.${cls}@text`, kind: "text", count: text.length, good: text.length,
      samples: [String(el.textContent || "").replace(/\s+/g, " ").trim().slice(0, 60)],
    });
  }
  // 同一个 class 会出现多次（嵌套/多块），按规则去重后取文字最多的几个
  return uniqByRule(out.sort((a, b) => b.count - a.count)).slice(0, limit);
}

function listCandidates(doc, limit) {
  // 书目列表 = 「同一个父节点下、同标签同类名的一批兄弟」。规则写成 `.父类 子标签`
  const groups = new Map();
  for (const parent of doc.querySelectorAll("ul, ol, div, section")) {
    const pcls = firstClass(parent);
    if (!pcls) continue;
    const byChild = new Map();
    for (const el of parent.children) {
      const key = el.tagName.toLowerCase() + "|" + firstClass(el);
      byChild.set(key, (byChild.get(key) || 0) + 1);
    }
    for (const [key, n] of byChild) {
      if (n < 3) continue;                     // 少于 3 条不成列表
      const [tag, cls] = key.split("|");
      const rule = cls ? `.${pcls} .${cls}` : `.${pcls} ${tag}`;
      groups.set(rule, Math.max(groups.get(rule) || 0, n));
    }
  }
  return [...groups.entries()]
    .map(([rule, count]) => ({ rule, kind: "list", count, good: count, samples: [] }))
    .sort((a, b) => b.count - a.count)
    .slice(0, limit);
}

/**
 * 找候选。
 *
 * `kind` 来自调用方（`RuleDebugDrawer` 的 `want.kind`）：link / media / text / list。
 * 返回最多 `limit` 条，每条都带 `count`（选到几条）与 `samples`（前几个值）。
 * 页面解析失败或没有候选时返回空数组——**空数组是个结论**（页面上确实没有），
 * 由调用方连同诊断一起展示，不要在这里编一个默认规则出来。
 */
export function findCandidates(html, kind, limit = 6) {
  const doc = parse(html);
  if (!doc || !kind) return [];
  const run = {
    link: linkCandidates, media: mediaCandidates,
    text: textCandidates, list: listCandidates,
  }[kind];
  if (!run) return [];
  try {
    return run(doc, limit);
  } catch (e) {
    // 脏 HTML 让某一族候选探不出来时，不该把整块面板弄空——诊断那边还有别的线索
    console.error("[candidates] 解析失败:", e);
    return [];
  }
}
