// 试跑 / 调试的步骤名 → 中文。**这一份是唯一的**：编辑弹窗的结果卡片和调试抽屉
// 都从这里取。
//
// 步骤名由后端产出（`core/verify.py` 与 `core/app_debug.py` 的 build_steps：
// search / explore / bookUrl / toc / content），前端只负责显示。这两个组件原来
// 各存了一份**逐字相同**的映射，改一处忘一处就会出现「同一步骤两处两种叫法」，
// 而那种不一致最难被发现——两边单看都对。
export const STEP_LABELS = {
  search: "搜索",
  explore: "发现",
  bookUrl: "详情",
  toc: "目录",
  content: "正文",
};

//: 步骤 → 表单字段及其展示元数据。**bookUrl 步覆盖 ruleBookInfo 全组字段**
//:（上游 BookInfo.kt 逐字段记 `┌获取X/└值` 事件，每个字段都值得独立编辑）；
//: 搜索步的 bookUrl 字段求值在搜索页（ruleSearch 组），保留原 def。
//: field 是完整路径（`ruleBookInfo.name`），宿主 setRuleField 直接按组写。
//:
//: 两个能力位**只在「没有」时才写**（缺省 = 有）：
//:   fact   这行有没有实测值。`none` 的行**不渲染**那一格——补一个「还没有实测值」
//:          的空格子，读起来像「引擎取不到」，其实是这行压根不是取值规则。
//:   verify 这行能不能行内「验证」（rule_try）。`none` 的行**不渲染按钮**：
//:          `nextContentUrl` 是 `getStringList(isUrl=true)`，而 rule_try 对 content 步
//:          走单值口径，拿它验会给出误导结论（宁可不给，也不给一个会说反的按钮）；
//:          正文 `content` 同理——正文对不对看的是内容，不是「选不选得中」（判据见
//:          该行定义处），验收一律走重调本步。
//: `key` 是 AI 补全那一侧认字段用的名字（`core.autofill.FILL_SPECS` 的蛇形键）：
//: 没有它的行，AI 补全弹窗就拿不到这个字段的中文名。它只影响取词，不影响写入落点
//: （落点表 `AUTOFILL_RULE_FIELDS` 自己带完整路径；写源用的是 `field`）。
//: **只有能 AI 补全的步才有意义**（现在 `bookUrl` / `search` / `content`），其余步的 `key` 没人读。
export const RULE_FIELD_DEFS = [
  { step: "search", field: "ruleSearch.bookList", group: "ruleSearch", key: "bookList", label: "搜索 · bookList", placeholder: "class.book-list@tag.li" },
  { step: "search", field: "ruleSearch.name", group: "ruleSearch", key: "name", label: "搜索 · 书名", placeholder: "class.title@text" },
  { step: "bookUrl", field: "ruleSearch.bookUrl", group: "ruleSearch", key: "book_url", label: "搜索 · bookUrl", placeholder: "tag.a@href" },
  { step: "bookUrl", field: "ruleBookInfo.name", group: "ruleBookInfo", key: "name", label: "详情 · 书名", placeholder: "class.title@text" },
  { step: "bookUrl", field: "ruleBookInfo.author", group: "ruleBookInfo", key: "author", label: "详情 · 作者", placeholder: "class.author@text" },
  { step: "bookUrl", field: "ruleBookInfo.kind", group: "ruleBookInfo", key: "kind", label: "详情 · 分类", placeholder: "class.kind@text" },
  { step: "bookUrl", field: "ruleBookInfo.wordCount", group: "ruleBookInfo", key: "word_count", label: "详情 · 字数", placeholder: "class.count@text" },
  { step: "bookUrl", field: "ruleBookInfo.lastChapter", group: "ruleBookInfo", key: "last_chapter", label: "详情 · 最新章节", placeholder: "class.last@text" },
  { step: "bookUrl", field: "ruleBookInfo.intro", group: "ruleBookInfo", key: "intro", label: "详情 · 简介", placeholder: "class.intro@text" },
  { step: "bookUrl", field: "ruleBookInfo.coverUrl", group: "ruleBookInfo", key: "cover_url", label: "详情 · 封面链接", placeholder: "class.cover img@src" },
  { step: "bookUrl", field: "ruleBookInfo.tocUrl", group: "ruleBookInfo", key: "toc_url", label: "详情 · 目录链接", placeholder: "class.list a@href" },
  { step: "toc", field: "ruleToc.chapterList", group: "ruleToc", key: "chapterList", label: "目录 · chapterList", placeholder: "class.chapter@tag.a" },
  // 正文**给行内快验**（D6：按事实判，不是一刀切）：材料与规则形态对不上时按钮根本不出现
  // （判据在后端 `core.rule_try`，前端 `canFastVerify` 只为显隐）；快验只回答取没取到，
  // 正文可用性一律以「重调本步」的 verdict 为准。
  { step: "content", field: "ruleContent.content", group: "ruleContent", key: "content", label: "正文 · content", placeholder: "id.content@text" },
  // 分页：单条时串行跟随、多条时并发抓取（上游 BookContent.kt），所以它取的是一**组**链接——
  // 组规则拿单值口径去验会说反，这行不给按钮（`verify: "none"` 是**步骤级**的原因，与材料无关）
  { step: "content", field: "ruleContent.nextContentUrl", group: "ruleContent", key: "nextContentUrl", label: "正文 · 下一页链接", placeholder: "class.next@href", verify: "none" },
  // 页面脚本（不是取值规则）：它的结果成为这一步的响应体，取值由 content 那条做；
  // 生效前提是某条 URL 规则开了 webView，判据在后端 `core.quality.static_misconfig_notes`
  { step: "content", field: "ruleContent.webJs", group: "ruleContent", key: "webJs", label: "正文 · webJs（页面脚本）", placeholder: "var urls = params.chapter_images; …", fact: "none", verify: "none" },
];

