// 材料栏：**这一步手上真有哪几份材料**、默认先看哪一份，以及层证据怎么渲染。
//
// 两条分工（别把判据搬到前端来）：
//   - 层→材料种类的权威映射在后端 `core/agent_plan.MATERIAL_KINDS`（给 Agent 用）。
//     前端不抄一份：这里只回答「这次结果里真有哪几份」——没有的那份**不出标签**，
//     摆一个点开是空的标签，读起来像「引擎没给」，其实是这一步不该有它。
//   - 层本身的判据在 `core/page_layer.py`（判完挂在 `pages[].page_layer`，含证据行）。
//     这里只把它渲染成一行**事实**：不给下一步动作——下一步的判据只有 `core/agent_plan`
//     一处，接上 `/agent-plan` 之前不新增第二条。
//
// 材料是**取证**不是验证：句子只说观测到了什么，不说「通过」。

/** 层码 → 一句话。层码本身的取值域在 `core/agent_context.LAYERS`。 */
export const LAYER_LABELS = {
  L1: "原文里就有目标",
  L2: "容器在、内容要渲染",
  L3: "要解密或读运行时对象",
  L4: "数据走接口",
  L5: "要登录",
};

//: 默认材料的优先序：**先看这一步独有的那份**（L3 看运行时形状、L4 看接口条目、
//: 正文看取到的全文），大纲/命中片段是各步都有的兜底。
const PREFERRED = ["runtime", "network", "content", "outline", "matched", "page"];

function isFilled(value) {
  return !!value && typeof value === "object" && Object.keys(value).length > 0;
}

/**
 * 这一步的材料标签（有序）。``name`` 是步名（`core.app_debug.SEGMENT_NAMES` 的取值）。
 *
 * 大纲 / 命中片段 / 页面原文三份**恒在**（各自的空态会说清为什么没有：连 App 通道
 * 取不到命中片段、这一步没抓到页面……「为什么没有」本身是信息）；其余三份只在
 * 真有的时候出现。
 */
export function materialTabs({ name = "", content = null, runtime = null, network = null } = {}) {
  const tabs = [
    { value: "outline", label: "大纲" },
    { value: "matched", label: "命中片段" },
    { value: "page", label: "页面原文" },
  ];
  // 正文全文只有正文步有；接口条目是**运行级**材料（本次运行最后一次渲染发过的请求，
  // 通常就是正文页那一次），所以也只挂在正文步上——挂到别的步上是把材料说错地方
  if (name === "content" && content && content.hasText) {
    tabs.push({ value: "content", label: "正文全文" });
  }
  if (isFilled(runtime)) tabs.push({ value: "runtime", label: "运行时形状" });
  if (name === "content" && Array.isArray(network) && network.length) {
    tabs.push({ value: "network", label: "接口条目" });
  }
  return tabs;
}

/** 该步默认先看哪一份：`PREFERRED` 里第一个存在的；一份都没有则空串。 */
export function preferredMaterial(tabs) {
  const values = (tabs || []).map((tab) => tab && tab.value).filter(Boolean);
  return PREFERRED.find((value) => values.includes(value)) || values[0] || "";
}

/** 一条证据行 → 一句话（`why` + `note` + 出处）。出处要能指回去复核（AGENTS #4）。 */
export function evidenceLine(item) {
  if (!item || typeof item !== "object") return "";
  const where = [];
  if (item.source) where.push(String(item.source));
  if (Number(item.line) > 0) where.push("第 " + Number(item.line) + " 行");
  return [item.why, item.note, where.join(" ")]
    .map((part) => String(part == null ? "" : part).trim())
    .filter(Boolean)
    .join(" · ");
}

/**
 * 这一页的层与证据（`pages[].page_layer`）→ 渲染用的一行事实；没有页面/没有判定返回 null。
 *
 * ``code`` 空但有 ``unsure`` 也是**事实**（判不出层，且说清了为什么）——照原样显示，
 * 不造一个 L0，也不说成「L1」（读不到 ≠ 没有，AGENTS #12）。
 */
