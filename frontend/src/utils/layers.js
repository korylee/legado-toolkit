// 调试抽屉的「层」：**页面那半来自后端，源声明这半在这里**（十-5 编排时订正过）。
//
// 为什么要改：层的判据原来整份长在这个文件里（页面痕迹 + 门槛 + 源声明），而生成链
// （Python，`services/add_source`）也要判「这一页要不要改用引擎取」——两条路各写一份必然漂，
// 还得配逐词比对。现在：
//
//   - **页面侧**（L1 有没有目标 / L2 容器在但空 / L3 加密痕迹 / L4 接口痕迹 + 统计 + 登录词）
//     → `core/page_layer.py` 一份，判完由 `core/jvm_debug` 挂到 `pages[].page_layer`；
//     这里**只渲染**它（与 AGENTS #7/#8 同一条原则：判据/枚举只在后端一处定义）。
//   - **源声明**（`ruleContent.webJs` / URL 规则带 `webView`，按段归属）→ 留在这里：
//     它要对**正在编辑的表单**即时反应，只有前端知道。生成链用不到它（那时还没有源）。
//
// 合并在 `classifyLayer` 里按「下一步动作」的优先级做一次（L5 → L3 → L4 → L2 → L1），
// 证据行两边拼起来——每条结论都要能指回原文或源里的那一行。

/** 五档：名字 + 下一步动作（界面上直接照着说）。 */
export const LAYER_INFO = {
  L1: { name: "L1 静态直出", action: "直接写选择器" },
  L2: { name: "L2 JS 壳", action: "换能跑 JS 的通道 + webJs" },
  L3: { name: "L3 加密负载", action: "用 webJs 读页面里的全局对象（别自己实现解密）" },
  L4: { name: "L4 接口取数", action: "抓接口 + JSONPath" },
  L5: { name: "L5 需登录 / 被墙", action: "先解决登录态（浏览器 profile 预热或连 App）" },
};

//: 源里带 webView 标记的判据（URL 规则的选项 `,{"webView":true}`）。
//: **与 `DebugService.kt` 的 `webViewPattern` 逐字相同**——同一个结论两个语言各判一次，
//: 漂了就分家（`tests/test_jvm_debug_contract.py` 有一条逐字比对的测试）。
export const WEBVIEW_RE = /"?webView"?\s*:\s*(?:true|1|"true")/i;

//: 「页面读不出来的东西在全局对象里」的痕迹：webJs 脚本引用页面上的全局。
//: **L2 与 L3 的分界就在这里**——L2 只要求渲染（容器在但空），L3 得读页面 JS 建出来的对象。
const GLOBAL_READ_RE = /\b(params|window|globalThis|__NUXT__|__NEXT_DATA__)\b/;

//: 段 → 这条段自己的 URL 字段（`webView` 标记挂在这些规则上）。
//: **按段归属**：`ruleContent.webJs` 说的是**正文那一段**，把它算到搜索页头上会把
//: 「搜索页明明静态直出」的源判成 L2/L3（实测口袋漫画就是这样）。
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
 * 源里所有字符串值（按出现顺序）。
 *
 * **不要用 `JSON.stringify(源)` 去测**：转义后的引号会让「带引号的词」这类判据失效
 * （实测踩过），而且命中时也看不出是哪条规则命中的。
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
 * 源声明的证据（不看页面）。**`step` 给了就只算这一段自己的**：不给（判不出段）时按
 * 「整条源」算——那时宁可多给一条证据，也别把一段的结论按到另一段上。
 */
export function declaredEvidence(source, step) {
  const src = source || {};
  const out = [];
  const rc = src.ruleContent || {};
  const forContent = !step || step === "content";
  const webJs = forContent ? String(rc.webJs || "") : "";
  if (webJs.trim()) {
    out.push({ why: "源声明了 webJs", note: "取值要在页面里执行 JS（DOM 里没有现成的值）",
               snippet: webJs.trim().slice(0, 120), line: 0, readsGlobal: GLOBAL_READ_RE.test(webJs) });
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

/** 源声明的那半判成哪一层（没命中就是空）。 */
export function declaredLayer(source, step) {
  const ev = declaredEvidence(source, step);
  if (ev.some((e) => e.readsGlobal)) return "L3";
  if (ev.length) return "L2";
  return "";
}

const hasLogin = (source) => {
  const src = source || {};
  return !!String(src.loginUrl || "").trim() || !!src.enabledCookieJar;
};

/**
 * 定层：**页面那半读后端结论**（`pages[].page_layer`）+ 源声明那半在这里判，合一次。
 *
 * 返回 `{layer, info, evidence, page, unsure}`：`layer` 为 `""` 表示判不了（`unsure` 里
 * 是为什么——没有页面、没有「要什么」的定义，或两者都不成立：**不硬猜**）。
 *
 * 合并顺序按「下一步动作」：**L5 → L3 → L4 → L2 → L1**。加密痕迹优先于接口痕迹——
 * 前者是「数据要解密」，后者只是「数据要渲染」。
 */
export function classifyLayer(pageLayer, source, opts = {}) {
  const page = pageLayer && typeof pageLayer === "object" ? pageLayer : {};
  const step = String(opts.step || "");
  const srcEv = declaredEvidence(source, step);
  const declared = declaredLayer(source, step);
  const stats = page.stats || null;
  const evidence = [...(page.evidence || []), ...srcEv];
  const out = { layer: "", info: null, evidence, page: { stats, hasWanted: page.has_wanted },
                unsure: page.unsure || "" };

  // L5：页面上有登录词 **且** 源声明了登录方式（光看词会把「请登录后评论」判成墙）
  if (page.login_marker && hasLogin(source)) {
    out.layer = "L5";
    out.info = LAYER_INFO.L5;
    out.evidence = [{ why: "页面出现登录词且源声明了登录方式", note: "先解决登录态再看规则",
                      snippet: page.login_marker, line: 0 }].concat(evidence);
    return out;
  }
  if (page.layer === "L3" || declared === "L3") {
    out.layer = "L3";
  } else if (page.layer === "L4") {
    out.layer = "L4";
  } else if (declared === "L2" || page.layer === "L2") {
    out.layer = "L2";
  } else if (page.layer === "L1") {
    out.layer = "L1";
  }
  if (out.layer) {
    out.info = LAYER_INFO[out.layer];
    out.unsure = "";
  }
  return out;
}
