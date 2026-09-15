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

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  result: { type: Object, default: null },
  initialStep: { type: String, default: "" },
});
const emit = defineEmits(["update:modelValue", "goto"]);

const visible = computed({
  get: () => props.modelValue,
  set: (v) => emit("update:modelValue", v),
});

// explore 是发现链路的产出步（key 带 `发现::` 时后端才产出它）
const STEP_LABELS = { search: "搜索", explore: "发现", bookUrl: "详情链接", toc: "目录", content: "正文" };
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
  subTab.value = "values";
  renderLimit.value = RENDER_CHUNK;
  searchKey.value = "";
  activeHit.value = 0;
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
  subTab.value = "values";
  renderLimit.value = RENDER_CHUNK;
  searchKey.value = "";
  activeHit.value = 0;
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
  const text = (current.value && current.value.matched_html) || "";
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
  <el-drawer v-model="visible" title="试跑调试" size="72%" destroy-on-close>
    <el-empty v-if="!steps.length" description="没有试跑结果" :image-size="80" />

    <template v-else>
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
        <el-tab-pane label="提取结果" name="values">
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
            规则<b>选中的那块 DOM</b> 的 outerHTML——改规则时看这个，
            比在整页里猜要快得多。
          </p>
          <!-- 没有命中片段时按钮禁用，避免「点一下复制了空串」 -->
          <div class="toolbar">
            <el-button size="small" :disabled="!(current && current.matched_html)"
                       @click="copyMatched">复制</el-button>
          </div>
          <pre v-if="current && current.matched_html"
               class="debug-pre debug-pre-wrap">{{ current.matched_html }}</pre>
          <el-empty v-else description="没有命中片段" :image-size="60" />
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