export function layerRow(page) {
  const raw = page && page.page_layer;
  if (!raw || typeof raw !== "object") return null;
  const code = String(raw.layer || "").trim().toUpperCase();
  const lines = (Array.isArray(raw.evidence) ? raw.evidence : [])
    .map(evidenceLine)
    .filter(Boolean);
  const unsure = String(raw.unsure || "").trim();
  if (!code && !lines.length && !unsure) return null;
  return {
    code,
    label: LAYER_LABELS[code] || "",
    lines,
    more: Math.max(0, lines.length - 1),
    unsure,
  };
}

/**
 * 运行时形状（L3 的材料）→ 文本。**只有形状没有值**：类型、键名、计数、样例域名——
 * 采集侧就只采这些（Kotlin `BrowserBridge.RUNTIME_SHAPE_EXPR`），这里也不去猜值。
 */
export function runtimeText(runtime) {
  if (!runtime || typeof runtime !== "object") return "";
  const lines = [];
  const paramsType = String(runtime.params_type || "").trim();
  if (paramsType) lines.push("params 类型：" + paramsType);
  const keys = Array.isArray(runtime.keys) ? runtime.keys : [];
  if (keys.length) lines.push("键（" + keys.length + "）：" + keys.join("、"));
  const images = runtime.chapter_images;
  if (images && typeof images === "object") {
    const parts = [];
    if (images.type) parts.push("类型 " + images.type);
    if (Number.isFinite(images.count)) parts.push("共 " + images.count + " 条");
    if (Number.isFinite(images.http)) parts.push("http " + images.http);
    if (Number.isFinite(images.blob)) parts.push("blob " + images.blob);
    if (images.sample_host) parts.push("域名 " + images.sample_host);
    if (parts.length) lines.push("chapter_images：" + parts.join(" · "));
  }
  for (const fail of Array.isArray(runtime.fails) ? runtime.fails : []) {
    lines.push("采集失败：" + String(fail));
  }
  // 页面里的候选数据源（`globals`）：**只列形状**，不猜哪个是图片——那是判给 AI 的事。
  // 空清单与采集失败是两件事：这里只在真有候选时说，失败走上面的「采集失败」。
  const globals = Array.isArray(runtime.globals) ? runtime.globals : [];
  if (globals.length) {
    const rows = globals.map((g) => {
      const name = String(g.name || "");
      const type = String(g.type || "").replace(/^\[object\s*|\]$/g, "");
      if (Number.isFinite(g.count)) return name + " · " + type + " · " + g.count + " 条";
      const keys = Array.isArray(g.keys) ? g.keys.length : 0;
      return name + " · " + type + (keys ? " · " + keys + " 个键" : "");
    });
    lines.push("候选数据源（" + globals.length + "）：\n  " + rows.join("\n  "));
    const dropped = Number(runtime.globals_dropped || 0);
    if (dropped > 0) lines.push("还有 " + dropped + " 个没列出来（超出上限）");
  }
  return lines.join("\n");
}

/**
 * 这份材料**是谁取回来的**（`pages[].origin`）：`engine` = 本机引擎这次调试交回来的整页
 * （App 手上那份，过了它的 JS / cookie / UA），`fetch` = 我们自己的 HTTP 栈另抓的一份。
 *
 * 两份**可能不是同一页**（L2–L4 的站点尤其），而「验证」验的就是手上这一份——所以界面
 * 必须说得出来。没有页面材料返回 null（不造「未知来源」）。
 */
export function materialOrigin(page) {
  if (!page || typeof page !== "object") return null;
  const origin = String(page.origin || "");
  if (origin === "engine") {
    return {
      kind: "engine",
      label: "引擎那份（0 请求）",
      hint: "本机引擎这次调试交回来的整页：过了它的 JS / cookie / UA",
    };
  }
  if (origin === "fetch") {
    const when = String(page.fetched_at || "");
    return {
      kind: "fetch",
      label: when ? "另抓的一份（" + when + "）" : "另抓的一份",
      hint: page.cached
        ? "来自缓存，不是这次新抓的"
        : "我们自己另抓的一份：可能与引擎看到的不同（L2–L4 尤其）",
    };
  }
  return null;
}

