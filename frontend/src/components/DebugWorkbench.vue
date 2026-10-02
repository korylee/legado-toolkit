<!-- 调试工作台本体（第三期自 RuleDebugDrawer 抽出）：判定/诊断/证据/规则编辑。
     运行态读 useDebugSession；编辑上下文（源/规则/类型）走 props——弹框与工作台
     页面都挂它。旧壳 RuleDebugDrawer 只是 el-drawer 薄包装。 -->
<script setup>
// 调试抽屉：把每一步的证据摊开。
// 三个子页签：提取结果（全文）/ 命中源码 / 整页源码。
//
// **引擎只有一台**（App 引擎：连 App / 本机），判定与逐段结论都来自它——
// 2026-09-20 起抽屉里**不再提供"本地跑一遍"这个动作**（原来那个「重新调试本页」
// 按钮、候选规则的「试」都撤了：它们给的是离线引擎的判定，而这台引擎只是 App 的
// 近似。要验一条规则就点「重新调试本步」，那是真引擎，D2 之后第二次约 1 秒）。
//
// **本地回放只剩一个用途**：「命中源码」那块 DOM——引擎现在自己会把它带回来
// （`steps[].matched_html`，第三期回填），**只有引擎没覆盖的段**（详情段、末段是
// 属性名的规则）才退回本地投影：拿我们补抓的页面把规则跑一遍。两者来源不同，
// 界面上分别标着（引擎给的 = App 选中的；投影 = 可能不一样）。
//
// 设计取舍：**源码默认按「格式化」显示，但能手切回「原文」，复制永远是原文**。
// 原来只给原文（pre-wrap 软换行 + 按字符偏移分片渲染），理由是不能让用户把改过字符的
// 文本当真实响应——那条理由还成立，所以：格式化只是**显示**（`utils/htmlView.js`：只动
// 空白 + 展开实体），按钮写「复制原文」，切回「原文」就是响应本身。
import { ref, computed, watch, nextTick, onMounted } from "vue";
import { ElMessage } from "element-plus";

import { agentPlan, replayStep, ruleCandidates, suggestRule, verifyCandidate } from "../api/rules";
import { getLLMStatus } from "../api/llm";
// 步骤名 → 中文的**唯一**一份（编辑弹窗共用），别再在本组件里写第二份
import { FIELD_OF_STEP, STEP_LABELS } from "../utils/steps";
import { useDebugSession } from "../composables/useDebugSession";
import { compareRuns, statusLabel } from "../utils/debugCompare";
// 第 1 层「在页面上找目标」：候选规则由后端 core/candidates 在补抓的原文上算出来
// （启发式的唯一一份；不发请求、不调模型），见下方 watch
// 源码的显示层（折行缩进 + 实体展开，纯函数）：好看的和能抄的是两份东西，见 htmlView
import { formatHtml } from "../utils/htmlView";
// 定层（九-1）：先定层再写规则——判据与证据行都在纯函数里，这里只负责把「这一步要什么」传进去
import { classifyLayer } from "../utils/layers";
// 五格决策：**首屏的判据只有这一份**（现状 / 解决 / 取证 / AI 补足）。原来这里的
// `diagnosis` / `aiLayerBlock` 与 `debugOutlets` / `debugNextAction` / `debugEvidence`
// 各判一遍，同一件事说三遍、动作互相覆盖——现在收敛进 `buildDecision`。
import { buildDecision, decisionLines } from "../utils/debugDecision";
// 点选（九-2a）：元素 → 候选选择器 + 实测三个数。**只是提议**，验收仍走真引擎
import { parseDoc, previewCss, selectorCandidates } from "../utils/selector";
import { assessRuleQuality } from "../utils/ruleQuality";
import { candidateView } from "../utils/debugCandidate";
import DebugCandidateCard from "./DebugCandidateCard.vue";
import StepDecision from "./StepDecision.vue";

const props = defineProps({
  initialStep: { type: String, default: "" },
  //: 源有没有声明 cookie jar。登录墙判定要用它：那一档「200 + 登录词」以它为前提
  //: （「请登录」在正常页面的导航栏里太常见）
  enabledCookieJar: { type: Boolean, default: false },
  //: 完整源对象（表单里那份）：定层要读源**自己声明的能力**——`ruleContent.webJs` /
  //: `loginUrl` / URL 规则里的 `webView`（AGENTS #13：能推导的别让用户填）
  source: { type: Object, default: null },
  //: 「从此步重跑」在途（父组件的 appDebugging）。重跑是 App 实测动作，
  //: 在途时按钮要转圈、并挡住连点——两次分段调试的 WS 会话会互相顶掉
  //: 自动生成失败时保留的上游原因；调试材料是补救路径，不得覆盖这条原因。
  entryError: { type: String, default: "" },
});

// 结果、在途、对比基线直接读**会话**（第二期收尾）：本组件不再经 props 接收
// 运行态——弹框与工作台页面挂它，看到的是同一份
const { result, compare, running, channel: debugChannel } = useDebugSession();
const rerunning = running;
const prevResult = computed(() => compare.value.prev);
const emit = defineEmits(["update:modelValue", "goto", "rerunFrom", "applyRule"]);

//: 规则四段与源类型都从 **source 自己**派生，宿主只传 source 一份（AGENTS #13：
//: 算得出的不传）——历史上它们是两个独立 props，靠宿主记得传齐；工作台路由那次
//: 迁移 `enabledCookieJar` 就漏传过。再要加派生输入，从这里出，别加 props。
const sourceType = computed(() => Number((props.source || {}).bookSourceType) || 0);
const rules = computed(() => {
  const s = props.source || {};
  const rs = s.ruleSearch || {};
  const rt = s.ruleToc || {};
  const rc = s.ruleContent || {};
  return { search: rs.bookList || "", bookUrl: rs.bookUrl || "",
           toc: rt.chapterList || "", content: rc.content || "" };
});

// explore 是发现链路的产出步（key 带 `发现::` 时后端才产出它）
//: 每次渲染的字符数。整页 HTML 可能 100 万字符，全量进 DOM 会卡
const RENDER_CHUNK = 20000;
//: 搜索最多索引的命中数。整页 HTML 里搜 div / class 必然远超此数，
//: 超限时必须显式告知，否则计数器会把「前 200 处」说成全部
const HIT_LIMIT = 200;

const staleSteps = ref(new Set());

function markStepStale(name) {
  if (!name) return;
  staleSteps.value = new Set([...staleSteps.value, name]);
}

// 新的真实引擎结果到达后，只有实际返回的步骤才清除对应 stale 标记；
// 错误体或空结果不应把“规则已修改、尚未验证”洗掉。
watch(result, (next) => {
  const names = (next && next.steps || []).map((step) => step.name).filter(Boolean);
  if (!names.length) return;
  const nextStale = new Set(staleSteps.value);
  names.forEach((name) => nextStale.delete(name));
  staleSteps.value = nextStale;
});

const activeStep = ref("");
const subTab = ref("values");

//: 两块源码的显示形态：true = 折行缩进 + 实体展开（好读）；false = 原文（好抄）
const formatSource = ref(true);
const renderLimit = ref(RENDER_CHUNK);
const searchKey = ref("");
const activeHit = ref(0);

const steps = computed(() => (result.value && result.value.steps) || []);
const pages = computed(() => (result.value && result.value.pages) || []);

//: 结果来源。**两台都是 App 引擎**，差别只在环境，所以必须标出来：
//:   - `app`：连 App 实测（手机上跑，登录态/网络出口都是真的）
//:   - `jvm`：本机引擎（**同一段 App 代码**跑在本机：真规则、真 JS；差在环境——
//:     登录态要预热、没有手机的网络出口）见 lessons §六十三
//: 本地回放不再是来源（它只在「命中源码」那里做 DOM 投影，不在结果链路上）
const channel = computed(() => String((result.value || {}).source || ""));
const isAppResult = computed(() => channel.value === "app");
//: 跑的是不是「App 的真引擎」——连 App 与本机引擎都算。事件流页签、分段重跑、
//: 「用实测值当基准」这些只对真引擎成立
const isEngineResult = computed(() => channel.value === "app" || channel.value === "jvm");
//: App 推来的原始事件流。steps[].values 是它去掉耗时前缀后的段内文本，
//: 排查「哪一步慢」「App 到底推了什么」只能看这里
const events = computed(() => (result.value && result.value.events) || []);
//: 默认子页签：真引擎（连 App / 本机）看事件流，本地回放看提取结果（那两个 tab
//: 各自只在对应的结果下出现，选错会是一片空白）
const defaultSubTab = computed(
  () => (isEngineResult.value && events.value.length ? "events" : "values"));

// ---------------------------------------------------------------- 与上次相比（ux-debug-loop）
const diffRows = computed(() => (prevResult.value ? compareRuns(prevResult.value, result.value) : []));
const diffBy = computed(() => Object.fromEntries(diffRows.value.map((d) => [d.name, d])));
const diffSummary = computed(() => {
  if (!prevResult.value || !diffRows.value.length) return "";
  const changed = diffRows.value.filter((d) => d.status !== "same")
    .map((d) => `${STEP_LABELS[d.name] || d.name} ${statusLabel(d.status)}`);
  return changed.length
    ? "与上次相比：" + changed.join(" · ") + "；其余相同"
    : "与上次相比：全部相同";
});
function diffOf(name) {
  return diffBy.value[name];
}
function switchToApp() {
  debugChannel.value = "app";
  emit("rerunFrom", (current.value || {}).name);
}
function diffTagType(status) {
  return { regressed: "danger", changed: "warning", moved: "info", new: "success" }[status] || "info";
}
//: 失败步骤「直达诊断区」：诊断块常驻抽屉上部，但可能在折叠线以下——
//: 选中失败段时把它滚进视野（成功时不打扰，默认停在页签区）
const decisionBox = ref(null);
watch([result, activeStep], () => {
  if (!result.value) return;
  if ((current.value || {}).verdict === "fail") {
    nextTick(() => decisionBox.value && decisionBox.value.scrollIntoView(
      { behavior: "smooth", block: "start" }));
  }
});

