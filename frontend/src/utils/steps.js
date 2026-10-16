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
export const RULE_FIELD_DEFS = [
  { step: "search", field: "ruleSearch.bookList", group: "ruleSearch", key: "bookList", label: "搜索 · bookList", placeholder: "class.book-list@tag.li" },
  { step: "bookUrl", field: "ruleSearch.bookUrl", group: "ruleSearch", key: "bookUrl", label: "搜索 · bookUrl", placeholder: "tag.a@href" },
  { step: "bookUrl", field: "ruleBookInfo.name", group: "ruleBookInfo", key: "name", label: "详情 · 书名", placeholder: "class.title@text" },
  { step: "bookUrl", field: "ruleBookInfo.author", group: "ruleBookInfo", key: "author", label: "详情 · 作者", placeholder: "class.author@text" },
  { step: "bookUrl", field: "ruleBookInfo.kind", group: "ruleBookInfo", key: "kind", label: "详情 · 分类", placeholder: "class.kind@text" },
  { step: "bookUrl", field: "ruleBookInfo.wordCount", group: "ruleBookInfo", key: "wordCount", label: "详情 · 字数", placeholder: "class.count@text" },
  { step: "bookUrl", field: "ruleBookInfo.lastChapter", group: "ruleBookInfo", key: "lastChapter", label: "详情 · 最新章节", placeholder: "class.last@text" },
  { step: "bookUrl", field: "ruleBookInfo.intro", group: "ruleBookInfo", key: "intro", label: "详情 · 简介", placeholder: "class.intro@text" },
  { step: "bookUrl", field: "ruleBookInfo.coverUrl", group: "ruleBookInfo", key: "coverUrl", label: "详情 · 封面链接", placeholder: "class.cover img@src" },
  { step: "bookUrl", field: "ruleBookInfo.tocUrl", group: "ruleBookInfo", key: "tocUrl", label: "详情 · 目录链接", placeholder: "class.list a@href" },
  { step: "toc", field: "ruleToc.chapterList", group: "ruleToc", key: "chapterList", label: "目录 · chapterList", placeholder: "class.chapter@tag.a" },
  { step: "content", field: "ruleContent.content", group: "ruleContent", key: "content", label: "正文 · content", placeholder: "id.content@text" },
];
