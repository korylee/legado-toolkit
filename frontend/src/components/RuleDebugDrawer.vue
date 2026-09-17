<script setup>
// 试跑调试抽屉：把每一步的证据摊开。
// 三个子页签：提取结果（全文）/ 命中源码 / 整页源码。
//
// 设计取舍：**不对 HTML 注入换行**。
// 虽然注入后按行渲染更省事，但用户会把这段源码复制去改规则——
// 改过字符的源码会与真实响应不一致。改为 pre-wrap 软换行 +
// 按字符偏移分片渲染，保证「复制出来的就是原文」。
import { ref, computed, watch, nextTick } from "vue";
import { ElMessage } from "element-plus";

import { replayStep } from "../api/rules";
// 步骤名 → 中文的**唯一**一份（编辑弹窗共用），别再在本组件里写第二份
import { STEP_LABELS } from "../utils/steps";

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  result: { type: Object, default: null },
  initialStep: { type: String, default: "" },
  //: 当前表单里每一步的规则（steps[].name → 规则字符串），用于「重放本步」
  rules: { type: Object, default: () => ({}) },
  sourceType: { type: Number, default: 0 },
});
const emit = defineEmits(["update:modelValue", "goto"]);

const visible = computed({
  get: () => props.modelValue,
  set: (v) => emit("update:modelValue", v),
});

// explore 是发现链路的产出步（key 带 `发现::` 时后端才产出它）
//: 每次渲染的字符数。整页 HTML 可能 100 万字符，全量进 DOM 会卡
const RENDER_CHUNK = 20000;
//: 搜索最多索引的命中数。整页 HTML 里搜 div / class 必然远超此数，
//: 超限时必须显式告知，否则计数器会把「前 200 处」说成全部
const HIT_LIMIT = 200;

const activeStep = ref("");
const subTab = ref("values");
const renderLimit = ref(RENDER_CHUNK);
const searchKey = ref("");
const activeHit = ref(0);

const steps = computed(() => (props.result && props.result.steps) || []);
const pages = computed(() => (props.result && props.result.pages) || []);

//: 结果来源。App 实测（走 App 的调试 WS）与本地回放（我们的离线引擎）可信度
//: 差很多——本地只判「非空/不报错」且跑不了 JS 规则。**必须显式标出来**：
//: 两者的三态视觉完全一样，不标就分不清哪份该信
const isAppResult = computed(() => (props.result || {}).source === "app");
//: App 推来的原始事件流。steps[].values 是它去掉耗时前缀后的段内文本，
//: 排查「哪一步慢」「App 到底推了什么」只能看这里
const events = computed(() => (props.result && props.result.events) || []);
//: 默认子页签：App 结果看事件流，本地回放看提取结果（那两个 tab 各自只在
//: 对应的结果下出现，选错会是一片空白）
const defaultSubTab = computed(
  () => (isAppResult.value && events.value.length ? "events" : "values"));

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

// 三态圆点：fail 红 / unknown 灰 / pass 且有附注 黄 / 纯 pass 绿
function dotClass(s) {
  if (!s) return "";
  if (s.verdict === "fail") return "err";
  if (s.verdict === "unknown") return "unknown";
  return s.has_notes ? "warn" : "ok";
}
function tagType(s) {
  if (!s) return "info";
  if (s.verdict === "fail") return "danger";
  if (s.verdict === "unknown") return "info";
  return s.has_notes ? "warning" : "success";
}
const VERDICT_TEXT = { pass: "通过", fail: "失败", unknown: "无法判定" };
function verdictText(s) {
  return (s && VERDICT_TEXT[s.verdict]) || "";
}

// 搜索在**完整原文**上做（纯字符串扫描，结果不进 DOM），最多记 HIT_LIMIT 处
const hitOffsets = computed(() => {
  const key = searchKey.value.trim();
  const html = (currentPage.value && currentPage.value.html) || "";
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
  const html = (currentPage.value && currentPage.value.html) || "";
  if (!key || !html) return false;
  const last = offsets[offsets.length - 1];
  return html.indexOf(key, last + Math.max(1, key.length)) >= 0;
});

// 只渲染前 renderLimit 个字符，避免百万字符全量进 DOM
const headText = computed(() => {
  const html = (currentPage.value && currentPage.value.html) || "";
  return html.slice(0, renderLimit.value);
});
const hasMore = computed(() => {
  const html = (currentPage.value && currentPage.value.html) || "";
  return renderLimit.value < html.length;
});

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