const replaying = ref(false);
const replayResult = ref(null);

//: 失败步骤 → 该去哪个规则子页签改。搜索与详情链接都在「搜索」页签里
//: （ruleSearch.bookList / bookUrl 是同一个表单的两项）
const STEP_RULE_TAB = {
  search: "search", bookUrl: "search", toc: "toc", content: "content",
};
const current = computed(
  () => steps.value.find((s) => s.name === activeStep.value) || steps.value[0] || null,
);
const currentPage = computed(() => {
  if (!current.value || !current.value.page_id) return null;
  return pages.value.find((p) => p.id === current.value.page_id) || null;
});
//: 这一步的页面源码，按当前显示形态给（格式化 / 原文）。**搜索与渲染都用它**——
//: 两边必须是同一份文本，否则高亮位置会整体错位
const pageText = computed(() => {
  const html = (currentPage.value && currentPage.value.html) || "";
  return formatSource.value && html ? formatHtml(html) : html;
});
//: 复制按钮一律给**原文**（格式化只是给人看的：它动过空白、展开过实体）
const pageTextRaw = computed(() => (currentPage.value && currentPage.value.html) || "");

// 三态圆点：fail 红 / unknown 灰 / pass 且有附注 黄 / 纯 pass 绿
function dotClass(s) {
  if (!s) return "";
  if (s.verdict === "fail") return "err";
  if (s.verdict === "unknown") return "unknown";
  return s.has_notes ? "warn" : "ok";
}
// 搜索在**完整原文**上做（纯字符串扫描，结果不进 DOM），最多记 HIT_LIMIT 处
const hitOffsets = computed(() => {
  const key = searchKey.value.trim();
  const html = pageText.value;
  if (!key || !html) return [];
  const out = [];
  let from = 0;
  while (out.length < HIT_LIMIT) {
    const i = html.indexOf(key, from);
    if (i < 0) break;
    out.push(i);
    from = i + Math.max(1, key.length);
  }
  return out;
});

// 命中是否被 HIT_LIMIT 截断：索引已占满，且最后一处之后还能再找到
// （只看长度不够——恰好 200 处时不该提示「命中过多」）
const hitsTruncated = computed(() => {
  const offsets = hitOffsets.value;
  if (offsets.length < HIT_LIMIT) return false;
  const key = searchKey.value.trim();
  const html = pageText.value;
  if (!key || !html) return false;
  const last = offsets[offsets.length - 1];
  return html.indexOf(key, last + Math.max(1, key.length)) >= 0;
});

// 只渲染前 renderLimit 个字符，避免百万字符全量进 DOM
const headText = computed(() => pageText.value.slice(0, renderLimit.value));
const hasMore = computed(() => renderLimit.value < pageText.value.length);

// 把已渲染的片段按命中位置切成 [普通, 高亮, 普通, ...]
const segments = computed(() => {
  const head = headText.value;
  const key = searchKey.value.trim();
  if (!key) return [{ text: head, hit: false, index: -1 }];
  const segs = [];
  let cursor = 0;
  let hitIndex = 0;
  hitOffsets.value.forEach((off) => {
    if (off >= head.length) return;
    if (off > cursor) segs.push({ text: head.slice(cursor, off), hit: false, index: -1 });
    segs.push({
      text: head.slice(off, off + key.length),
      hit: true,
      index: hitIndex,
    });
    hitIndex += 1;
    cursor = off + key.length;
  });
  if (cursor < head.length) segs.push({ text: head.slice(cursor), hit: false, index: -1 });
  return segs;
});

onMounted(() => {
  if (result.value) activeStep.value = props.initialStep || (steps.value[0] && steps.value[0].name) || "";
});
watch(result, (show) => {
  if (!show) return;
  // 每次打开都重拉：用户可能刚在「设置 → 模型」里配好，不该还看着上一次的结论
  refreshLLMStatus();
  if (show) activeStep.value = props.initialStep || (steps.value[0] && steps.value[0].name) || "";
  subTab.value = defaultSubTab.value;
  renderLimit.value = RENDER_CHUNK;
  searchKey.value = "";
  activeHit.value = 0;
  replayResult.value = null;
});

// 「从此步重跑」完成后父组件会更新 initialStep 想定位到重跑的那一步。
// 抽屉常开时 modelValue 不变（上面那个 watch 不触发），必须自己盯着 initialStep 走
watch(() => props.initialStep, (v) => {
  if (v) selectStep(v);
});

//: 「从此步重跑」可不可点：搜索步永远可以（等于整链）；其余步要上轮结果里
//: 有这一步的 URL（拼 App 的 ++/--/裸URL key 用），没有就只能先跑一次完整链路
const canRerun = computed(() => {
  if (!isEngineResult.value) return false;
  const s = current.value || {};
  if (s.name === "search") return true;
  return !!String(s.url || "").trim();
});

// 跳到第 i 处命中（i 为负则向前），必要时先扩大渲染范围
function gotoHit(i) {
  const total = hitOffsets.value.length;
  if (!total) return;
  activeHit.value = ((i % total) + total) % total;
  const off = hitOffsets.value[activeHit.value];
  if (off + RENDER_CHUNK > renderLimit.value) renderLimit.value = off + RENDER_CHUNK;
  nextTick(() => {
    const el = document.getElementById("debug-hit-" + activeHit.value);
    if (el) el.scrollIntoView({ block: "center", behavior: "smooth" });
  });
}

function selectStep(name) {
  activeStep.value = name;
  subTab.value = defaultSubTab.value;
  renderLimit.value = RENDER_CHUNK;
  searchKey.value = "";
  activeHit.value = 0;
  replayResult.value = null;   // 上一步的重放结论不适用于当前这步
  candidateResults.value = {};
  candidateVerifying.value = "";
}

//: 当前步骤的规则：**只读镜像**源表单里那一份（可编辑的值在父组件手里）。
//: 「网页视图」的命中高亮与规则质量诊断都读它，所以它必须跟着表单即时变。
const currentRule = computed(() => {
  const name = (current.value || {}).name || "";
  return String((rules.value || {})[name] || "");
});
//: 重放要同时满足：这一步对应一个页面的 HTML、这一步有规则可回放、
//: 且这一步确实是一条规则步骤（explore 的规则结构不同，不给重放）
const canReplay = computed(
  () => !!currentPage.value && !!currentRule.value
    && !!STEP_RULE_TAB[(current.value || {}).name],
);

//: 「命中源码」要显示的那段 DOM。**两个来源，标签必须分清**：
//:   - **引擎给的**（`steps[].matched_html`）：连 App / 本机都会带回来，那就是 App
//:     自己选中的那块 DOM；
//:   - **本地投影**（`replayResult`）：引擎没覆盖这一段时的退路，拿我们补抓的页面
//:     把规则跑一遍——它是投影不是判定，所以标成警告色。
//:
//: 顺带说一句用途：本地回放结果正是「让 AI 改规则」要喂给模型的东西（规则 + 它
//: 选中的 DOM），所以两者摆在同一屏上。
const matchedFrom = computed(() => {
  if ((current.value || {}).matched_html) return isAppResult.value ? "App 实测" : "本机引擎";
  return replayResult.value ? "本地调试" : "";
});
//: 来源标签的配色跟着来源走（本地投影只是投影，用警告色）
const matchedFromType = computed(() => {
  if (matchedFrom.value === "本地调试") return "warning";
  return isAppResult.value ? "success" : "primary";
});
//: 引擎给了位置、却没给命中片段：App 通道只推文本（不可修），本机引擎则是真的没
//: 返回。分开判是为了把原因说到用户能照着做（AGENTS #4）；两者都只在
//: `matchedHtml` 为空时才成立，所以不会把投影给的那份误标成空。
const matchedEmptyFromEngine = computed(
  () => isEngineResult.value && !(current.value || {}).matched_html,
);
const matchedHtml = computed(() => (current.value || {}).matched_html
  || (replayResult.value || {}).matched_html || "");
//: 命中源码的显示形态（同 pageText：显示一份、复制一份）
const matchedShown = computed(() => (formatSource.value && matchedHtml.value
  ? formatHtml(matchedHtml.value) : matchedHtml.value));