/**
 * 这一步该给哪个动作——判据是**材料 + 层**，不是规则：
 *
 *   `verify` 用手上这份材料跑快验（`rule_try` 永远不发请求）；
 *   `render` 这份材料不对味（层 L2 要渲染后才有内容，而手上是另抓的静态页）→ 先跑一次
 *            本步调试把引擎那份取回来，之后同一页上的验证都是 0 请求。
 *
 * 其余层（L3/L4/L5/判不了）一律只给 `verify`：那两层的材料还没做，给「取材料」是假线索。
 */
export function verifyAction({ layer = "", origin = "" } = {}) {
  if (String(origin || "") === "engine") {
    return { kind: "verify", label: "验证", hint: "" };
  }
  if (String(layer || "").trim().toUpperCase() === "L2") {
    return {
      kind: "render",
      label: "取渲染材料",
      hint: "这一页要渲染后才有内容：跑一次本步调试取引擎那份（之后同一页上的验证都不发请求）",
    };
  }
  return { kind: "verify", label: "验证", hint: "" };
}

/** 单条响应体的展示上限：再长也不是给人读的（完整的那份在页面上一次请求里）。 */
const NETWORK_BODY_CHARS = 4000;

/**
 * 接口条目（L4 的材料：本页实际发过的请求）→ 文本。**它是证据**：方法、地址、状态、
 * 响应片段原样带出来，别在这里判「哪条才是正文接口」——那是用户或 AI 看着材料做的判断。
 */
/**
 * 这一页**走没走浏览器渲染**——与 `materialOrigin` 是两件事，别互相代替。
 *
 * `origin` 只说「这份 HTML 是谁交回来的」（引擎 / 我们另抓），**不等于渲染过**：实测
 * 源没在 URL 规则上声明 `webView` 时，引擎那份同样是静态页（`rendered=0`、48KB）；
 * 加 `{"webView":true}` 之后才是渲染页（`rendered=1`、218KB、运行时形状才有）。
 *
 * 判据**不在这里**：归属由后端判到页上（`core/jvm_debug._annotate_render`）——页面地址
 * 带着 App 才懂的请求选项（`…,{"webView":true}`），剥它的权威实现只有 Python 那一份
 * （AGENTS #20），前端比字符串会说反。这里只把 `page.rendered` 翻成人话：
 *   - `true` → 这份材料是渲染后的；
 *   - `false` → 这一页要渲染但没成，带上原因；
 *   - 缺 → **不说**（读不到 ≠ 没渲染；没声明 webView 的静态页也不该被念叨）。
 */
export function renderLine(page) {
  if (!page || typeof page !== "object") return null;
  if (page.rendered === true) {
    return {
      kind: "rendered",
      text: "这一页是渲染后的",
      hint: "浏览器跑完页面脚本才拿到的这份材料",
    };
  }
  if (page.rendered === false) {
    const code = String(page.render_reason || "");
    const why = REASON_TEXT[code] || code;
    return { kind: "failed", text: "这一页要渲染但没成" + (why ? "：" + why : ""), hint: "" };
  }
  return null;
}

//: 侧车 `shadow_last_reason` 的码 → 人话。**认不出的原样显示**：那是「对端多了一个码」，
//: 藏起来就没人知道要补这一条（AGENTS #4）。
const REASON_TEXT = {
  no_url: "既没有 url 也没有 html",
  browser_unavailable: "本机浏览器不可用",
  unsupported_is_rule: "这条页面脚本要用页面环境，本机调试不支持",
  unsupported_is_rule_local: "这条页面脚本在本机求值失败",
};

export function networkText(entries) {
  const list = Array.isArray(entries) ? entries : [];
  return list
    .map((item, index) => {
      if (!item || typeof item !== "object") return "";
      const head = "#" + (index + 1) + " " + String(item.method || "GET") + " "
        + String(Number(item.status) || 0) + " " + String(item.url || "");
      const mime = String(item.mime || "").trim();
      const post = String(item.post_data || "").trim();
      const body = String(item.body || "");
      return [
        head.trim(),
        mime ? "类型：" + mime : "",
        post ? "提交：" + post : "",
        body ? "响应片段：\n" + body.slice(0, NETWORK_BODY_CHARS) : "",
      ].filter(Boolean).join("\n");
    })
    .filter(Boolean)
    .join("\n\n");
}
