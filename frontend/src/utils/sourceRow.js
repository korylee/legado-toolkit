// 列表那一行的**显示事实**：一行显示什么健康态、什么深度结论、哪些标签，全部从
// `row` 算出来——纯函数，不碰请求、不碰状态。
//
// 为什么单独成文件：这些判断在列表页要渲染**两遍**（移动端卡片 + 桌面表格），
// 而重复的是**规则**不是样式。规则分两处写，改一处另一处不报错，只会在另一种
// 形态上得出不同结论（同一个源手机上标「需登录」、表格里标「可用」）——
// 与 AGENTS #10 那条「一处写、别处只留指针」是同一件事，只是这次落在模板上。
//
// 与 `utils/health.js` 的分工：那边是**枚举的显示文案**（后端下发的显示层副本，
// 如 HEALTH_LABELS）；这边是**一行的派生结论**（把 row 的字段翻成要显示的东西）。
// 别把两边合并：一个是词表，一个是判断。
import { isQualityTag, isStatusTag, splitTags, tagOfType } from "./tags";
import { healthLabel } from "./health";
import { DEPTH_SHORT } from "./checkFields";

//: 健康态 → el-tag 配色。按「动作的紧急度」分色：可用/已失效是结论的两极
//: （success/danger），需登录/需翻墙/待验证都是「还没到删的地步」（warning/info）。
const healthType = {
  ok: "success", dead: "danger", auth: "warning", gfw: "info",
  pending: "info",
};

export const typeLabel = (v) => tagOfType(v) || ("类型" + v);

//: 列表 API 的 toc_complete/content_ok 是 0/1/null（SourceOut 里是 Optional[int]，
//: 由 SQLite 的三态整数来的），而校验结果里是 true/false/null（Python bool）。
//: **不转就静默坏掉**：模板里判的是 `=== 1`，赋个 true 进去两个分支都不成立，
//: 表现是那一格永远显示「未验证」——看着像没校验，其实是类型不对
export const triToInt = (v) => (v === true ? 1 : v === false ? 0 : null);

//: URL 两侧必须同口径：列表里的 source_url 是库里归一化过的（去空白/尾斜杠/小写），
//: 后端下发 items 时也已归一。这里再兜一次——归一化是最容易漏在半路的那种约定
export const urlKey = (u) => String(u || "").trim().replace(/\/+$/, "").toLowerCase();

//: 用户**手动锁定**的健康状态（`system_tags_locked`）。
//:
//: 锁定只改了 `group_name` 里的状态标签，而「健康」这一列读的是 `checks` 的实测
//: 结论——于是同一屏上标签说「可用」、列说「失效」，两个来源打架。锁定的意思就是
//: 「这条以我为准」，所以列里要显示锁定的值，**实测值进 tooltip 不丢**。
export function lockedStatus(row) {
  if (!row.system_tags_locked) return "";
  return splitTags(row.group_name || "").find((t) => isStatusTag(t)) || "";
}

export function userTagsOf(row) {
  return splitTags(row.user_tags || "");
}

// group_name 是系统标签的逗号拼接串。类型和健康状态表格里已各自单独成列展示，
// 这里只取不重复的质量标签（规则完整），避免同一信息渲染两遍。
export function qualityTagsOf(row) {
  return splitTags(row.group_name || "").filter((t) => isQualityTag(t));
}

/**
 * 健康那一格的**同一个判断**：锁定优先于实测，两者都没有才返回 null。
 *
 * 返回 null 表示没有可显示的状态——表格显示「未校验」，卡片不显示那一格（卡片窄，
 * 下面那行「验到哪」已经说明了同一件事）。
 *
 * 做成函数而不是两段模板分支：这是三处（卡片、表格、表格的 tooltip）都要一致的口径，
 * 而「锁定」这一档存在的全部理由就是对冲两个来源打架——两处渲染出不同结论，
 * 比多写几行代码糟得多。
 */
export function healthCell(row) {
  const locked = lockedStatus(row);
  if (locked) return { locked: true, label: locked + " · 手动", type: "warning" };
  if (row.health) {
    return { locked: false, label: healthLabel(row.health),
             type: healthType[row.health] || "info" };
  }
  return null;
}

/**
 * 标签那一格的**标签序列**（质量标签 → 手动 → 用户标签），顺序与配色都只有这一份。
 *
 * `key` 带上来源前缀：同一个词既可能是系统标签也可能是用户标签，只用 label 当 key
 * 会撞（撞了之后 Vue 复用错节点，表现是删除一个标签、另一个跟着变）。
 */