//: 命中片段为空时那一句话。每种空值都要能照着做（AGENTS #4：不许静默空白）。
//: 顺序就是优先级：页面 → 投影的已知原因（不支持 / 在读 / 选中 0 条）→ 引擎空。
//: 后两者都只在投影没给东西时到达，所以新文案**不会吞掉**
//: 下面那两条旧原因（不支持本地调试 / 没选中）。
const matchedHint = computed(() => {
  if (!currentPage.value) return "这一步没有页面。App 只推文本，页面是我们另抓的";
  // 页面在、录音不能播：这条原因必须原样到用户眼前（AGENTS #4）
  if (!canReplay.value) return "这一步的规则不支持本地调试";
  if (replaying.value) return "正在读取…";
  if (replayResult.value) {
    // 回放不了（JS / xpath 等）时 `rule_error` 就是原因，别笼统说「没有命中」
    return (replayResult.value.rule_error || replayResult.value.detail
            || "这条规则在这份页面上没有选中任何 DOM");
  }
  // 这里开始：投影也没给东西。引擎自己带回的那份命中片段为空，
  // 必须说清楚差在哪个通道、下一步能做什么
  if (matchedEmptyFromEngine.value) {
    if (isAppResult.value) {
      return "本段取不到命中源码：App 调试通道只推文本；要查看本段命中源码，"
        + "请改用本机引擎调试。";
    }
    return "本段取不到命中源码：本机引擎未返回命中片段。";
  }
  // **这一支今天不可达**，写在这里是为了「多一条通道时也不撒谎」（AGENTS #4）。
  // 为什么不可达：本 computed 唯一的消费点是模板里
  // `<pre v-if="matchedHtml">` 的 `v-else`（hint 显示 ⟹ `matchedHtml` 为空 ⟹ 本段
  // `matched_html` 为空）；而结果来源 `source` 只有两个产者——`core/app_debug.py:898`
  // 产出 `"app"`、`core/jvm_debug.py:328` 产出 `"jvm"`——所以 `isEngineResult` 恒真，
  // 上面 `matchedEmptyFromEngine` 那一支必定先命中，走不到这里。
  // 留成显式文案而不是 `return "正在读取…"`：后者在 `replaying` 为假时是谎报
  // （页面在、也没在读，用户只会看到一个永不结束的加载态）。将来多一条通道
  // 时这句就立刻是对的，不必等有人先想起它。
  return "本段取不到命中源码：既没有引擎返回的命中片段，"
    + "本地调试也没选中 DOM。";
});

//: 打开抽屉 / 换步骤就自动回放一次：**诊断与「命中源码」都要它的结果**。
//: 用的是表单里的当前规则，所以改完规则点「用本页重放」就能刷新。
watch([result, activeStep], () => {
  if (!result.value) return;
  if (canReplay.value) doReplay();
});

// ---------------------------------------------------------------- 第 0 层：诊断
// 把「这一步为什么取不到」分成**下一步动作不同**的几类，而不是笼统一句「失败」。
// 全部由前端从已有数据算出（规则字符串、step.url/page_id、notes、本地回放结果、
// 补抓页面的节点统计），不新增后端接口。
//
// 为什么先要有这一层：**取不到有五种成因，动作完全不同**。最坑的是「连页面都没有」——
// 正文规则为空时 App 不会发请求（退回拿章节链接当正文），而补抓只从 `≡获取成功:` 那行
// 取 URL，于是静默跳过：抽屉里整页源码一片空白、也没有一句解释，用户根本不知道该改什么
// （实测口袋漫画就是这样）。

//: 每一步「要拿到什么」。诊断要拿它当对照物，不然「取不到」没有判据
const STEP_WANT = {
  search: { kind: "list", label: "书目列表" },
  explore: { kind: "list", label: "发现列表" },
  bookUrl: { kind: "link", label: "详情页链接" },
  toc: { kind: "link", label: "章节链接" },
  content: { kind: "text", label: "正文内容" },
};
const want = computed(() => {
  const name = (current.value || {}).name || "";
  const w = STEP_WANT[name];
  if (!w) return null;
  // 漫画 / 听书的正文是图片或音频，要的东西不一样，判据也得跟着变
  if (name === "content" && [1, 2, 3].includes(sourceType.value)) {
    return { kind: "media", label: "正文图片/音频" };
  }
  return w;
});

//: 定层（九-1）：**这一页 + 这个源** → 层 + 证据行 + 页面统计。
//: 判据、门槛、证据抓法都在 `utils/layers.js`（纯函数，有 node 断言）——这里只是调用点，
//: **不要在组件里另写一份**（原来那几个统计就长在这儿，容易与那边漂）
const layer = computed(() => classifyLayer(
  // **页面那半读后端结论**（`pages[].page_layer`，判据在 `core/page_layer.py`）；
  // 这半（源声明 + 合并）留在前端，因为要对正在编辑的表单即时反应
  (currentPage.value || {}).page_layer || null, props.source || null,
  { step: (current.value || {}).name || "" }));
//: 补抓页面上的节点统计。「你要的东西这页上到底有没有」全靠它——
//: 没有的话，选择器改多少遍都取不到
const pageStats = computed(() => (layer.value.page || {}).stats || null);
const hasWanted = computed(() => {
  const got = (layer.value.page || {}).hasWanted;
  return got === undefined ? null : got;
});

// —— 第 1 层：在页面上找目标 ——
//: 候选由**后端**（core/candidates，启发式的唯一一份）在补抓的那份原文上算：
//: 不发请求、不调模型。**没有「试」**：验一条候选要走真引擎，也就是「用这条」
//: 填进表单 + 「重新调试本步」——本地跑一遍给的是近似结论，撤掉它正是这一轮的目的。
const candidates = ref([]);

//: 候选面板只对 **L1**（或判不出层）开着：对 L2–L5，这份 HTML 上的选择器能选中、
//: 但**选中的不是数据**（假证据）——那正是「找出来的不符合预期」的根因（TODO §九）。
//: 判不出来（没有页面 / 没有目标定义）时照旧给，那是 L1 的正常情况。
//: **这是材料闸门、不是动作判据**：它只决定「摆不摆这份材料」，动作仍由后端计划给；
//: 同一份闸门也管交给判据的候选——不然 L2–L5 上的假证据会被判成「用候选规则」。
const candidatesUsable = computed(() => !layer.value.layer || layer.value.layer === "L1");

//: 竞态保护：换页/换步后，迟到的旧响应不得覆盖新结论（序号不匹配就丢弃）
let candidatesSeq = 0;
watch([result, activeStep, currentPage], async () => {
  const page = currentPage.value;
  const kind = (want.value || {}).kind || "";
  const seq = ++candidatesSeq;
  if (!page || !kind || !String(page.html || "").trim()) {
    candidates.value = [];
    return;
  }
  try {
    const r = await ruleCandidates(page.html, kind);
    if (seq === candidatesSeq) candidates.value = (r || {}).candidates || [];
  } catch (e) {
    if (seq === candidatesSeq) {
      candidates.value = [];
      ElMessage.error("候选生成失败：" + e.message);
    }
  }
}, { immediate: true });

/** 取到的值能不能当链接打开，返回可打开的绝对地址（空串 = 打不开）。
 *
 *  **不能只认 `http(s)://`**：书源的 URL 规则取到的**大量是站内相对路径**
 *  （实测形态如 `/manhua/xxx-5QBQX/1.html`），那反而是最常见的。
 *  相对路径按 HTML 的规矩相对**这一页的 URL** 解析（`currentPage.url` 就是当初
 *  抓这页用的地址）。
 *
 *  只认这三种形态，别的**一律当普通文本**：把「第一章」这类标题拿去 `new URL()`
 *  会拼出一个看着像链接、点开 404 的地址，比不给按钮更糟。
 */
