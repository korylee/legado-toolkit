<!-- 材料文本查看器：**唯一一份**（调试工作台与证据面板都用它）。
     只负责把一段材料摆出来：格式化/原文切换、分块渲染 + 加载更多、页内搜索与高亮、
     复制原文。判据全在 `utils/materialView`（分块窗口 / 命中索引 / 切片）与
     `utils/htmlView`（格式化只改显示，**复制永远是原文**）。

     两件必须记住的：
       - 高亮位置按**显示文本**找、处数按**原文**数；两者不等时如实说，不假装对齐；
       - 换材料要**重挂**（宿主给 `:key`）：搜索词与渲染窗口是这次材料的临时态，
         跨材料留着只会让人以为搜的是新那份。 -->
<script setup>
import { computed, ref, nextTick, watch } from "vue";
import { ElMessage } from "element-plus";

import { HIT_LIMIT, findHits, segmentsOf, viewWindow } from "../utils/materialView";
import { MATERIAL_CHUNK, formatMaterial } from "../utils/htmlView";

const props = defineProps({
  //: 材料原文（命中片段 / 页面原文 / 大纲…由宿主决定取哪一份）
  raw: { type: String, default: "" },
  //: 要不要页内搜索。材料很长时它是主要导航手段，短材料上可以关掉
  searchable: { type: Boolean, default: true },
  searchPlaceholder: { type: String, default: "搜索" },
  //: 没有材料时显示的话（宿主知道「为什么没有」，这里不编）
  emptyText: { type: String, default: "没有可看的材料" },
});

const formatted = ref(true);
const renderLimit = ref(MATERIAL_CHUNK);
const searchKey = ref("");
const activeHit = ref(0);

//: 命中在**原文**上数（覆盖整份材料，不只是已渲染的一段）
const hits = computed(() => findHits(props.raw, searchKey.value));
//: 渲染窗口：先截断，再格式化
const window_ = computed(() => viewWindow(props.raw, renderLimit.value));
//: 屏幕上显示的那份（复制与搜索计数都按原文，这里只用于显示）
const displayText = computed(() => (formatted.value
  ? formatMaterial(window_.value.head) : window_.value.head));
//: 高亮位置在**显示文本**上找：格式化会改偏移，拿原文偏移切片必然错位
const displayHits = computed(() => findHits(displayText.value, searchKey.value).offsets);
const segments = computed(() =>
  segmentsOf(displayText.value, displayHits.value, searchKey.value));
//: 窗口里按原文口径应有多少处：与显示口径不等就说明高亮不全（如实说）
const hitsInWindow = computed(() =>
  hits.value.offsets.filter((off) => off < window_.value.head.length).length);
const hitsMisaligned = computed(() => !!searchKey.value.trim()
  && hitsInWindow.value !== displayHits.value.length);

//: 换材料**不重挂**（搜索词要跟着走：从「命中片段」切到「大纲」还要搜同一条选择器），
//: 只把渲染窗口与当前命中复位——否则窗口停在上一次的「加载更多」，位置也没意义
watch(() => props.raw, () => {
  renderLimit.value = MATERIAL_CHUNK;
  activeHit.value = 0;
});

//: 换搜索词回到第一处，否则会停在上一个词的第 N 处
function onSearch() {
  activeHit.value = 0;
}

function scrollToActive() {
  const el = document.getElementById("material-hit-" + activeHit.value);
  if (el) el.scrollIntoView({ block: "center", behavior: "smooth" });
}

/**
 * 宿主用：把某个词灌进搜索框并定位第一处（「点规则框 → 看页面上的选择器」那条）。
 * 暴露成方法而不是 prop：同一个词连点两次也要重新定位，prop 值没变不会触发。
 */
function focusOn(term) {
  searchKey.value = String(term || "");
  activeHit.value = 0;
  nextTick(scrollToActive);
}
defineExpose({ focusOn });

function gotoHit(step) {
  const total = hits.value.offsets.length;
  if (!total) return;
  activeHit.value = ((activeHit.value + step) % total + total) % total;
  // 命中在窗口之外：先把窗口撑到它那里，再滚过去
  const off = hits.value.offsets[activeHit.value];
  if (off + MATERIAL_CHUNK > renderLimit.value) renderLimit.value = off + MATERIAL_CHUNK;
  nextTick(scrollToActive);
}

function loadMore() {
  renderLimit.value += MATERIAL_CHUNK;
}

async function copyRaw() {
  const text = String(props.raw || "");
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
    ElMessage.success("已复制原文");
  } catch (e) {
    ElMessage.warning("复制失败：" + ((e && e.message) || e));
  }
}
</script>

<template>
  <div class="mv">
    <div class="mv-bar">
      <template v-if="searchable">
        <el-input
          v-model="searchKey"
          size="small"
          style="width: 200px"
          :placeholder="searchPlaceholder"
          clearable
          @update:model-value="onSearch"
        />
        <template v-if="hits.offsets.length">
          <el-button size="small" @click="gotoHit(-1)">上一处</el-button>
          <el-button size="small" @click="gotoHit(1)">下一处</el-button>
          <span class="muted">
            第 {{ activeHit + 1 }} / {{ hits.offsets.length
            }}{{ hits.truncated ? "+" : "" }} 处
          </span>
        </template>
        <span v-else-if="searchKey.trim()" class="muted">未找到</span>
      </template>
      <el-radio-group v-model="formatted" size="small">
        <el-radio-button :value="true">格式化</el-radio-button>
        <el-radio-button :value="false">原文</el-radio-button>
      </el-radio-group>
      <el-button size="small" :disabled="!raw" @click="copyRaw">复制原文</el-button>
      <span v-if="raw" class="muted">{{ raw.length }} 字符</span>
    </div>

    <!-- 两处数不等时如实说：不假装高亮就是全部 -->
    <p v-if="hitsMisaligned" class="mv-note">
      已渲染的这段里有 {{ displayHits.length }} 处标出来了，按原文数应为
      {{ hitsInWindow }} 处（格式化会改字符位置）：点「加载更多」或换关键词再看。
    </p>

    <pre v-if="raw" class="mv-pre"><template
      v-for="(seg, i) in segments" :key="i"><span
      v-if="!seg.hit">{{ seg.text }}</span><mark
      v-else :id="'material-hit-' + seg.index"
      :class="{ 'mv-hit-active': seg.index === activeHit }">{{ seg.text }}</mark></template></pre>
    <p v-else class="mv-empty">{{ emptyText }}</p>

    <div v-if="window_.hasMore" class="mv-bar" style="margin-top: 6px">
      <el-button size="small" @click="loadMore">
        加载更多（已渲染 {{ renderLimit }} / {{ raw.length }} 字符）
      </el-button>
      <span v-if="hits.truncated" class="muted">命中过多，仅索引前 {{ HIT_LIMIT }} 处</span>
    </div>
  </div>
</template>

<style scoped>
.mv-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 6px;
}
.mv-note {
  margin: 0 0 8px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.mv-pre {
  margin: 0;
  padding: 10px;
  max-height: 460px;
  overflow: auto;
  background: #fff;
  border: 1px solid var(--el-border-color-lighter, #ebeef5);
  border-radius: 4px;
  font: 12px/1.6 ui-monospace, SFMono-Regular, Consolas, monospace;
  color: var(--el-text-color-regular);
  white-space: pre-wrap;
  word-break: break-all;
}
.mv-hit-active { background: #ffd666; }
.mv-empty {
  margin: 0;
  color: var(--el-text-color-secondary);
  font-size: 13px;
}
</style>