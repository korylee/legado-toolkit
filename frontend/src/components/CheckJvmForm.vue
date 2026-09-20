<template>
  <div class="jvm-check-form">
    <!-- 自检：本机引擎的前置。**打开就自动跑一次**（它是只读的探测，1-2 秒），
         省掉「先点自检、再点开始」那道仪式；不自检就开跑只会在 Gradle 里炸出
         一堆看不懂的错。 -->
    <div class="row">
      <el-tag v-if="checking" size="small" type="info">正在自检…</el-tag>
      <el-tag v-else-if="selftest && selftest.ok" size="small" type="success">
        自检通过
      </el-tag>
      <el-tag v-else-if="selftest" size="small" type="danger">自检未通过</el-tag>
      <el-button size="small" link type="primary" :loading="checking" @click="runSelftest">
        重新自检
      </el-button>
    </div>

    <el-alert v-if="selftest && !selftest.ok" type="error" :closable="false" show-icon
              style="margin: 8px 0" :title="blockReason" />

    <!-- 本次要用的参数：**只读展示**。它们存在设置里（那一页能改），这里不重复放一份
         编辑框——两处都能改就会漂，而漂的表现是「弹框里显示的和实际跑的不是一套」。 -->
    <el-descriptions v-if="conf" :column="2" size="small" border style="margin: 8px 0">
      <el-descriptions-item label="测试关键词">{{ conf.keyword || "我" }}</el-descriptions-item>
      <el-descriptions-item label="超时 / 并发">{{ conf.timeout }}s / {{ conf.concurrency }}</el-descriptions-item>
      <el-descriptions-item label="校验深度">{{ depthLabel(conf.depth) }}</el-descriptions-item>
      <el-descriptions-item label="范围">
        <!-- 选了具体几条时**范围由选中决定**，设置里的「条数上限」不再参与
             （否则会出现「选了 20 条只跑了 3 条」这种看不出来的截断） -->
        <template v-if="selectedCount > 0">选中的 {{ selectedCount }} 条源</template>
        <template v-else-if="conf.limit > 0">{{ conf.limit }} 条（不是全部！）</template>
        <template v-else>全部在用源</template>
      </el-descriptions-item>
    </el-descriptions>

    <div class="muted hint">
      {{ depthHintText(conf?.depth) }}
    </div>
    <div class="muted hint">
      预计耗时：{{ costText }}；跑批期间不要关闭后端。
      这几项在「设置 → JVM 校验」里改。
    </div>
  </div>
</template>

<script setup>
// 全量校验弹框里的「JVM 引擎」分支（原先是「设置 → JVM 校验」页签上的一个按钮）。
//
// **为什么搬到这儿**：它写的是 `meta` 表的结论、显示在**列表页的 JVM 列**——动作应该和
// 它产出的东西在同一屏；兄弟动作（本地引擎的全量校验）本来就在这个弹框里。搬过来之后
// 「校验所有源」只有一个入口，引擎是它的一个选项。
//
// 参数**不在这里编辑**：那是设置页的职责（AGENTS #8：默认值与取值范围只在后端定义，
// 前端只拿值）。弹框负责把「这次会用哪套参数、要跑多久」说清楚，并挡住环境没配好的情况。
import { ref, computed, onMounted } from "vue";
import { getSettings } from "../api/settings";
import { jvmSelftest } from "../api/jvm.js";
import { depthCost, depthHintText, depthLabel } from "../utils/jvmDepth";

const props = defineProps({
  //: 列表页勾选的条数（0 = 全量）。范围由它决定，设置里的「条数上限」只在全量时参与。
  selectedCount: { type: Number, default: 0 },
});
const emit = defineEmits(["ready"]);
const conf = ref(null);
const selftest = ref(null);
const checking = ref(false);

const blockReason = computed(() => {
  const bad = ((selftest.value || {}).checks || []).filter((c) => !c.ok);
  const first = bad[0] || {};
  return (first.name ? first.name + "：" : "") + (first.hint || "环境自检未通过");
});

async function load() {
  // **先关闸门**：弹框不销毁内容，重开时上一次的「自检通过」还留在父组件里
  // （1-2 秒自检期间按钮会是可点的，而那道闸门正是为了挡「没自检就开跑」）
  emit("ready", false);
  try {
    const s = await getSettings();
    conf.value = s.values.jvm || {};
  } catch (e) { /* 设置接口挂了就只跑自检，参数留空 */ }
  await runSelftest();
}

async function runSelftest() {
  checking.value = true;
  try {
    selftest.value = await jvmSelftest();
  } catch (e) {
    selftest.value = { ok: false, checks: [{ name: "自检接口", hint: "自检失败：" + e }] };
  } finally {
    checking.value = false;
    emit("ready", !!selftest.value?.ok);
  }
}

function depthHint(v) { return depthHintText(v); }

//: 预计耗时。**全量那条是实测值**（搜索档 3774 条约 17 分钟）；选中几条时按同一份实测
//: 折算每源耗时，并说清「起停是固定成本」——不然「20 条怎么也要十几秒」看起来像 bug。
const costText = computed(() => {
  if (props.selectedCount > 0) {
    return "起停约十几秒，外加每源约 0.3 秒（按全量实测折算）";
  }
  return depthCost(conf.value?.depth);
});

onMounted(load);
defineExpose({ reload: load });
</script>

<style scoped>
.row { display: flex; align-items: center; gap: 6px; }
.hint { font-size: 12px; margin-top: 2px; }
</style>