//: 规则字段（驼峰）→ structured 的蛇形键。**`ruleBookInfo.*` 这组必须与
//: `core.autofill.FILL_SPECS.bookUrl` 的字段集一致**（`TestAutofillFieldMapParity`
//: 逐词比对钉着）：这里是规则的驼峰名，那边是 structured 的蛇形键，两边各改一边的后果
//: 不是报错，是那个字段恒显示「未取到」。
//: **正文的实测值不往这张表里加**：它同时是详情步 AI 写入落点的逆表（往这里加
//: `ruleContent.*`，详情步的 AI 就会多出一个写错组的落点）。
export const FIELD_TO_STRUCTURED = {
  "ruleBookInfo.name": "name",
  "ruleBookInfo.author": "author",
  "ruleBookInfo.kind": "kind",
  "ruleBookInfo.wordCount": "word_count",
  "ruleBookInfo.lastChapter": "last_chapter",
  "ruleBookInfo.intro": "intro",
  "ruleBookInfo.coverUrl": "cover_url",
  "ruleBookInfo.tocUrl": "toc_url",
};

//: autofill 行的 field（后端 FILL_SPECS 的蛇形键）→ 写入草稿的规则字段路径。
//: **bookUrl 部分派生自 FIELD_TO_STRUCTURED 的逆表**（同一组映射不维护两份：
//: 两份各改一边不会报错，只会写进一个不存在的键、界面却说「已写入」）。
//: 搜索步必须手写：它的 name 是**条目级**规则（ruleSearch.name），与详情步
//: 同名不同组，反查给不出这层信息。契约测试钉在
//: tests/test_jvm_debug_contract.py::TestAutofillFieldMapParity。
export const BOOKINFO_STRUCTURED_TO_RULE = Object.fromEntries(
  Object.entries(FIELD_TO_STRUCTURED).map(([ruleField, key]) => [key, ruleField]),
);
export const AUTOFILL_RULE_FIELDS = {
  bookUrl: BOOKINFO_STRUCTURED_TO_RULE,
  search: {
    book_url: "ruleSearch.bookUrl",
    name: "ruleSearch.name",
  },
  // 正文步手写：两个字段同在 ruleContent 组（`key` 是 FILL_SPECS 的蛇形键，落点是驼峰路径）
  content: {
    webJs: "ruleContent.webJs",
    content: "ruleContent.content",
  },
};
