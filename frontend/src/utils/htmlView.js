// 抽屉里那两块源码的**显示层**处理：折行缩进 + 实体展开。
//
// 为什么有它：页面原文与引擎带回来的命中 DOM 都是**整行的 HTML**（jsoup 序列化出来就是
// 一行），再加上 `&nbsp;` / `&#160;` 这类实体，读起来是一堵墙。这里只做**显示**——
// 抽屉里那条「复制原文」照旧给原文：写规则/写正则要用的是原文，看懂结构要的是这份。
//
// 三条有意为之的边界：
//   - **script / style / 注释的内容原样留着**：里面本来就有 `<` `>`，按标签切会散架；
//     而且浏览器**不对它们的内容做实体解码**（`&amp;` 在 script 里就是四个字符），所以
//     解码只在「文本节点」上做，不是先整篇解一遍。
//   - **只动空白**：文本节点里的连续空白折成一个空格、去掉首尾；标签与属性一个字不重排
//     （大小写、引号、属性顺序都照原样）。原文长什么样，切回「原文」就能看到。
//   - **`<` 后面不是字母就不是标签**（`1 < 2 > 0` 当文本），否则正文里的数学式会被切散。

/** HTML 具名实体：只收页面上真会出现的那些（其余照原样留着，别猜）。 */
export const NAMED_ENTITIES = {
  amp: "&", lt: "<", gt: ">", quot: '"', apos: "'", nbsp: "\u00a0",
  copy: "\u00a9", reg: "\u00ae", trade: "\u2122", deg: "\u00b0", plusmn: "\u00b1",
  times: "\u00d7", divide: "\u00f7", frac12: "\u00bd", sup2: "\u00b2", sup3: "\u00b3",
  micro: "\u00b5", hellip: "\u2026", mdash: "\u2014", ndash: "\u2013",
  minus: "\u2212", middot: "\u00b7", bull: "\u2022", lsquo: "\u2018",
  rsquo: "\u2019", ldquo: "\u201c", rdquo: "\u201d", laquo: "\u00ab",
  raquo: "\u00bb", sect: "\u00a7", para: "\u00b6", dagger: "\u2020",
  permil: "\u2030", prime: "\u2032", Prime: "\u2033", euro: "\u20ac",
  pound: "\u00a3", yen: "\u00a5", cent: "\u00a2", larr: "\u2190",
  rarr: "\u2192", uarr: "\u2191", darr: "\u2193", harr: "\u2194",
  infin: "\u221e", ne: "\u2260", le: "\u2264", ge: "\u2265", sim: "\u223c",
  alpha: "\u03b1", beta: "\u03b2", gamma: "\u03b3", delta: "\u03b4",
  lambda: "\u03bb", mu: "\u03bc", pi: "\u03c0", sigma: "\u03c3",
  omega: "\u03c9", Omega: "\u03a9",
};

