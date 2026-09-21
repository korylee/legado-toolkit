// 调试抽屉的「层」判定（九-1）：**先定层，再写规则**。
//
// 要解决的问题（TODO §九）：用户拿到一个源，改半天选择器也取不到东西，而「取不到」在界面上
// 与「源坏了」长得一样。定层给的是**下一步动作**——L1 写选择器就能过；L2/L3 得换能跑 JS 的
// 通道；L4 要抓接口。**判据全部不需要执行 JS、也不需要解密**（我们能看的就是原文 + 源自己
// 的声明）。
//
// 两条硬规矩：
//   - **判官是源自己声明的能力**，不是页面统计：`ruleContent.webJs` 非空、URL 规则带
//     `webView` → 直接进 L2/L3 分支（AGENTS #13：能推导的别让用户填）。页面统计只是补充证据。
//   - **每条结论都要带证据行**：用户要能反驳它（「你说加密，哪一行看出来的」）。所以
//     返回值里的 `evidence[]` 是**数据**（为什么 + 原文片段 + 在原第几行），不是一句解释。
//
// 与「命中源码」那两块的关系：这里判的是**这一页 + 这个源**，结论只喂给横幅与「下一步动作」，
// 不参与任何判定（判定只由引擎给）。

/** 五档：名字 + 下一步动作（界面上直接照着说）。 */
export const LAYER_INFO = {
  L1: { name: "L1 静态直出", action: "直接写选择器" },
  L2: { name: "L2 JS 壳", action: "换能跑 JS 的通道 + webJs" },
  L3: { name: "L3 加密负载", action: "用 webJs 读页面里的全局对象（别自己实现解密）" },
  L4: { name: "L4 接口取数", action: "抓接口 + JSONPath" },
  L5: { name: "L5 需登录 / 被墙", action: "先解决登录态（浏览器 profile 预热或连 App）" },
};

