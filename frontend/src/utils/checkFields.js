// 校验参数的字段定义：**唯一一份**。
//
// 「设置 → 校验」（全量）与「校验参数 → 本次覆盖」（只含 perRun 的那些）
// 都从这里渲染。之前两个组件各写了一遍字段列表，加了缓存有效期之后字段数
// 就分家了（8 vs 6），而且**没有任何机制保证同步**——加第三个设置项要记得
// 改三处（settings_store.DEFAULTS、CheckSettingsPanel、CheckOverrideForm）。
//
// perRun 回答的是"这一项能不能被本次校验覆盖"，这个决定以前只隐含在
// "两个组件各写了什么"里，现在写在字段上。
//
// 为什么缓存有效期不能覆盖：它是**策略**（结果保留多久），不存在"这一次算
// 几天有效"的说法。本次想强制重测用「忽略缓存」开关——那才是它的一次性对应物。

// 探测深度的说明文案（候选值来自后端 limits.probe_depth，这里只负责显示成什么）。
//
// **一档对一级星级**，所以每一档都把「能验到哪颗星」和「每源要多打几次请求」写出来——
// 后者是决定值不值得开高档的唯一依据。合并前这里是 3 档、另加一个「搜索探测」开关，
// 两个旋钮能配出「关搜索却要验目录」这种永远跑不到的组合。
export const DEPTH_LABELS = {
  1: "1 · 主页（1 次请求 → 1★ 可达）",
  2: "2 · 搜索（+1~2 次 → 2★ 连通 / 3★ 命中）",
  3: "3 · 目录（+2 次 → 4★ 实测目录）",
  4: "4 · 正文（+1 次 → 5★ 实测正文）",
};

//: 深度的**短标签**，给列表那一列用（`DEPTH_LABELS` 是设置表单里的长文案，
//: 两者都对着后端的 `PROBE_DEPTHS`，改档位时一起改）。
export const DEPTH_SHORT = { 1: "主页", 2: "搜索", 3: "目录", 4: "正文" };

export const CHECK_FIELDS = [
  {
    key: "concurrency", label: "并发数", type: "number", perRun: true,
    suffix: "同时发出的请求数",
  },
  {
    key: "timeout", label: "单请求超时", type: "number", perRun: true,
    suffix: "秒",
  },
  {
    key: "probe_depth", label: "探测深度", type: "depth", perRun: true,
  },
  {
    key: "verify_ssl", label: "校验 SSL 证书", type: "bool", perRun: true,
    hint: "关掉可绕过自签名证书报错，代价是不再校验 TLS 身份",
  },
  {
    key: "proxy", label: "代理", type: "text", perRun: true,
    placeholder: "http://127.0.0.1:7890",
    hint: "留空直连。只支持 http:// 与 https://（socks5 需要额外依赖，本项目未装）。"
        + "填了就是所有校验请求都走它，直连能通的源也会绕一圈。",
  },
  {
    key: "cache_ttl_ok", label: "可用源缓存有效期", type: "number", perRun: false,
    suffix: "天",
    hint: "有效期内直接复用校验结果、不重新请求。",
  },
  {
    key: "cache_ttl_auth", label: "「需登录」缓存有效期", type: "number", perRun: false,
    suffix: "天",
    hint: "它是由页面里的登录/反爬特征**推断**出来的状态（不像 403 那样是站点明确拒绝），"
        + "而触发它的常常是当时的临时页面。设 0 = 每次校验都重测这一档。",
  },
  {
    key: "cache_ttl_other", label: "其他状态缓存有效期", type: "number", perRun: false,
    suffix: "天",
    hint: "待验证/需登录/需翻墙留短一点，免得旧结论一直挂着。"
        + "要这一次全部重测，用工具栏「校验参数 → 忽略缓存」。",
  },
];

//: 能被「本次校验」临时覆盖的那些
export const PER_RUN_FIELDS = CHECK_FIELDS.filter((f) => f.perRun);

/** key -> 中文标签，给"本次：并发数 8、代理 直连"这类摘要用。 */
export const FIELD_LABELS = Object.fromEntries(
  CHECK_FIELDS.map((f) => [f.key, f.label]),
);
