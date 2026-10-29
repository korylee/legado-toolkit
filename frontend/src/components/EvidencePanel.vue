<!-- 这一步的证据（编辑页就地看）：实测摘要 / 结构化列表 / 命中片段 / 页面原文。
     **判据全在 utils**：摘要与列表来自 `debugStepResult`（步骤渲染的唯一一份），
     文本展示走 `MaterialViewer`（与调试工作台同一个查看器）。

     为什么单独成件：编辑页与完整调试要看到的是**同一份材料**（同一次任务的结果体，
     由 `debugResultCache` 取）。这里只负责把那份材料摆出来，不新增任何判定——
     层、材料种类、规则是否通过，判据分别在 `page_layer` / `debugMaterial` / 引擎。 -->
<script setup>
import { computed, ref } from "vue";

import MaterialViewer from "./MaterialViewer.vue";
import { stepResult, structuredList } from "../utils/debugStepResult";

const props = defineProps({
  //: 结果体里的这一步（`values` / `structured` / `matched_html` / `verdict`）
  step: { type: Object, default: null },
  //: 这一步的页面（`{id, url, html, len}`，可能没有）
  page: { type: Object, default: null },
  //: 这次调试的通道（`jvm` / `app`）：只用来把「命中片段为什么会空」说准
  channel: { type: String, default: "" },
  loading: { type: Boolean, default: false },
  error: { type: String, default: "" },
});

const summary = computed(() => stepResult(props.step || {}));
const list = computed(() => structuredList(props.step || {}));

//: 材料：只有两份——写规则要看的就是「选中了哪些」与「这一页长什么样」
const MATERIALS = [
  { value: "matched", label: "命中片段" },
  { value: "page", label: "页面原文" },
];
const material = ref("matched");

const raw = computed(() => {
  if (material.value === "page") return String((props.page && props.page.html) || "");
  return String((props.step || {}).matched_html || "");
});

const emptyHint = computed(() => {
  if (material.value === "page") return "这一步没有抓到页面";
  if (props.channel === "app") {
    return "App 调试通道只推文本，拿不到命中片段；改用本机引擎能看到";
  }
  return "本机引擎没有返回命中片段：规则可能一个节点都没选中";
});
</script>

<template>
  <div class="evp">
    <p v-if="loading" class="evp-muted">正在取这次调试的结果…</p>
    <p v-else-if="error" class="evp-bad">{{ error }}</p>
    <p v-else-if="!step" class="evp-muted">这一步还没跑过：先跑一次调试，材料才有来源。</p>

    <template v-else>
      <!-- 实测摘要：引擎这一跑到底取到了什么 -->
      <div class="evp-facts">
        <span v-if="summary.total !== null" class="evp-total">
          {{ summary.totalLabel }} {{ summary.total }}
        </span>
        <span v-for="f in summary.fields" :key="f.label" class="evp-field">
          <span class="evp-key">{{ f.label }}</span>
          <span class="evp-val">{{ f.value || "（空）" }}</span>
        </span>
        <span v-if="summary.content && summary.content.hasText" class="evp-field">
          <span class="evp-key">正文</span>
          <span class="evp-val">{{ summary.content.chars }} 字符<template
            v-if="summary.content.pages"
          > · {{ summary.content.pages }} 页</template></span>
        </span>
        <span
          v-if="summary.content && summary.content.firstLine"
          class="evp-field"
        >
          <span class="evp-key">首段</span>
          <span class="evp-val">{{ summary.content.firstLine }}</span>
        </span>
        <span v-if="summary.reason" class="evp-bad">{{ summary.reason }}</span>
        <span
          v-else-if="!summary.fields.length && summary.total === null
            && !(summary.content && summary.content.hasText)"
          class="evp-muted"
        >引擎没有给出可读的取值</span>
      </div>

      <!-- 结构化列表：能取到整批时看这个，比事件行好读 -->
      <table v-if="list" class="evp-table">
        <thead>
          <tr><th v-for="c in list.columns" :key="c.key">{{ c.label }}</th></tr>
        </thead>
        <tbody>
          <tr v-for="(row, i) in list.rows" :key="i">
            <td v-for="c in list.columns" :key="c.key">{{ row[c.key] }}</td>
          </tr>
        </tbody>
      </table>
      <p v-if="list && list.truncated" class="evp-muted">
        只显示前 {{ list.rows.length }} 条（共 {{ list.total }}）
      </p>

      <!-- 材料：命中片段 / 页面原文。换材料不重挂查看器——它自己按 raw 换窗口 -->
      <div class="evp-bar">
        <el-radio-group v-model="material" size="small">
          <el-radio-button v-for="m in MATERIALS" :key="m.value" :value="m.value">
            {{ m.label }}
          </el-radio-button>
        </el-radio-group>
      </div>
      <MaterialViewer :raw="raw" :searchable="false" :empty-text="emptyHint" />
    </template>
  </div>
</template>

<style scoped>
.evp { font-size: 12px; }
.evp-muted { margin: 4px 0; color: var(--el-text-color-secondary); }
.evp-bad { color: var(--el-color-danger); }
.evp-facts { display: flex; flex-wrap: wrap; gap: 4px 12px; margin: 4px 0; }
.evp-total { color: var(--el-text-color-regular); font-weight: 600; }
.evp-field { display: inline-flex; gap: 4px; max-width: 100%; }
.evp-key { color: var(--el-text-color-secondary); flex: 0 0 auto; }
.evp-val { color: var(--el-text-color-primary); word-break: break-all; }
.evp-table { width: 100%; border-collapse: collapse; margin: 6px 0; }
.evp-table th, .evp-table td {
  border: 1px solid var(--el-border-color-lighter, #ebeef5);
  padding: 2px 6px; text-align: left; vertical-align: top; word-break: break-all;
}
.evp-table th { background: var(--el-fill-color-lighter, #fafafa); font-weight: 500; }
.evp-bar { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin: 8px 0 4px; }
</style>