//: 页面上的「加密负载」痕迹。每一条都必须**自证**是加密/混淆产物（别用"含 encrypt 字样"
//: 这类宽判据——正文里出现这些词的概率不为零）。
const PAGE_MARKERS = [
  ["长 base64 赋值", /var\s+\w+\s*=\s*['"][A-Za-z0-9+/=]{200,}['"]/g,
   "页面把一大段负载藏在一个变量里（base64）"],
  ["CryptoJS", /CryptoJS/g, "页面用 CryptoJS 自己解密"],
  ["_0x 混淆", /_0x[0-9a-f]{4,}/g, "脚本被混淆（_0x 变量名）"],
  ["decrypt(", /decrypt\s*\(/g, "页面里有解密调用"],
  ["xhr_mode", /xhr_mode/g, "正文走 XHR 拉取（DOM 里不会有）"],
  ["createObjectURL", /createObjectURL/g, "图片/数据是运行时生成的 blob（DOM 里没有地址）"],
];

//: 「页面读不出来的东西在全局对象里」的痕迹：webJs 脚本引用页面上的全局。
//: 与 L2 的分界就在这里——L2 只要求渲染（容器存在但为空），L3 得读**页面 JS 建出来的对象**。
const GLOBAL_READ_RE = /\b(params|window|globalThis|__NUXT__|__NEXT_DATA__)\b/;

//: 登录墙特征词。**只在源声明了登录方式时才算 L5**：光看词会把正常页面里的
//: 「请登录后评论」判成登录墙（与 `core/checker.is_login_wall` 同一立场）
const LOGIN_MARKERS = ["请登录", "立即登录", "登录后查看", "登录后可见", "请先登录"];

//: 源里带 webView 标记的判据（URL 规则的选项 `,{"webView":true}`）。
//: **与 `DebugService.kt` 的 `webViewPattern` 逐字相同**——同一个结论两个语言各判一次，
//: 漂了就分家（`tests/test_jvm_debug_contract.py` 有一条逐字比对的测试）
export const WEBVIEW_RE = /"?webView"?\s*:\s*(?:true|1|"true")/i;

/** 页面节点统计（原在抽屉里，提出来给更多地方用）。 */
export function pageStats(html) {
  const text = String(html || "");
  if (!text) return null;
  const low = text.toLowerCase();
  return {
    links: (low.match(/<a[\s>][^>]*href=/g) || []).length,
    images: (low.match(/<img[\s\/>]/g) || []).length,
    // 只有「有值的 src」才算真能取到的图：`<img src="">` 是 JS 注入留下的占位
    imagesWithSrc: (text.match(/<img[\s>][^>]*src=["'](?!["'])/gi) || []).length,
    textLen: text.replace(/<[^>]+>/g, "").replace(/\s+/g, "").length,
  };
}

/** 这一页上有没有「你要的那个东西」（门槛取粗：它只用来挡住「怎么改选择器都取不到」）。 */
export function hasWanted(stats, want) {
  if (!stats || !want) return null;
  if (want.kind === "media") return stats.imagesWithSrc > 1;   // >1：排除只有 logo 的情况
  if (want.kind === "link") return stats.links > 0;
  if (want.kind === "list") return stats.links > 0 || stats.images > 0;
  return stats.textLen > 200;
}

/** 第几行（1 起）：证据行要能指回去。 */
function lineOf(text, index) {
  let line = 1;
  for (let i = 0; i < index && i < text.length; i += 1) {
    if (text[i] === "\n") line += 1;
  }
  return line;
}

/** 按每条规则抓证据（最多 limit 条，每条带原文片段与行号）。 */
function gather(text, patterns, limit = 3) {
  const out = [];
  for (const [why, re, note] of patterns) {
    for (const m of text.matchAll(re)) {
      out.push({ why, note, snippet: m[0].slice(0, 120).replace(/\s+/g, " "),
                 line: lineOf(text, m.index) });
      if (out.length >= limit) break;
    }
    if (out.length >= limit) break;
  }
  return out;
}

/**
 * 源里所有字符串值（按出现顺序）。
 *
 * **不要用 `JSON.stringify(源)` 去测**：转义后的 `\"webView\"` 会让「带引号的词」
 * 这类判据失效（实测踩过），而且命中时也看不出是哪条规则命中的。
 */
function sourceStrings(source) {
  const out = [];
  const walk = (v) => {
    if (typeof v === "string") out.push(v);
    else if (Array.isArray(v)) v.forEach(walk);
    else if (v && typeof v === "object") Object.values(v).forEach(walk);
  };
  walk(source);
  return out;
}

/**
 * 段 → 这条段自己的 URL 字段（`webView` 标记挂在这些规则上）。
 * **判层要按段来**：`ruleContent.webJs` 说的是**正文那一段**，把它算到搜索页头上
 * 会把「搜索页明明是静态直出」的源判成 L2/L3（实测口袋漫画就是这样：搜索页结果
 * 在原文里，webJs 只服务正文）。
 */
const URL_FIELDS_BY_STEP = {
  search: ["searchUrl"],
  explore: ["exploreUrl"],
  bookUrl: ["ruleSearch.bookUrl", "ruleBookInfo.tocUrl"],
  toc: ["ruleToc.tocUrl", "ruleToc.chapterUrl"],
  content: ["ruleContent.nextContentUrl"],
};

function fieldOf(src, path) {
  let cur = src;
  for (const k of path.split(".")) {
    if (!cur || typeof cur !== "object") return "";
    cur = cur[k];
  }
  return typeof cur === "string" ? cur : "";
}

/**
 * 源自己声明的证据（不看页面）。**`step` 给了就只算这一段自己的**：不给（判不出段）
 * 时按「整条源」算——那时宁可多给一条证据，也别把一段的结论按到另一段上。
 */
function sourceEvidence(source, step) {
  const src = source || {};
  const out = [];
  const rc = src.ruleContent || {};
  const forContent = !step || step === "content";
  const webJs = forContent ? String(rc.webJs || "") : "";
  if (webJs.trim()) {
    out.push({ why: "源声明了 webJs", note: "取值要在页面里执行 JS（DOM 里没有现成的值）",
               snippet: webJs.trim().slice(0, 120), line: 0 });
    if (GLOBAL_READ_RE.test(webJs)) {
      out.push({ why: "webJs 读的是页面全局对象",
                 note: "值在页面脚本建出来的对象里（渲染完才存在），不是选择器能取到的",
                 snippet: (webJs.match(GLOBAL_READ_RE) || [""])[0], line: 0 });
    }
  }
  if (forContent && String(rc.content || "").trim().startsWith("@js:")) {
    out.push({ why: "正文规则是 @js:", note: "正文不是选择器算出来的，是脚本算出来的",
               snippet: String(rc.content).trim().slice(0, 120), line: 0 });
  }
  // webView 标记挂在**某一条 URL 规则**上：按段挑字段，别拿 toc 上的标记去说搜索页
  const urlFields = step ? (URL_FIELDS_BY_STEP[step] || []) : null;
  const webViewRule = (urlFields
    ? urlFields.map((f) => fieldOf(src, f)).find((s) => WEBVIEW_RE.test(s))
    : sourceStrings(src).find((s) => WEBVIEW_RE.test(s)));
  if (webViewRule) {
    const at = webViewRule.search(WEBVIEW_RE);
    out.push({ why: "URL 规则带 webView 标记",
               note: "这条源自己声明要交给 WebView 渲染",
               snippet: webViewRule.slice(Math.max(0, at - 20), at + 30).trim(), line: 0 });
  }
  return out;
}

const hasLogin = (source) => {
  const src = source || {};
  return !!String(src.loginUrl || "").trim() || !!src.enabledCookieJar;
};

/**
 * 判层。返回 `{ layer, info, evidence, page }`：
 *   - `layer`：`"L1"`–`"L5"`；**`""` = 判不了**（没有页面统计、也没有源声明——不硬猜）
 *   - `evidence[]`：`{why, note, snippet, line}`（line=0 表示来自源声明，不在页面上）
 *   - `page`：`{stats, hasWanted}`，方便界面顺带显示「这一页有没有你要的东西」
 *
 * `want` 是这一步要拿到什么（`{kind: "list"|"link"|"text"|"media"}`）；不给就**不判 L1**
 * （L1 的定义就是「原文里就有目标」，没有目标就无从说起）。
 * `step` 是这一步的段名：**源声明的能力按段归属**（`ruleContent.webJs` 只说正文那段，
 * URL 上的 `webView` 只算它所属的那条规则），不给就按整条源算。
 */
export function classifyLayer(html, source, opts = {}) {
  const text = String(html || "");
  const want = opts.want || null;
  const step = String(opts.step || "");
  const stats = pageStats(text);
  const wanted = hasWanted(stats, want);
  const srcEv = sourceEvidence(source, step);
  const page = { stats, hasWanted: wanted };

  // L5：登录墙。**要源自己声明了登录方式**才算——否则「请登录」在正常页面上太常见
  if (text && hasLogin(source)) {
    const hit = LOGIN_MARKERS.find((w) => text.includes(w));
    if (hit) {
      return { layer: "L5", info: LAYER_INFO.L5, page,
               evidence: [{ why: "页面出现登录词且源声明了登录方式",
                            note: "先解决登录态再看规则", snippet: hit, line: lineOf(text, text.indexOf(hit)) }],
      };
    }
  }

  // L3：加密 / 混淆 / 数据在全局对象里
  const enc = gather(text, PAGE_MARKERS);
  const globalRead = srcEv.filter((e) => e.why === "webJs 读的是页面全局对象");
  if (enc.length || globalRead.length) {
    return { layer: "L3", info: LAYER_INFO.L3, page, evidence: [...enc, ...srcEv] };
  }

  // L4：原文里没有列表，但有接口调用的痕迹
  const apiMarks = gather(text, [
    ["fetch(", /\bfetch\s*\(/g, "页面用 fetch 取数据（渲染后才进 DOM）"],
    ["axios", /\baxios\b/g, "页面用 axios 取数据"],
    ["/api/", /\/api\//g, "页面里有接口路径"],
  ], 2);
  if (apiMarks.length && wanted === false) {
    return { layer: "L4", info: LAYER_INFO.L4, page, evidence: apiMarks };
  }

  // L2：源声明要渲染（webJs / webView），但没看到加密痕迹
  const declared = srcEv.filter((e) => e.why !== "正文规则是 @js:");
  if (declared.length) {
    return { layer: "L2", info: LAYER_INFO.L2, page, evidence: declared };
  }

  // L1：原文里就有目标
  if (wanted === true) {
    return { layer: "L1", info: LAYER_INFO.L1, page, evidence: [] };
  }

  // 判不了就说判不了（没有页面 / 没有目标 / 三者都不成立）——**不硬猜**
  const why = !text ? "这一步没有页面 HTML（只有源声明可用）"
    : !want ? "这一步没有「要什么」的定义，判不了 L1"
      : "原文里没有目标，也没有可判的痕迹";
  return { layer: "", info: null, page, evidence: [], unsure: why };
}