watch(() => props.modelValue, (show) => {
  if (!show) return;
  activeStep.value = props.initialStep || (steps.value[0] && steps.value[0].name) || "";
  subTab.value = defaultSubTab.value;
  renderLimit.value = RENDER_CHUNK;
  searchKey.value = "";
  activeHit.value = 0;
  replayResult.value = null;
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
}

//: 当前步骤的规则（取自表单，所以改完规则就能立刻重放看效果）
const currentRule = computed(
  () => (props.rules || {})[(current.value || {}).name] || "",
);
//: 重放要同时满足：这一步对应一个页面的 HTML、这一步有规则可回放、
//: 且这一步确实是一条规则步骤（explore 的规则结构不同，不给重放）
const canReplay = computed(
  () => !!currentPage.value && !!currentRule.value
    && !!STEP_RULE_TAB[(current.value || {}).name],
);

//: 「命中源码」要显示的那段 DOM。两个来源：
//:   - 本地试跑（`/rules/chain`）：结果里直接带 `matched_html`
//:   - **连 App 调试**：App 只推文本、不给 DOM（`core/app_debug.py` 里根本没有
//:     hits），所以拿**我们补抓的页面本地回放一遍**算出来。
//:
//: 原来这个 tab 只读 `current.matched_html`，于是它在唯一的调试入口下**永远是空的**，
//: 而旁边的文案还写着「规则选中的那块 DOM」——那是句假话。
//:
//: 顺带说一句用途：本地回放结果正是「让 AI 改规则」要喂给模型的东西（规则 + 它
//: 选中的 DOM），所以两者摆在同一屏上。
const matchedFrom = computed(() => {
  if ((current.value || {}).matched_html) return "本地试跑";
  return replayResult.value ? "本地回放" : "";
});
const matchedHtml = computed(() => (current.value || {}).matched_html
  || (replayResult.value || {}).matched_html || "");
const matchedHint = computed(() => {
  if (!currentPage.value) return "这一步没抓到页面（App 只推文本，页面是补抓来的）";
  if (!canReplay.value) return "这一步没有可回放的规则";
  if (replayResult.value) {
    // 回放不了（JS / xpath 等）时 `rule_error` 就是原因，别笼统说「没有命中」
    return (replayResult.value.rule_error || replayResult.value.detail
            || "这条规则在这份页面上没有选中任何 DOM");
  }
  return "正在读取…";
});

//: 进这个 tab 就自动回放一次：空着的 tab 让人以为功能坏了（原来就是这样）
watch([subTab, activeStep], () => {
  if (subTab.value !== "matched") return;
  if ((current.value || {}).matched_html) return;   // 本地试跑的结果里已经有了
  if (canReplay.value) doReplay();
});

async function doReplay() {
  if (!canReplay.value) return;
  replaying.value = true;
  try {
    replayResult.value = await replayStep(
      currentPage.value.html, currentRule.value,
      current.value.name, props.sourceType);
  } catch (e) {
    ElMessage.error("重放失败: " + e.message);
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

function loadMore() {
  renderLimit.value += RENDER_CHUNK;
}

// 标签占比：后端给的是 0~1 的比值，展示成百分比才有单位
function formatRatio(value) {
  if (value === null || value === undefined || value === "") return "";
  const ratio = Number(value);
  return Number.isFinite(ratio) ? (ratio * 100).toFixed(1) + "%" : String(value);
}

async function copyMatched() {
  const text = matchedHtml.value;
  try {
    // navigator.clipboard 只在安全上下文（https / localhost）存在。
    // 本前端开了 server.host，用户会从局域网 IP 用 http 打开——
    // 那里它是 undefined，直接调用会同步抛 TypeError，界面毫无反应。
    // 必须包在 try 里，并给出明确反馈。
    await navigator.clipboard.writeText(text);
    ElMessage.success("已复制命中源码");
  } catch (e) {
    ElMessage.warning("复制失败，请手动选中文本");
  }
}
</script>

<template>
  <el-drawer v-model="visible" size="72%" destroy-on-close>
    <!-- 来源必须写在标题旁：App 实测与本地回放的三态视觉完全一样，
         不标就分不清手里这份结果该信到什么程度 -->
    <template #header>
      <span>试跑调试</span>
      <el-tag size="small" :type="isAppResult ? 'success' : 'info'"
              style="margin-left: 8px">
        {{ isAppResult ? "App 实测" : "本地回放 · 仅供参考" }}
      </el-tag>
    </template>

    <el-empty v-if="!steps.length" description="没有试跑结果" :image-size="80" />

    <template v-else>
      <el-alert v-if="!isAppResult" type="warning" :closable="false" show-icon
                style="margin-bottom: 10px"
                title="本地回放只判「取到值 / 不报错」，且跑不了 JS 规则——与 App 的真实行为可能有偏差。要确认请用「连 App 调试」。" />
      <div class="debug-step-tabs">
        <!-- 高亮要跟着「实际显示的那一步」（current 在 activeStep 失效时会回退到
             steps[0]），否则重跑后会出现「有内容、没有任何页签高亮」 -->
        <span v-for="s in steps" :key="s.name" class="debug-step-tab"
              :class="{ active: !!current && s.name === current.name }" @click="selectStep(s.name)">
          <i class="dot" :class="dotClass(s)"></i>{{ STEP_LABELS[s.name] || s.name }}
        </span>
      </div>

      <div v-if="current" class="debug-head">
        <el-tag size="small" :type="tagType(current)">{{ verdictText(current) }}</el-tag>
        <span class="mono muted grow">{{ current.url }}</span>
        <!-- 失败就直接把人送到对应的规则页签，省掉自己翻页签找字段 -->
        <el-button v-if="current.verdict === 'fail'" size="small" type="primary" plain
                   @click="gotoRuleField(current)">去改规则</el-button>
        <!-- 用已抓到的 HTML 重放，**不发网络请求**：改完规则立刻看判定变没变 -->
        <el-button size="small" :loading="replaying" :disabled="!canReplay"
                   @click="doReplay">用本页重放</el-button>
      </div>

      <div v-if="replayResult" class="replay-box">
        <el-tag size="small" :type="tagType(replayResult)">{{ verdictText(replayResult) }}</el-tag>
        <span class="muted">取到 {{ (replayResult.values || []).length }} 条</span>
        <span v-if="replayResult.reason" class="muted">· {{ replayResult.reason }}</span>
        <span v-if="isAppResult" class="muted">
          · 这份重放跑在我们抓的 HTML 上，不是 App 所见
        </span>
        <el-button link size="small" @click="replayResult = null">关闭</el-button>
      </div>

      <p v-if="current && current.reason" class="debug-reason">{{ current.reason }}</p>
      <p v-if="current && current.rule_error" class="debug-reason">
        规则无法离线回放：{{ current.rule_error }}
      </p>
      <ul v-if="current && current.notes && current.notes.length" class="debug-notes">
        <li v-for="(n, i) in current.notes" :key="i">
          {{ n }}
          <el-button v-if="n.indexOf('bookSourceType') >= 0" size="small" link
                     type="primary" @click="emit('goto', 'basic')">
            去改类型
          </el-button>
        </li>
      </ul>

      <el-tabs v-model="subTab">
        <!-- App 事件排第一，且 App 结果下不显示「提取结果」：
             App 实测的 values 就是事件原文去掉耗时前缀，两个 tab 说的是同一件事；
             而且那份 evidence 统计（标签占比之类）是对**事件文本**算的，
             在 App 结果下没有意义。只有本地回放的 values 才是真正取到的值 -->
        <el-tab-pane v-if="events.length" name="events">
          <template #label>App 事件 ({{ events.length }})</template>
          <p class="muted" style="margin: 6px 0">
            App 推来的原始事件流，行首的 <span class="mono">[mm:ss.SSS]</span>
            是 App 自己记的相对耗时。
          </p>
          <div v-for="(e, i) in events" :key="i" class="debug-event">{{ e.text }}</div>
        </el-tab-pane>

        <el-tab-pane v-if="!isAppResult" label="提取结果" name="values">
          <div v-if="current" class="debug-evidence">
            <span>条数 {{ current.evidence.values_total }}</span>
            <span>字符 {{ current.evidence.chars }}</span>
            <span>中文 {{ current.evidence.cjk_chars }}</span>
            <span>块级分隔 {{ current.evidence.block_seps }}</span>
            <span>标签占比 {{ formatRatio(current.evidence.tag_ratio) }}</span>
            <span v-if="current.evidence.noise_hit">噪声命中「{{ current.evidence.noise_hit }}」</span>
          </div>
          <p class="muted" style="margin: 6px 0">
            这里是规则<b>实际取到的值</b>。正文规则通常只有 1 条、就是全文。
          </p>
          <div v-for="(v, i) in (current ? current.values : [])" :key="i" class="debug-value">
            <div class="debug-value-idx">#{{ i + 1 }}（{{ v.length }} 字符）</div>
            <!-- 提取值经 replayer 的 text 动作把 \s+ 折成了空格，通常是一行超长文本，
                 必须和「命中源码」一样软换行，否则只能横向滚动阅读 -->
            <pre class="debug-pre debug-pre-wrap">{{ v }}</pre>
          </div>
          <el-empty v-if="current && !current.values.length"
                    description="没有取到值" :image-size="60" />
        </el-tab-pane>

        <el-tab-pane label="命中源码" name="matched">
          <p class="muted" style="margin: 6px 0">
            当前规则在这份页面上<b>选中了哪块 DOM</b>——改规则时看这个，
            比在整页里猜要快得多。
          </p>
          <p v-if="matchedFrom" class="muted" style="margin: 6px 0">
            <el-tag size="small" :type="matchedFrom === '本地试跑' ? 'success' : 'warning'">
              {{ matchedFrom }}
            </el-tag>
            <span v-if="matchedFrom === '本地回放'" style="margin-left: 6px">
              跑不了 JS 规则，可能与 App 的实际命中不同
            </span>
          </p>
          <!-- 没有命中片段时按钮禁用，避免「点一下复制了空串」 -->
          <div class="toolbar">
            <el-button size="small" :loading="replaying" :disabled="!canReplay"
                       @click="doReplay">重新提取</el-button>
            <el-button size="small" :disabled="!matchedHtml" @click="copyMatched">
              复制
            </el-button>
          </div>
          <pre v-if="matchedHtml" class="debug-pre debug-pre-wrap">{{ matchedHtml }}</pre>
          <el-empty v-else :description="matchedHint" :image-size="60" />
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
            </div>
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
                加载更多（已渲染 {{ renderLimit }} / {{ currentPage.html.length }} 字符）
              </el-button>
            </div>
          </template>
          <el-empty v-else description="这一步没有抓到页面" :image-size="60" />
        </el-tab-pane>

      </el-tabs>
    </template>
  </el-drawer>
</template>

<style scoped>
.debug-step-tabs { display: flex; gap: 4px; margin-bottom: 12px; flex-wrap: wrap; }
.debug-step-tab {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 4px 10px; border-radius: 4px; cursor: pointer;
  border: 1px solid #dcdfe6; font-size: 13px;
}
.debug-step-tab.active { border-color: #409eff; color: #409eff; }
.debug-head { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
.replay-box {
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  padding: 6px 10px; margin: 4px 0 8px;
  background: #f0f9eb; border: 1px solid #e1f3d8; border-radius: 4px;
  font-size: 13px;
}
/* 原样渲染：App 给的行首已带对齐的 [mm:ss.SSS]，再叠一层我们自己量的耗时
   就是两套时间戳，反而更难读。pre-wrap 保住行内空格 */
.debug-event {
  padding: 1px 0;
  font-family: Consolas, Monaco, monospace; font-size: 12px;
  line-height: 1.6;
  white-space: pre-wrap; word-break: break-all;
}
.debug-reason { color: #f56c6c; margin: 4px 0; }
.debug-notes { color: #e6a23c; margin: 4px 0; padding-left: 18px; line-height: 1.7; }
.debug-evidence {
  display: flex; gap: 14px; flex-wrap: wrap;
  color: #909399; font-size: 12px; margin-bottom: 8px;
}
.debug-value { margin-bottom: 10px; }
.debug-value-idx { color: #909399; font-size: 12px; margin-bottom: 2px; }
.debug-pre {
  font-family: Consolas, Monaco, monospace; font-size: 12px;
  background: #f5f7fa; padding: 8px; border-radius: 4px;
  max-height: 52vh; overflow: auto; margin: 0;
}
.debug-pre-wrap { white-space: pre-wrap; word-break: break-all; }
.debug-hit-active { outline: 2px solid #f56c6c; }
</style>