export function tagCellsOf(row) {
  const out = qualityTagsOf(row).map((t) => ({ key: "q:" + t, label: t, type: "info" }));
  if (row.system_tags_locked) out.push({ key: "locked", label: "手动", type: "warning" });
  return out.concat(userTagsOf(row).map((t) => ({ key: "u:" + t, label: t, type: "success" })));
}

//: 「一个标签都没有」的判据。只数质量标签与用户标签——「手动」不算：它标的是
//: 「状态是我锁的」，不代表这条源有标签。
export const hasAnyTag = (row) => !!(qualityTagsOf(row).length || userTagsOf(row).length);

//: 深度列上的**结果**标记。深度只说「验到哪」，而 4★/5★ 依赖的是 toc/content 的
//: 实测结果——depth≥3 里约一半是「验了但没过」（实测：depth 3 有 118 条目录不完整，
//: depth 4 有 8 条正文不可用）。取**最深的那个有结论的**：正文优先、其次目录；
//: 两者都没结论就不标——不能把「没验到」说成「没过」。
export function depthVerdict(row) {
  if (row.content_ok != null) return { label: "正文", ok: row.content_ok === 1 };
  if (row.toc_complete != null) return { label: "目录", ok: row.toc_complete === 1 };
  return null;
}

//: 深度列显示的那行字：**结果优先，深度兜底**。
//:
//: 有结论就显示「正文 ✗」「目录 ✓」——那才是「验得怎么样」；没有结论才显示验到
//: 哪一步（主页/搜索/…）。**不要拼成「正文 目录 ✗」**：depth 恰好等于结果所在那步时
//: 会写成「目录 目录 ✗」（实测 125 条），读起来像重复；而那种情况下深度信息
//: 由 tooltip 兜住，代价可以忽略（实测只有 38 条落在"深度比结论更深"的形态）。
export function depthText(row) {
  const v = depthVerdict(row);
  if (v) return v.label + (v.ok ? " ✓" : " ✗");
  return DEPTH_SHORT[row.probe_depth] || "";
}

//: 结果好坏的着色（**没有结论就不着色**）：一眼扫过去，绿=验过且通过、红=验了没过
export function depthClass(row) {
  const v = depthVerdict(row);
  return v ? (v.ok ? "v-ok" : "v-bad") : "";
}

//: 本机引擎逐段结论的**状态名**（`checks.jvm_state` 的下发值）。
//: 认不出的取值原样返回：宁可显示 "xxx" 也不要显示 undefined。
export const JVM_STATE_LABELS = {
  ok: "搜索通过", no_result: "跑了但没出结果", empty_js_shell: "JS 规则是空壳",
  login_wall: "页面要求登录（不是源坏了）",
  timeout: "超时", error: "报错", invalid: "规则无效",
};
export function jvmStateLabel(state) { return JVM_STATE_LABELS[state] || state; }

//: 结论按**段**展开成若干行，供 tooltip 逐行显示（原来这些只喂 JVM 列那一格，
//: 现在「验证」列一列一结论，明细全在它的 tooltip 里）。
//:
//: **只显示跑到的段**：跑到搜索档的行不该出现「正文：未验证」——那是**没跑**，
//: 不是**跑了没过**，摆在一起会让人以为源有问题（S3-3 的核心取舍）。
//: 段的顺序固定为 搜索 → 目录 → 正文，与探测深度同向。
export function jvmSteps(row) {
  const stage = row.jvm_stage || "";
  if (!stage) return [];
  const out = [{
    label: "搜索",
    text: row.jvm_hit != null ? "命中 " + row.jvm_hit + " 本"
      : (jvmStateLabel(row.jvm_state) || "—"),
    ok: row.jvm_state === "ok" ? true : null,
  }];
  if (stage === "toc" || stage === "content") {
    out.push({
      label: "目录",
      text: row.jvm_toc_count != null ? row.jvm_toc_count + " 章" : "未取到",
      ok: row.jvm_toc_ok === true ? true : row.jvm_toc_ok === false ? false : null,
    });
  }
  if (stage === "content") {
    out.push({
      label: "正文",
      text: row.jvm_content_len != null ? row.jvm_content_len + " 字" : "未取到",
      ok: row.jvm_content_ok === true ? true : row.jvm_content_ok === false ? false : null,
    });
  }
  return out;
}