function openableUrl(v) {
  const s = String(v || "").trim();
  if (!s || /\s/.test(s)) return "";
  const base = (currentPage.value && currentPage.value.url) || "";
  if (/^https?:\/\//i.test(s)) return s;
  if (s.startsWith("//")) return "https:" + s;      // 协议相对
  if (s.startsWith("/") && base) {
    try { return new URL(s, base).href; } catch (e) { return ""; }
  }
  return "";
}

//: 「用这条」：只是把规则**填进表单**，不落库——保存由用户自己在弹窗里决定
function useCandidate(c) {
  const field = FIELD_OF_STEP[(current.value || {}).name];
  if (!field) return false;
  markStepStale((current.value || {}).name);
  emit("applyRule", { field, rule: c.rule });
  return true;
}

//: 候选的规则文本：算法候选给 `rule`，点选候选给 `legado`/`css` 两种写法。
//: 统一在这里取，动作分派才不会把点选候选的规则写成 undefined
function candidateRule(c) {
  return String((c && (c.rule || c.legado || c.css)) || "");
}

//: 候选卡片上的动作**只在这里分派**（卡片组件不判定，也不认识业务动作）
function onCandidateAction(key, c, i = 0) {
  if (key === "use") return useCandidate({ rule: candidateRule(c) });
  if (key === "rerun") return applyCandidateAndRerun({ rule: candidateRule(c) });
  if (key === "ai") return verifyAndApplyCandidate(c, i);
  if (key === "preview") return previewCandidate(c);
  return undefined;
}

const candidateVerifying = ref("");
const candidateResults = ref({});

function candidateKey(c, i = 0) {
  return String((c && c.rule) || "") + "#" + i;
}

function candidateCard(c, i = 0) {
  const stored = candidateResults.value[candidateKey(c, i)] || {};
  const presentation = candidatePresentation(c, i);
  return {
    ...candidateView({ ...c, ...stored, kind: c.kind || c.role }, {
      intent: (want.value || {}).kind || "",
      status: stored.status || c.status || (c.verified ? "verified" : "pending"),
    }),
    presentation,
  };
}

function candidatePresentation(c, i = 0) {
  const result = candidateResults.value[candidateKey(c, i)] || c || {};
  const status = result.status || (result.verified ? "verified" :
    (result.rule_error ? "needs_engine" : "rejected"));
  if (status === "verified") {
    return { label: "可直接使用", type: "success", action: "apply", actionLabel: "应用" };
  }
  if (status === "rejected") {
    return { label: "不可用", type: "danger", action: "details", actionLabel: "查看原因" };
  }
  return { label: "需要实测", type: "warning", action: "verify", actionLabel: "验证并应用" };
}

function candidateTarget(step) {
  return ({ search: "search", explore: "explore", bookUrl: "info",
    toc: "toc", content: "content" })[step] || "";
}

function candidateQuery(step) {
  const s = current.value || {};
  // 搜索段没有 URL 时使用现有调试链的默认关键词；其他段必须使用实测页面地址。
  return String(s.url || s.query || (step === "search" ? "我" : "")).trim();
}

async function verifyAndApplyCandidate(c, i = 0) {
  if (!c) return;
  const key = candidateKey(c, i);
  const shown = candidatePresentation(c, i);
  if (shown.action === "apply") return useCandidate(c);
  if (shown.action === "details") {
    ElMessage.warning(c.note || c.engine?.reason || "这条候选未通过验证");
    return;
  }
  const field = aiField.value;
  const target = candidateTarget((current.value || {}).name || "");
  const query = candidateQuery((current.value || {}).name || "");
  if (!field || !target || !query || candidateVerifying.value) return;
  candidateVerifying.value = key;
  try {
    const result = await verifyCandidate(props.source || {}, field, c.rule, target, query);
    candidateResults.value = Object.assign({}, candidateResults.value, { [key]: result });
    if (result.status === "verified") {
      useCandidate(c);
      ElMessage.success("候选已通过本机引擎预验，正在重新调试本步");
       emit("rerunFrom", (current.value || {}).name);
    } else {
      ElMessage.warning(result.engine?.reason || result.engine?.detail || "真实引擎未通过");
    }
  } catch (e) {
    ElMessage.error("候选验证失败：" + e.message);
  } finally {
    candidateVerifying.value = "";
  }
}

function applyCandidateAndRerun(c) {
  if (!c || rerunning.value) return;
  if (!useCandidate(c)) return;
  if (canRerun.value) emit("rerunFrom", (current.value || {}).name);
}

// —— 点选：
//
// 渲染的是**我们补抓的** HTML（`pages[].html`）。页面自带的脚本**不执行**（iframe 只给
// `allow-same-origin`），点选与高亮由**父页面**注入——同源能拿到它的 document。
// 两条边界必须记住：① L2–L4 的页面上这里看不到数据（那是通道的问题，不是规则的问题）；
// ② 点出来的选择器**只是提议**，验收仍走「重新调试本步」的真引擎（AGENTS #3）。
const frameRef = ref(null);
//: 选中的元素与它的候选：`{tag, candidates: [{css, legado, why, hits, uniq, ratio}]}`
const picked = ref(null);
//: 正在预览（高亮全部命中）的那条候选
const activeCss = ref("");

//: 塞进 iframe 的 HTML。移除会把 iframe 自己导航走的 meta refresh，
//: 同时把资源基准改回当前页面 URL。否则 srcdoc 里的 `/static/...` 会继承 GUI
//: 的 origin，请求被发到本后端而不是原站，日志里就会出现本地 `/static/css/...` 404。
const frameHtml = computed(() => {
  const raw = String(pageTextRaw.value || "")
    .replace(/<meta[^>]+http-equiv=["']?refresh["']?[^>]*>/gi, "")
    .replace(/<base[^>]*>/gi, "");
  const pageUrl = String((currentPage.value && currentPage.value.url) || "").trim();
  if (!pageUrl) return raw;
  const safeUrl = pageUrl.replace(/&/g, "&amp;").replace(/"/g, "&quot;");
  const base = `<base href="${safeUrl}">`;
  return /<head(?:\s[^>]*)?>/i.test(raw)
    ? raw.replace(/(<head(?:\s[^>]*)?>)/i, `$1${base}`)
    : base + raw;
});

function frameDoc() {
  const f = frameRef.value;
  try {
    return (f && f.contentDocument) || null;
  } catch (e) {
    return null;     // 跨源拿不到（正常不该发生：srcdoc + allow-same-origin 是同源）
  }
}

function ensureFrameStyle(doc) {
  if (!doc || doc.getElementById("zc-pick-style")) return;
  const st = doc.createElement("style");
  st.id = "zc-pick-style";
  // iframe 是**独立文档**：宿主的 CSS 变量与样式表都进不来，色值只能写字面值
  // （别把它「统一」成 var(--app-*)——那样只在宿主页面生效，框选高亮会静默失效）
  st.textContent = ".zc-picked{outline:2px solid #e6a23c !important;outline-offset:1px}"
    + ".zc-hit{outline:2px dashed #409eff !important;outline-offset:1px}"
    + "html{background:#fff;color:#303133}body{margin:12px;font:14px/1.7 system-ui,-apple-system,Segoe UI,sans-serif;"
    + "word-break:break-word}img,video{max-width:100%;height:auto}pre{white-space:pre-wrap;word-break:break-word}"
    + "a{color:#409eff}";
  (doc.head || doc.documentElement).appendChild(st);
}

function clearMark(doc, cls) {
  if (!doc) return;
  doc.querySelectorAll("." + cls).forEach((n) => n.classList.remove(cls));
}

//: 当前字段里的规则在这一页上选中了什么（「改一个字符看命中集合怎么变」）
const rulePreview = ref(null);

function onFrameLoad() {
  const doc = frameDoc();
  if (!doc) return;
  ensureFrameStyle(doc);
  // 只挂一次：srcdoc 每次重载都会走 load，重复挂会让一次点击收两遍
  doc.removeEventListener("click", onFrameClick, true);
  doc.addEventListener("click", onFrameClick, true);
  // 切步/换页之后上一次的选择不适用了（页面已经不是那一页）
  picked.value = null;
  activeCss.value = "";
  applyRulePreview();
}

//: 把**当前字段的规则**在页面上画出来（剥掉末段取值动作，见 `previewCss`）。
//: 「改一个字符就能看到命中集合怎么变」——不用每次跑一遍引擎
function applyRulePreview() {
  const doc = frameDoc();
  if (!doc) return;
  clearMark(doc, "zc-hit");
  rulePreview.value = previewCss(doc, currentRule.value);
  if (!rulePreview.value) return;
  try {
    doc.querySelectorAll(rulePreview.value.css).forEach((n) => n.classList.add("zc-hit"));
  } catch (e) { /* 选择器不合法：不动 */ }
}

// 换了步骤 / 改了规则就跟着重画（在「网页视图」页签上才看得见效果）
watch(currentRule, () => { if (subTab.value === "dom") applyRulePreview(); });

//: **对数验收**（九-4）：当前字段的规则在这份 HTML 上选中几个节点。
//: 用 `DOMParser` 独立算一遍（不依赖 iframe 开没开），与「网页视图」里画出来的同源同法。
//: 对什么数：**引擎那一步报的条数**（`◇目录总数:199` 那种）——两边都算出来才谈得上验收
//: （原来那条「本地回放 vs 引擎」的比法随本地引擎退场没了，比的是同一条规则在两个页面上
//: 看到的东西，不一致就说明**材料不同**或 App 另有过滤）。
const localRuleCount = computed(() => {
  const html = pageTextRaw.value;
  if (!html || !currentRule.value) return null;
  return previewCss(parseDoc(html), currentRule.value);
});

//: 引擎那一步自己报的条数（事件流里的 `◇书籍总数:7` / `◇目录总数:199`）。
//: **只认带「总数/条数/数量/个数」的键**：`◇章节名称:第1页` 里的 1 不是条数
const engineCount = computed(() => {
  for (const n of ((current.value || {}).notes || [])) {
    const m = String(n).match(/◇[^:：]*(?:总数|条数|数量|个数)[:：]\s*(\d+)/);
    if (m) return { label: String(n).replace(/^◇/, "").split(/[:：]/)[0], n: Number(m[1]) };
  }
  return null;
});

//: 质量是「规则是否值得继续保留」的证据摘要，不是替代真引擎 verdict。
//: 本地命中、引擎实际值、去重、数量对数和选择器稳定性分开计，避免 DOM 命中自证。
const ruleQuality = computed(() => assessRuleQuality({
  rule: currentRule.value,
  preview: rulePreview.value,
  values: (current.value && current.value.values) || [],
  expectedCount: engineCount.value && engineCount.value.n,
  verdict: (current.value || {}).verdict,
  kind: (want.value || {}).kind,
}));

function qualityTagType(q) {
  return { good: "success", usable: "warning", weak: "warning", bad: "danger" }[q.key] || "info";
}

function onFrameClick(ev) {
  const doc = frameDoc();
  const el = ev.target;
  // **别让链接把 iframe 带走**：sandbox 挡得住顶层导航，挡不住 iframe 自己跳
  ev.preventDefault();
  ev.stopPropagation();
  if (!doc || !el || !el.tagName) return;
  const candidates = selectorCandidates(doc, el, {
    step: (current.value || {}).name,
    field: FIELD_OF_STEP[(current.value || {}).name],
  });
  picked.value = { tag: el.tagName.toLowerCase(), candidates };
  clearMark(doc, "zc-picked");
  el.classList.add("zc-picked");
  previewCandidate(candidates[0] || null);
}

//: 高亮一条候选的**全部命中**——「看选中效果」。改一个字符就能看到集合怎么变
function previewCandidate(c) {
  const doc = frameDoc();
  if (!doc) return;
  clearMark(doc, "zc-hit");
  activeCss.value = (c && c.css) || "";
  if (!c) return;
  try {
    doc.querySelectorAll(c.css).forEach((n) => n.classList.add("zc-hit"));
  } catch (e) { /* 选择器不合法：不动 */ }
}

// —— 第 2 层：**AI 提议 + 分层验证** ——
// 第 1 层是确定性扫描；AI 只在用户点击后提议。候选先由后端本地初筛，
// 本地无法判断的候选由「验证并应用」调用真实 JVM 引擎，不能把两种结论混在一起。
const aiLoading = ref(false);
const aiRes = ref(null);

const aiField = computed(() => FIELD_OF_STEP[(current.value || {}).name] || "");
const canSuggest = computed(() => !!currentPage.value && !!aiField.value);

//: 「有没有可用的模型」。**必须与 canSuggest 分开**：程序挑候选那趟（preselect）
//: 不依赖模型，两者合成一个会让没配模型的用户连免费那趟都拿不到（`runPreselect`
//: 正是拿 canSuggest 当门槛的）。
//:
//: 判据用 `active.api_key_set` 而不是 `active` 非空——profile 存在但 key 为空时
//: 后端 `LLMConfig.enabled` 仍是假（它等于 `bool(api_key)`），只看非空会漏掉这一档，
//: 用户点下去还是拿到「没有可用的模型」。
//:
//: 默认 true（拉到之前按可用算）：默认 false 会让按钮先灰一下，比晚一步发现更糟。
//: 拉取失败也维持 true——点了后端会给明确文案，比误禁用强。
const llmReady = ref(true);

async function refreshLLMStatus() {
  try {
    const s = await getLLMStatus();
    llmReady.value = !!(s && s.active && s.active.api_key_set);
  } catch (e) { /* 见上：保持"可用" */ }
}
//: 本次调用的 token 用量。**空对象要当没有**（`{}` 在 JS 里是真值，
//: 直接判 `v-if="aiRes.usage"` 会在没拿到用量时显示「0 tokens」）
const aiUsage = computed(() => {
  const u = (aiRes.value && aiRes.value.usage) || {};
  return u.prompt_tokens ? u : null;
});

//: **真引擎**取到的值 = 「正确的规则应当取到形似的东西」。行首的 ┌└◇ 是事件流的
//: 结构符号、不是内容，喂模型前先剥掉。
//: ⚠️ **本地回放的结果不能当基准**：那批值正是**当前这条坏规则**的产物，拿它比
//: 等于自证循环（后端 `core/repair/suggest.preselect` 的注释也这么写着）。
//: 所以这里按来源判一下——以前无条件用 `current.values`，本地结果会喂进去
const appValues = computed(() => (isEngineResult.value
  ? (current.value || {}).values || []
  : [])
  .map((v) => String(v).replace(/^[┌└◇≡⇒︾︽\s]+/, "").trim())
  .filter(Boolean).slice(0, 8));

//: DOM 大纲的起点：拿第 1 层第一个候选的首段（列表步是容器、链接步是条目，
//: 两种都能让模型看见目标附近的结构）。**留空等于从 `<html>` 起**——深于 6 层的
//: 容器那样根本走不到，而提示词要求 class 必须真实存在于大纲里。
//: 后端负责 class./id./tag. → CSS 的转换（那是回放器的活，前端不抄一份）
const focusRule = computed(
  () => String((candidates.value[0] || {}).rule || currentRule.value || "").split("@")[0],
);

//: 「程序先挑」的结果（免费那趟）。与 aiRes 分开存：一个是本地挑选的结论，
//: 一个是模型给的候选，混在一起会分不清哪条花了钱、哪条没花
const preselRes = ref(null);
const preselFor = ref("");     //: 这份结论是给哪一步算的（换步骤时的过期保护）
const preselLoading = ref(false);

function suggestBody(step) {
  return {
    html: currentPage.value.html,
    step,
    rule: currentRule.value,
    step_label: STEP_LABELS[step] || step,
    want_label: (want.value && want.value.label) || "",
    field: aiField.value,
    focus: focusRule.value,
    source_type: sourceType.value,
    app_values: appValues.value,
    candidates: candidates.value.map((c) => c.rule),
    enabled_cookie_jar: !!props.enabledCookieJar,
    diagnosis: decisionLines(decision.value),
  };
}

//: 换步骤就自动跑**免费**那趟：程序先在候选里挑一遍（拿 App 实测值当基准）。
//: 它不发模型请求、不花钱，所以可以自动；付费的那趟见 askAI
async function runPreselect() {
  if (!canSuggest.value) return;
  const step = (current.value || {}).name || "";
  preselLoading.value = true;
  preselRes.value = null;
  preselFor.value = step;
  try {
    const r = await suggestRule(Object.assign(suggestBody(step), { dry_run: true }));
    // 过期保护：等回来时用户可能已经切到别的步骤了
    if (preselFor.value === step) preselRes.value = r;
  } catch (e) {
    if (preselFor.value === step) {
      preselRes.value = { preselect: null, login_wall: false, error: "请求失败：" + e.message };
    }
  } finally {
    preselLoading.value = false;
  }
}

async function askAI() {
  if (!canSuggest.value) return;
  aiLoading.value = true;
  aiRes.value = null;
  candidateResults.value = {};
  candidateVerifying.value = "";
  const step = (current.value || {}).name || "";
  try {
    aiRes.value = await suggestRule(suggestBody(step));
  } catch (e) {
    aiRes.value = { candidates: [], llm: "error", error: "请求失败：" + e.message };
  } finally {
    aiLoading.value = false;
  }
}

//: 这一页是不是登录墙（后端判的，判定表在 core/checker）。是的话**不让点**：
//: 模型看到的不是 App 看到的那份（App 带登录态），提了也验不了、也修不对
const loginWall = computed(() => !!(preselRes.value || {}).login_wall);

// —— 五格决策：**判据在后端一处**（`core/agent_plan`：缺口唯一、fix 与 probe 分栏、
//    AI 只认合格材料），这里只提交**观测到的事实**。取不到计划时不自作判据：两套判据
//    正是这条链路要消掉的东西，那时只渲染现状与证据并说明接口不可用。
const plan = ref(null);
let planSeq = 0;          // 竞态保护：换步骤后迟到的旧响应不得覆盖新结论

function planBody() {
  const s = current.value || {};
  const finished = staleSteps.value.has(s.name);
  return {
    layer: layer.value.layer || "",
    target: { step: s.name || "", want: (want.value || {}).kind || "",
              rule_empty: !currentRule.value.trim() },
    step: {
      verdict: s.verdict || "", reason: s.reason || "", detail: s.detail || "",
      rule_error: s.rule_error || "", stale: finished,
      page_id: s.page_id || "", url: s.url || "",
      values_count: (s.values || []).length,
    },
    page: {
      present: !!currentPage.value,
      stats: pageStats.value || {},
      has_wanted: hasWanted.value === undefined ? null : hasWanted.value,
      evidence: layer.value.evidence || [],
    },
    channel: channel.value,
    replay: {
      present: !!replayResult.value,
      values_count: ((replayResult.value || {}).values || []).length,
      rule_error: (replayResult.value || {}).rule_error || "",
    },
    signals: {
      login_wall: loginWall.value,
      llm_ready: llmReady.value,
      can_suggest: canSuggest.value,
    },
    candidates: candidatesUsable.value
      ? candidates.value.map((c) => ({ rule: c.rule || "" }))
      : [],
    capabilities: { jvm_debug: true, app_debug: true },
    model_available: canSuggest.value && llmReady.value && !loginWall.value,
  };
}

async function refreshPlan() {
  const seq = ++planSeq;
  try {
    const out = await agentPlan(planBody());
    if (seq === planSeq) plan.value = out;
  } catch (e) {
    if (seq === planSeq) plan.value = null;
  }
}

// 免费、不发模型请求、不落库，所以换步骤/换页面/换规则/换回放结论都可以自动刷
watch([result, activeStep, currentPage, currentRule, replayResult, candidates,
       preselRes, llmReady], refreshPlan, { immediate: true });

//: 这一步的**五格视图模型**：判据来自上面的 `plan`，句子与按钮词由 `debugDecision` 出
const decision = computed(() => buildDecision({
  plan: plan.value,
  step: current.value || {},
  want: want.value,
  channel: channel.value,
  page: currentPage.value,
  layer: layer.value,
  stats: pageStats.value,
  replay: replayResult.value,
  quality: ruleQuality.value,
  stale: staleSteps.value.has((current.value || {}).name),
}));

// 换步骤 / 换页面就自动跑那趟**免费的**（程序先挑 + 登录墙判断）。它不发模型请求，
// 所以可以自动跑；付费的那趟只有 askAI 里有，且只由按钮点击触发
watch([result, activeStep, currentPage], () => {
  preselRes.value = null;
  aiRes.value = null;
  runPreselect();
});

async function doReplay() {
  if (!canReplay.value) return;
  replaying.value = true;
  try {
    replayResult.value = await replayStep(
      currentPage.value.html, currentRule.value,
      current.value.name, sourceType.value);
  } catch (e) {
    ElMessage.error("调试失败：" + e.message);
    replayResult.value = null;
  } finally {
    replaying.value = false;
  }
}

// 从失败步骤直接送到对应的规则页签，省掉自己翻页签找字段
function gotoRuleField(step) {
  const ruleTab = STEP_RULE_TAB[(step || {}).name];
  emit("goto", ruleTab ? { tab: "rules", ruleTab } : { tab: "basic" });
}

function focusRunEntry() {
  window.scrollTo({ top: 0, behavior: "smooth" });
}

//: 五格决策给的动作 → 现有事件。**不新造动作通道**：改源走 gotoRuleField、
//: 重验走 rerunFrom、用候选走 useCandidate（AGENTS #3：验收只走真引擎）
function onDecisionFix(fix) {
  const step = current.value;
  if (!fix || !step) return;
  if (fix.kind === "rerun") return emit("rerunFrom", step.name);
  if (fix.kind === "run_debug") return focusRunEntry();
  if (fix.kind === "apply_candidate") return useCandidate(fix.candidate);
  return gotoRuleField(step);
}

//: 取证：换通道再观测一次。**它不是解决方案**，只是把材料取回来交给上面的「解决」
function onDecisionProbe() {
  switchToApp();
}

function onDecisionGotoBasic() {
  emit("goto", "basic");
}

function loadMore() {
  renderLimit.value += RENDER_CHUNK;
}

// 标签占比：后端给的是 0~1 的比值，展示成百分比才有单位
function formatRatio(value) {
  if (value === null || value === undefined || value === "") return "";
  const ratio = Number(value);
  return Number.isFinite(ratio) ? (ratio * 100).toFixed(1) + "%" : String(value);
}

//: 复制一律走这里，且**只复制原文**：格式化视图里换行是加的、实体是展开的，
//: 拿它去写规则/正则就会以为页面上真是那样（原来那句「复制出来的就是原文」照旧成立）
async function copyRaw(text, okMsg) {
  try {
    // navigator.clipboard 只在安全上下文（https / localhost）存在。
    // 本前端开了 server.host，用户会从局域网 IP 用 http 打开——
    // 那里它是 undefined，直接调用会同步抛 TypeError，界面毫无反应。
    // 必须包在 try 里，并给出明确反馈。
    await navigator.clipboard.writeText(text);
    ElMessage.success(okMsg);
  } catch (e) {
    ElMessage.warning("复制失败，请手动选中文本");
  }
}

function copyMatched() {
  return copyRaw(matchedHtml.value, "已复制命中源码（原文）");
}

function copyPage() {
  return copyRaw(pageTextRaw.value, "已复制页面源码（原文）");
}
</script>

<template>
  <div class="debug-workbench">
    <!-- 来源必须写在标题旁：App 实测与本机引擎视觉完全一样，不标就分不清
         手里这份结果是在哪儿跑出来的 -->
    <div>
      <el-tag v-if="isEngineResult" size="small"
              :type="isAppResult ? 'success' : 'primary'">
        {{ isAppResult ? "App 实测" : "本机引擎" }}
      </el-tag>
    </div>

    <el-alert v-if="entryError" type="warning" :closable="false" show-icon
              style="margin-bottom: 10px" title="自动生成未完成">
      <span>{{ entryError }}</span>
      <el-button size="small" link type="primary" @click="focusRunEntry">
        重新调试搜索
      </el-button>
    </el-alert>
    <el-alert v-if="result && result.error" type="error" :closable="false" show-icon
              style="margin-bottom: 10px" :title="String(result.error)" />
    <el-empty v-if="!steps.length" description="没有调试结果；可先检查上面的原因或重新调试" :image-size="80" />

    <template v-else>
      <div class="debug-workspace">
        <aside class="debug-step-panel">
          <div class="debug-step-panel-title">调试步骤</div>
          <div class="debug-step-tabs">
            <!-- 高亮要跟着「实际显示的那一步」（current 在 activeStep 失效时会回退到
                 steps[0]），否则重跑后会出现「有内容、没有任何页签高亮」 -->
            <button v-for="s in steps" :key="s.name" type="button" class="debug-step-tab"
                    :class="{ active: !!current && s.name === current.name }"
                    @click="selectStep(s.name)">
              <i class="dot" :class="dotClass(s)"></i>
              <span class="debug-step-tab-label">{{ STEP_LABELS[s.name] || s.name }}</span>
              <el-tag v-if="diffOf(s.name) && diffOf(s.name).status !== 'same'"
                      size="small" :type="diffTagType(diffOf(s.name).status)"
                      class="diff-tag">{{ statusLabel(diffOf(s.name).status) }}</el-tag>
            </button>
          </div>
        </aside>
        <section class="debug-step-main">
      <!-- 与上次相比（ux-debug-loop）：重跑不清场，差异摆在一眼能看见的地方 -->
      <p v-if="diffSummary" class="muted" style="margin: 6px 0 0">{{ diffSummary }}</p>

      <!-- 五格决策卡：现状 / 解决 / 取证 / AI 补足 + 折叠的证据。
           判据只有 `utils/debugDecision.js` 一份，本组件只渲染并把动作分派回现有事件 -->
      <div ref="decisionBox">
        <StepDecision :decision="decision" :step="current || {}"
                      :step-label="STEP_LABELS[(current || {}).name] || (current || {}).name || ''"
                      :show-rerun="isEngineResult && !!current && !FIELD_OF_STEP[current.name]"
                      :can-rerun="canRerun" :rerunning="rerunning"
                      :rerun-tooltip="isAppResult
                        ? '让 App 从这一步重新调试：目录会连正文一起跑，正文只跑正文。规则改过会先问你是否推送。'
                        : '让本机引擎从这一步重新调试：目录会连正文一起跑，正文只跑正文。'"
                      :ai-loading="aiLoading"
                      @fix="onDecisionFix" @probe="onDecisionProbe" @ai="askAI"
                      @rerun="emit('rerunFrom', (current || {}).name)"
                      @goto-basic="onDecisionGotoBasic" />
      </div>

      <!-- 第 1 层：**在页面上找目标**。候选取自我们补抓的那份 HTML（纯前端算，
           不调模型、不发请求），每条都标出「选到几条 + 前几个值」——不给样本等于
           让用户再猜一次。「用这条」只填表单，验证走「重新调试本步」（App 引擎） -->
      <div v-if="candidates.length && candidatesUsable" class="candidates">
        <div class="cand-head">
          <b>在页面上找「{{ (want && want.label) || "目标" }}」</b>
          <span class="muted cand-head-hint">（「用这条」只把它填进表单；要验就点「重新调试本步」，
            那一步跑的是 App 引擎）</span>
        </div>
        <DebugCandidateCard v-for="(c, i) in candidates" :key="i"
                            :card="candidateCard(c, i)"
                            :actions="[
                              { key: 'use', label: '应用' },
                              { key: 'rerun', label: '应用并重调', type: 'success', disabled: !canRerun || rerunning },
                            ]"
                            @action="(key) => onCandidateAction(key, c, i)">
          <template #tags>
            <el-tag size="small" :type="c.count ? 'warning' : 'danger'">{{ c.count ? '需确认' : '不建议' }}</el-tag>
            <!-- 「命中 1228」与「命中 1198」在界面上都只是个数字：**去重**与占比才分得开 -->
            <el-tag v-if="c.uniq < c.count" size="small" type="warning">
              去重后 {{ c.uniq }}（{{ c.count - c.uniq }} 条重复）
            </el-tag>
          </template>
          <template #meta>
            <span>占比 {{ formatRatio(c.ratio) }}</span>
          </template>
        </DebugCandidateCard>
      </div>

      <!-- 第 2 层：**先让程序挑，挑不出来再问 AI**。
           程序那趟免费：拿 App 实测到的值当基准，看哪条候选取到的就是那批（多数情况
           一次就对上了，不用花钱）。剩下两种情况才需要模型：没有基准（不是 App 实测、
           或 App 那步本来就没取到值）与多条候选分不出高下。
           模型那趟**必须用户点**——它会花钱；而且每条候选回来都要过一遍回放器：
           验过的才显示条数样本，验不了的**显式标『只能连 App 试』**，不许伪装成已验证 -->
      <div class="ai-block">
        <div class="cand-head">
          <b>候选规则</b>
          <span class="muted">（先自动挑，挑不出来再问 AI）</span>
        </div>
        <!-- AI 的入口在决策卡里（材料不合格时整块不渲染）；这里是两种材料的落点：
             程序先挑的那趟免费，模型那趟只有用户点了才有 -->
        <p v-if="preselLoading" class="muted" style="margin: 4px 0 0">正在自动挑…</p>
        <!-- 程序挑出来了：直接把结论和依据摆出来 -->
        <template v-else-if="preselRes && preselRes.preselect && preselRes.preselect.picked">
          <DebugCandidateCard :card="candidateCard(preselRes.preselect.picked)"
                              :actions="[{ key: 'use', label: '用这条' }]"
                              @action="(key) => onCandidateAction(key, preselRes.preselect.picked)">
            <template #meta>
              <span>{{ preselRes.preselect.reason }}</span>
            </template>
          </DebugCandidateCard>
        </template>
        <!-- 挑不出来：说清是哪种挑不出来（没有基准 / 分不出高下），用户才知道该不该点 AI -->
        <p v-else-if="preselRes && preselRes.preselect" class="muted" style="margin: 6px 0 0">
          无法自动挑选：{{ preselRes.preselect.reason }}
        </p>
        <p v-if="aiRes && aiRes.error" class="muted ai-err">{{ aiRes.error }}</p>
        <p v-if="aiRes && aiRes.reason" class="muted" style="margin: 6px 0 0">
          AI 判断：{{ aiRes.reason }}
        </p>
        <!-- token 用量：一眼看出这次花了多少、前缀缓存吃到没有。
             「缓存命中」那一项只有服务端支持并返回时才显示 -->
        <p v-if="aiUsage" class="muted" style="margin: 6px 0 0">
          本次消耗 {{ aiUsage.prompt_tokens }} tokens<template
            v-if="aiUsage.prompt_cache_hit_tokens"> · 缓存命中 {{ aiUsage.prompt_cache_hit_tokens }}</template><template
            v-else-if="aiUsage.completion_tokens"> · 输出 {{ aiUsage.completion_tokens }}</template>
        </p>
        <DebugCandidateCard v-for="(c, i) in (aiRes ? aiRes.candidates : [])" :key="i"
                            :card="candidateCard(c, i)"
                            :actions="[{
                              key: 'ai',
                              label: candidatePresentation(c, i).actionLabel,
                              loading: candidateVerifying === candidateKey(c, i),
                              disabled: !!candidateVerifying,
                            }]"
                            @action="(key) => onCandidateAction(key, c, i)">
          <template #tags>
            <el-tag size="small" :type="candidatePresentation(c, i).type">
              {{ candidatePresentation(c, i).label }}
            </el-tag>
          </template>
          <template #meta>
            <span>{{ c.status === "verified" || c.verified
              ? (c.samples || []).join("  |  ")
              : (c.note || c.why || "需要真实引擎确认") }}</span>
          </template>
        </DebugCandidateCard>
      </div>

      <el-tabs v-model="subTab">
        <!-- App 事件排第一，且 App 结果下不显示「提取结果」：
             App 实测的 values 就是事件原文去掉耗时前缀，两个 tab 说的是同一件事；
             而且那份 evidence 统计（标签占比之类）是对**事件文本**算的，
             在 App 结果下没有意义。只有本地回放的 values 才是真正取到的值 -->
        <el-tab-pane v-if="events.length" name="events">
          <template #label>调试事件 ({{ events.length }})</template>
          <div v-for="(e, i) in events" :key="i" class="debug-event">{{ e.text }}</div>
        </el-tab-pane>

        <el-tab-pane v-if="!isEngineResult" label="提取结果" name="values">
          <div v-if="current" class="debug-evidence">
            <span>条数 {{ current.evidence.values_total }}</span>
            <span>字符 {{ current.evidence.chars }}</span>
            <span>中文 {{ current.evidence.cjk_chars }}</span>
            <span>块级分隔 {{ current.evidence.block_seps }}</span>
            <span>标签占比 {{ formatRatio(current.evidence.tag_ratio) }}</span>
            <span v-if="current.evidence.noise_hit">噪声命中「{{ current.evidence.noise_hit }}」</span>
          </div>
          <div v-for="(v, i) in (current ? current.values : [])" :key="i" class="debug-value">
            <div class="debug-value-idx">
              #{{ i + 1 }}（{{ v.length }} 字符）
              <!-- 值是链接时给个直接打开的入口（章节链接这类）。用 <a> 而不是
                   window.open：中键、右键复制链接这些原生行为都能用 -->
              <a v-if="openableUrl(v)" :href="openableUrl(v)" target="_blank"
                 rel="noopener noreferrer" class="val-open">打开</a>
            </div>
            <!-- 提取值经 replayer 的 text 动作把 \s+ 折成了空格，通常是一行超长文本，
                 必须和「命中源码」一样软换行，否则只能横向滚动阅读 -->
            <pre class="debug-pre debug-pre-wrap">{{ v }}</pre>
          </div>
          <el-empty v-if="current && !current.values.length"
                    description="没有取到值" :image-size="60" />
        </el-tab-pane>

        <el-tab-pane label="命中源码" name="matched">
          <p v-if="matchedFrom" class="muted" style="margin: 6px 0">
            <el-tag size="small" :type="matchedFromType">{{ matchedFrom }}</el-tag>
            <span v-if="matchedFrom === '本地调试'" style="margin-left: 6px">
              这是本地调试投影，不是 App 实测；
              跑不了 JS 规则，可能与 App 的实际命中不同
            </span>
          </p>
          <!-- 没有命中片段时按钮禁用，避免「点一下复制了空串」 -->
          <div class="toolbar">
            <el-radio-group v-model="formatSource" size="small">
              <el-radio-button :value="true">格式化</el-radio-button>
              <el-radio-button :value="false">原文</el-radio-button>
            </el-radio-group>
            <el-button size="small" :disabled="!matchedHtml" @click="copyMatched">
              复制原文
            </el-button>
          </div>
          <pre v-if="matchedHtml" class="debug-pre debug-pre-wrap">{{ matchedShown }}</pre>
          <el-empty v-else :description="matchedHint" :image-size="60" />
        </el-tab-pane>

        <el-tab-pane label="网页视图" name="dom">
          <p class="muted" style="margin: 6px 0">
            点页面上要的那块 → 下面是它的候选写法与**实测**（命中 / 去重 / 占比）。
            预览不加载站点的样式与图片；页面自带的脚本不执行——L2/L3 的页面上这里看不到数据，
            那是通道的问题，不是规则的问题。
          </p>
          <p v-if="layer.layer && layer.layer !== 'L1'" class="muted" style="margin: 6px 0">
            <el-tag size="small" type="warning">{{ layer.info.name }}</el-tag>
            <span style="margin-left: 6px">这一页的 HTML 是补抓的：{{ layer.info.action }}。</span>
          </p>
          <iframe ref="frameRef" class="pick-frame" :srcdoc="frameHtml"
                  sandbox="allow-same-origin" @load="onFrameLoad" />
          <p v-if="rulePreview" class="muted" style="margin: 6px 0 0">
            当前字段的规则 <span class="mono">{{ currentRule }}</span> 在这一页上选中
            <b>{{ rulePreview.hits }}</b> 个节点（去重 {{ rulePreview.uniq }}）——已用蓝框标出。
          </p>
          <!-- 对数验收（九-4）：这条规则在**我们抓的页面**上选中 N 个，引擎那一步报 M 个。
               一致 → 说明这条规则就是它取数用的那条；不一致 → 材料不同 / App 另有过滤，
               改规则之前先看这里 -->
          <p v-if="localRuleCount && engineCount" class="muted" style="margin: 6px 0 0">
            对数：这份页面 <b>{{ localRuleCount.hits }}</b> 个
            / 引擎那一步 <b>{{ engineCount.n }}</b> 个（{{ engineCount.label }}）
            <el-tag v-if="localRuleCount.hits === engineCount.n" size="small" type="success">
              已对数
            </el-tag>
            <el-tag v-else size="small" type="danger">数量不一致，先别改规则</el-tag>
          </p>
          <div v-if="currentRule" class="rule-quality">
             <span class="muted">规则质量</span>
             <el-tag size="small" :type="qualityTagType(ruleQuality)">
               {{ ruleQuality.label }}
             </el-tag>
             <span v-if="ruleQuality.reasons.length" class="muted">
               {{ ruleQuality.reasons.slice(0, 1).join("；") }}
             </span>
             <details v-if="ruleQuality.reasons.length > 1" class="quality-details">
               <summary>更多诊断</summary>
               <span>{{ ruleQuality.reasons.join("；") }}</span>
             </details>
           </div>
           <p v-if="currentRule && !localRuleCount" class="muted" style="margin: 6px 0 0">
            当前字段的规则在这一页上**选不中任何节点**：它可能不是选择器（`@js:` 那种），
            或者数据要渲染后才有。
          </p>
          <p v-if="picked" class="muted" style="margin: 8px 0 4px">
            选中的是 <span class="mono">&lt;{{ picked.tag }}&gt;</span>，它的候选：
          </p>
          <DebugCandidateCard v-for="(c, i) in (picked ? picked.candidates : [])" :key="i"
                              :card="candidateCard(c, i)"
                              :highlighted="c.css === activeCss"
                              :actions="[
                                { key: 'preview', label: '预览' },
                                { key: 'use', label: '用这条' },
                                { key: 'rerun', label: '应用并重调', type: 'success', disabled: !canRerun || rerunning },
                              ]"
                              @action="(key) => onCandidateAction(key, c, i)">
            <template #tags>
              <el-tag v-if="!c.legado" size="small" type="info">纯 CSS</el-tag>
            </template>
            <template #meta>
              <span>{{ c.role }} · 命中 {{ c.hits }} · 有效值 {{ c.valid }} · 空值 {{ c.empty }} · 去重 {{ c.uniq }}</span>
              <span>稳定性 {{ Math.round(c.stability * 100) }}%</span>
              <span>{{ c.why }}</span>
            </template>
          </DebugCandidateCard>
          <el-empty v-if="!picked" description="点上面的页面选一块" :image-size="60" />
        </el-tab-pane>

        <el-tab-pane label="整页源码" name="page">
          <template v-if="currentPage">
            <div class="toolbar" style="margin-bottom: 8px">
              <el-input v-model="searchKey" size="small" style="width: 200px"
                        placeholder="搜索（如 content）" clearable />
              <template v-if="hitOffsets.length">
                <el-button size="small" @click="gotoHit(activeHit - 1)">上一处</el-button>
                <el-button size="small" @click="gotoHit(activeHit + 1)">下一处</el-button>
                <!-- 被截断时用 200+ 表示，不把「前 200 处」说成全部 -->
                <span class="muted">
                  第 {{ activeHit + 1 }} / {{ hitOffsets.length }}{{ hitsTruncated ? "+" : "" }} 处
                </span>
                <span v-if="hitsTruncated" class="muted">
                  命中过多，仅索引前 {{ HIT_LIMIT }} 处
                </span>
              </template>
              <!-- 用 trim 后的值判断，与 hitOffsets 保持一致：纯空格不算搜过 -->
              <span v-else-if="searchKey.trim()" class="muted">未找到</span>
              <el-radio-group v-model="formatSource" size="small">
                <el-radio-button :value="true">格式化</el-radio-button>
                <el-radio-button :value="false">原文</el-radio-button>
              </el-radio-group>
              <el-button size="small" :disabled="!pageTextRaw" @click="copyPage">
                复制原文
              </el-button>
            </div>
            <!-- 页面来源：调试默认吃缓存，这一份 HTML 可能是**几分钟前**抓的。
                 不标出来的话，用户会把它当成刚抓的——那正是「看着正常、答的不是
                 你问的那件事」那一类问题 -->
            <p v-if="currentPage.fetched_at || currentPage.origin === 'engine'"
               style="margin: 0 0 8px">
              <span v-if="currentPage.fetched_at" class="muted">
                页面抓取于 {{ currentPage.fetched_at }}
              </span>
              <!-- **这一页是谁取回来的**：引擎给的是 **App 手上那份**（过了它的 JS /
                   cookie / UA），我们补抓的是另一条 HTTP 栈、没有登录态——两份可能不是
                   同一页，而「看源码改规则」正建立在这份材料上 -->
              <el-tag size="small" style="margin-left: 6px"
                      :type="currentPage.origin === 'engine' ? 'primary' : 'info'">
                {{ currentPage.origin === "engine" ? "本机引擎取回" : "我们抓到的" }}
              </el-tag>
              <el-tag v-if="currentPage.origin !== 'engine'" size="small"
                      style="margin-left: 4px"
                      :type="currentPage.cached ? 'warning' : 'success'">
                {{ currentPage.cached ? "来自缓存" : "本次新抓" }}
              </el-tag>
            </p>
            <el-alert v-if="currentPage.truncated" type="warning" :closable="false"
                      show-icon style="margin-bottom: 8px"
                      :title="'原文 ' + currentPage.len + ' 字符，已截断到前 '
                              + currentPage.html.length + ' 字符'" />
            <pre class="debug-pre debug-pre-wrap"><template
              v-for="(seg, i) in segments" :key="i"><span
              v-if="!seg.hit">{{ seg.text }}</span><mark
              v-else :id="'debug-hit-' + seg.index"
              :class="{ 'debug-hit-active': seg.index === activeHit }">{{ seg.text }}</mark></template></pre>
            <div v-if="hasMore" class="toolbar" style="margin-top: 8px">
              <el-button size="small" @click="loadMore">
                加载更多（已渲染 {{ renderLimit }} / {{ pageText.length }} 字符）
              </el-button>
            </div>
          </template>
          <el-empty v-else description="这一步没有抓到页面" :image-size="60" />
        </el-tab-pane>

      </el-tabs>
        </section>
      </div>
    </template>
  </div>