//: 不是标签名的 `<...>` 当文本：`<` 后面必须紧跟字母（或 `/字母`）才算标签开始。
const TAG_START = /^<\s*\/?\s*[a-zA-Z][\w:-]*/;
//: 实体：具名 / 十进制 `&#160;` / 十六进制 `&#xA0;`
const ENTITY_RE = /&(#x[0-9a-fA-F]+|#[0-9]+|[a-zA-Z][a-zA-Z0-9]{1,31});/g;
//: 自闭合 / 空元素：不增加缩进层级
const VOID_TAGS = new Set(["area", "base", "br", "col", "embed", "hr", "img",
  "input", "link", "meta", "param", "source", "track", "wbr"]);
//: 内容原样留着的元素（浏览器也不对它们的内容做实体解码，`pre` 里空白还有意义）
const RAW_TEXT_TAGS = new Set(["script", "style", "textarea", "pre"]);

/**
 * 展开 HTML 实体：`&nbsp;` → U+00A0、`&#160;` / `&#xA0;` 同、`&amp;` → `&`。
 * 认不出的（表里没有的具名实体、越界的数字引用）**原样保留**——猜错比不猜贵得多。
 */
export function decodeEntities(text) {
  return String(text == null ? "" : text).replace(ENTITY_RE, (whole, body) => {
    if (body[0] === "#") {
      const hex = body[1] === "x" || body[1] === "X";
      const code = hex ? parseInt(body.slice(2), 16) : parseInt(body.slice(1), 10);
      if (!Number.isFinite(code) || code <= 0 || code > 0x10ffff) return whole;
      try {
        return String.fromCodePoint(code);
      } catch (e) {
        return whole;
      }
    }
    const hit = Object.prototype.hasOwnProperty.call(NAMED_ENTITIES, body)
      ? NAMED_ENTITIES[body] : null;
    return hit === null ? whole : hit;
  });
}

/** 标签里那个 `>`（引号里的不算）。找不到返回 -1。 */
function tagEnd(text, start) {
  let quote = "";
  for (let i = start + 1; i < text.length; i += 1) {
    const ch = text[i];
    if (quote) {
      if (ch === quote) quote = "";
      continue;
    }
    if (ch === '"' || ch === "'") { quote = ch; continue; }
    if (ch === ">") return i;
  }
  return -1;
}

/** 标签名（小写）；不是标签返回空串。 */
function tagName(raw) {
  const m = raw.match(/^<\s*\/?\s*([a-zA-Z][\w:-]*)/);
  return m ? m[1].toLowerCase() : "";
}

/** 同名的结束标签位置（大小写不敏感）；没有返回 -1。 */
function closeTagStart(text, name, from) {
  const low = text.toLowerCase();
  const needle = "</" + name;
  let at = low.indexOf(needle, from);
  while (at >= 0) {
    const after = low[at + needle.length];
    if (after === ">" || after === " " || after === "\t" || after === "\n"
        || after === "/") {
      return at;
    }
    at = low.indexOf(needle, at + needle.length);
  }
  return -1;
}

/**
 * 把 HTML 折行缩进，便于阅读。返回的文本**只多了换行与缩进**（外加文本节点里的空白
 * 折叠与实体展开，`decodeEntities: false` 可关掉后者）。
 *
 * 三条口径：
 *   - **只有文本的元素挤在一行**（`<a href="/x">甲</a>`）——每个标签都换行反而更难读
 *   - 层级只在真正进入元素时 +1：空元素（`<br>`）、声明（`<!DOCTYPE>`）、注释都不算
 *   - `indentSize` 是每层的空格数（默认 2）。不用制表符：复制出去贴到别处，宽度不一样
 */
export function formatHtml(html, options = {}) {
  const indentUnit = " ".repeat(Math.max(0, Number(options.indentSize) || 2));
  const decode = options.decodeEntities !== false;
  const text = html == null ? "" : String(html);
  if (!text.trim()) return "";
  if (text.indexOf("<") < 0) return decode ? decodeEntities(text.trim()) : text.trim();

  const out = [];
  let depth = 0;
  let textStart = 0;      // 还没推出去的文本起点
  let search = 0;         // 找下一个 `<` 的位置
  const pushLine = (s) => {
    if (s !== "") out.push(indentUnit.repeat(depth) + s);
  };
  const textOf = (raw) => (decode ? decodeEntities(raw) : raw)
    .replace(/\s+/g, " ").trim();
  const pushText = (raw) => {
    const t = textOf(raw);
    if (t) pushLine(t);
  };

  while (search < text.length) {
    const lt = text.indexOf("<", search);
    if (lt < 0) {
      pushText(text.slice(textStart));
      break;
    }

    // 注释：整块一行，不进层级
    if (text.startsWith("<!--", lt)) {
      const end = text.indexOf("-->", lt + 4);
      const stop = end < 0 ? text.length : end + 3;
      pushText(text.slice(textStart, lt));
      pushLine(text.slice(lt, stop).trim());
      search = stop;
      textStart = stop;
      continue;
    }

    const gt = tagEnd(text, lt);
    const raw = gt < 0 ? "" : text.slice(lt, gt + 1);
    const isDecl = raw.startsWith("<!");
    // 不是 token：这个 `<` 属于文本，只把「找 `<` 的位置」往后挪，**不动 textStart**
    if (gt < 0 || (!isDecl && !TAG_START.test(raw))) {
      if (gt < 0) {
        pushText(text.slice(textStart));
        break;
      }
      search = lt + 1;
      continue;
    }

    pushText(text.slice(textStart, lt));
    if (isDecl) {
      pushLine(raw);
      search = gt + 1;
      textStart = gt + 1;
      continue;
    }

    const name = tagName(raw);
    const closing = /^<\s*\//.test(raw);
    const selfClosing = /\/\s*>$/.test(raw) || VOID_TAGS.has(name);
    if (closing) depth = Math.max(0, depth - 1);

    if (closing || selfClosing) {
      pushLine(raw);
      search = gt + 1;
      textStart = gt + 1;
      continue;
    }

    if (RAW_TEXT_TAGS.has(name)) {
      pushLine(raw);
      const closeAt = closeTagStart(text, name, gt + 1);
      const stop = closeAt < 0 ? text.length : closeAt;
      const inner = text.slice(gt + 1, stop);
      if (name === "script" || name === "style") {
        // 逐行 trim 后跟着缩进（内容是代码，行内空白不动）；也不做实体展开
        inner.split(/\r?\n/).forEach((line) => pushLine(line.trim()));
      } else {
        pushLine(inner);          // pre / textarea：空白有意义，整段留着
      }
      search = stop;              // 结束标签交给下一轮（层级还没 +1）
      textStart = stop;
      continue;
    }

    // 只有文本的元素：挤在一行（每个标签都换行反而更难读）
    const closeAt = closeTagStart(text, name, gt + 1);
    if (closeAt >= 0) {
      const inner = text.slice(gt + 1, closeAt);
      const closeGt = text.indexOf(">", closeAt);
      if (inner.indexOf("<") < 0 && closeGt >= 0) {
        pushLine(raw + textOf(inner) + text.slice(closeAt, closeGt + 1));
        search = closeGt + 1;
        textStart = closeGt + 1;
        continue;
      }
    }
    pushLine(raw);
    depth += 1;
    search = gt + 1;
    textStart = gt + 1;
  }
  return out.join("\n");
}