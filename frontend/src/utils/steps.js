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
  bookUrl: "详情链接",
  toc: "目录",
  content: "正文",
};