</template>

<style scoped>
/* 工作台专属令牌挂在组件根节点：调试页换配色不外溢到列表、回收站和任务抽屉。
   底色是**有语义的区分**（候选=确定性扫描、AI=模型提议、诊断=需要注意），
   不是随手挑的颜色，改之前先看它对应哪一类材料 */
.debug-workbench {
  --debug-panel-bg: var(--app-surface);
  --debug-panel-border: var(--app-border-light);
  --debug-surface-muted: var(--el-fill-color-lighter, #fafafa);
  --debug-candidate-bg: #f4f8ff;
  --debug-candidate-border: #d9ecff;
  --debug-ai-bg: #f7f4ff;
  --debug-ai-border: #e2d9ff;
}

.candidates {
  margin: var(--app-space-2) 0;
  padding: var(--app-space-2) 10px;
  background: var(--debug-candidate-bg);
  border: 1px solid var(--debug-candidate-border);
  border-radius: var(--app-radius-sm);
}
/* AI 那块与「在页面上找目标」同构，但底色分得开：一个是确定性扫描，
   一个是模型提议（而且**验不了的会出现在这里**） */
.ai-block {
  margin: var(--app-space-2) 0;
  padding: var(--app-space-2) 10px;
  background: var(--debug-ai-bg);
  border: 1px solid var(--debug-ai-border);
  border-radius: var(--app-radius-sm);
}
.ai-err { margin: 6px 0 0; color: #b88230; }
.cand-head { margin-bottom: 4px; }
.cand-head-hint { margin-left: 4px; }
.val-open { margin-left: 6px; font-size: 12px; }
.debug-workspace { display: flex; gap: 14px; align-items: flex-start; }
.debug-step-panel {
  flex: 0 0 190px;
  position: sticky;
  top: 0;
  padding: var(--app-space-3);
  background: var(--debug-panel-bg);
  border: 1px solid var(--debug-panel-border);
  border-radius: var(--app-radius-lg);
}
.debug-step-panel-title {
  margin: 0 2px var(--app-space-2);
  color: var(--app-text);
  font-size: 13px;
  font-weight: 600;
}
.debug-step-tabs { display: flex; flex-direction: column; gap: 5px; }
.debug-step-tab {
  display: flex; align-items: center; gap: 7px; width: 100%; min-width: 0;
  padding: var(--app-space-2) 9px; border: 1px solid transparent; border-radius: var(--app-radius-md);
  background: transparent; color: var(--app-text-secondary);
  font: inherit; font-size: 13px; text-align: left; cursor: pointer;
}
.debug-step-tab:hover { background: var(--el-fill-color-light, #f5f7fa); }
.debug-step-tab.active {
  border-color: var(--el-color-primary-light-5, #a0cfff);
  background: var(--el-color-primary-light-9, #ecf5ff);
  color: var(--app-primary);
}
.debug-step-tab-label { min-width: 0; flex: 1 1 auto; }
.debug-step-main { min-width: 0; flex: 1 1 auto; }
/* 原样渲染：App 给的行首已带对齐的 [mm:ss.SSS]，再叠一层我们自己量的耗时
   就是两套时间戳，反而更难读。pre-wrap 保住行内空格 */
.debug-event {
  padding: 1px 0;
  font-family: Consolas, Monaco, monospace; font-size: 12px;
  line-height: 1.6;
  white-space: pre-wrap; word-break: break-all;
}
.debug-evidence {
  display: flex; gap: 14px; flex-wrap: wrap;
  color: var(--app-text-muted); font-size: 12px; margin-bottom: var(--app-space-2);
}
.debug-value { margin-bottom: 10px; }
.debug-value-idx { color: var(--app-text-muted); font-size: 12px; margin-bottom: 2px; }
.debug-pre {
  font-family: Consolas, Monaco, monospace; font-size: 12px;
  background: var(--app-bg); padding: var(--app-space-2); border-radius: var(--app-radius-sm);
  max-height: 52vh; overflow: auto; margin: 0;
}
.debug-pre-wrap { white-space: pre-wrap; word-break: break-all; }
.debug-hit-active { outline: 2px solid var(--app-status-fail); }
.rule-quality {
  display: flex; align-items: center; flex-wrap: wrap; gap: 6px;
  margin: 6px 0; padding: 6px var(--app-space-2);
  border: 1px solid var(--app-border-light);
  border-radius: var(--app-radius-sm); background: var(--debug-surface-muted);
  font-size: 12px;
}
.quality-metric { color: var(--app-text-muted); }
.quality-details, .cand-details { color: var(--app-text-muted); font-size: 12px; }
.quality-details summary, .cand-details summary { cursor: pointer; color: var(--app-primary); }

@media (max-width: 720px) {
  .debug-workspace { display: block; }
  .debug-step-panel { position: static; padding: 8px; margin-bottom: 10px; }
  .debug-step-panel-title { margin-bottom: 6px; }
  .debug-step-tabs {
    flex-direction: row; overflow-x: auto; gap: 6px; padding-bottom: 2px;
  }
  .debug-step-tab { flex: 0 0 auto; width: auto; min-width: 86px; }
  .debug-step-tab-label { flex: 0 0 auto; }
  /* 候选标题那句提示在窄屏另起一行：跟在标题后面换行只会剩一个孤字（「擎)」） */
  .cand-head-hint { display: block; margin: 2px 0 0; }
}
</style>

<!-- 抽屉里我们自己的元素按 AGENTS #15 写在**不带 scoped** 的块里、用前缀限定：
     这里是 `.pick-`（点选页签那个 iframe）。跨组件共用的才上 frontend/src/styles.css -->
<style>
.pick-frame {
  width: 100%;
  height: 58vh;
  border: 1px solid var(--app-border-light);
  border-radius: var(--app-radius-sm);
  background: var(--app-surface);      /* 页面大多假设白底 */
}
</style>

<!-- 抽屉 teleport 到 body，内部结构的样式只能写在这里（AGENTS #15）。
     .diff-tag 跟着「与上次相比」的标记走，小到不挤占页签 -->
<style>
.debug-step-tab .diff-tag {
  margin-left: 4px;
  transform: scale(0.85);
}
</style>
