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

    <!-- 本次怎么跑：**在这里调**（原来只读、得去设置页改——那是"配置"的住法，
         而这些是"这次动作"的参数）。默认值来自后端（`settings_store.DEFAULTS`），
         上下界也由后端下发（AGENTS #8），**本次改动不写回设置**。 -->
    <el-form v-if="conf" label-width="96px" size="small" style="margin: 8px 0">
      <el-form-item label="测试关键词">
        <el-input v-model="run.keyword" style="width: 160px" placeholder="我" />
      </el-form-item>
      <el-form-item label="超时 / 并发">
        <el-input-number v-model="run.timeout" :min="limits.jvm_timeout?.[0] ?? 5"
                         :max="limits.jvm_timeout?.[1] ?? 120" :step="5" />
        <span class="muted" style="margin: 0 6px">秒 /</span>
        <el-input-number v-model="run.concurrency" :min="limits.jvm_concurrency?.[0] ?? 1"
                         :max="limits.jvm_concurrency?.[1] ?? 32" />
      </el-form-item>
      <el-form-item label="校验深度">
        <el-select v-model="run.depth" style="width: 200px">
          <el-option v-for="d in depthOpts" :key="d.value" :value="d.value" :label="d.label" />
        </el-select>
      </el-form-item>
      <el-form-item label="条数上限">
        <el-input-number v-model="run.limit" :min="limits.jvm_limit?.[0] ?? 0"
                         :max="limits.jvm_limit?.[1] ?? 100000" />
        <span class="muted" style="margin-left: 6px">0 = 不限</span>
      </el-form-item>
      <el-form-item label="范围">
        <!-- 范围由**入口**决定（勾选 / 当前筛选 / 全部）
             「条数上限」只对最后那种生效：范围既然明确给了，再截断就会出现
             「选了 20 条只跑了 3 条」这种看不出来的事 -->
        <template v-if="scope === 'selected'">勾选的 {{ scopeCount }} 条源</template>
        <template v-else-if="scope === 'filtered'">当前筛选的 {{ scopeCount }} 条源</template>
        <template v-else-if="run.limit > 0">{{ run.limit }} 条（不是全部！）</template>
        <template v-else>全部在用源</template>
      </el-form-item>
    </el-form>

    <div class="muted hint">
      {{ depthHintText(run.depth) }}
    </div>
    <div class="muted hint">
      预计耗时：{{ costText }}；跑批期间不要关闭后端。参数只作用于这一次。
    </div>
  </div>
</template>

<script setup>
// 全量校验弹框里的「JVM 引擎」分支（原先是「设置 → JVM 校验」页签上的一个按钮）。
//
// **为什么在这儿**：它产出的结论写进 `checks`（健康档位 / 星级 / 深度）并显示在**这一页
// 的列表**上——动作该和它产出的东西同屏。搬过来之后「校验所有源」只有一个入口，
// 2026-09-20 起引擎也只有一台（本地回放那条路撤了，见 TODO §一点九），
// 所以弹框里可调的是「跑哪些」而不是「用哪台引擎」。
//
// **参数在这里编辑**（2026-09-20 用户提出后改的）：测试关键词 / 超时 / 并发 / 挡位 /
// 条数上限都是"这次怎么跑"，长在动作旁边才对；设置页只留**配置**（App 源码目录 + 自检）。
// 默认值与取值范围仍然只有后端一份（AGENTS #8）：这里从 `GET /api/settings` 取
// `values`（当默认值用）与 `limits`（渲染上下界），**不硬编码**。
// 本次改动**不写回设置**——形状同本地校验的 `check: {...}`。
import { ref, computed, onMounted } from "vue";
import { getSettings } from "../api/settings";
import { jvmSelftest } from "../api/jvm.js";
import { depthCost, depthHintText, depthOptions } from "../utils/jvmDepth";

const props = defineProps({
  //: 本次范围（弹框那一行单选）：selected / filtered / all。**只影响展示**——
  //: 真正决定跑哪些的是 `runJvmBatch` 给后端的入参（两处各算一遍必然漂）
  scope: { type: String, default: "all" },
  //: 该范围下的条数（0 = 不知道/全量）
  scopeCount: { type: Number, default: 0 },
});
const emit = defineEmits(["ready"]);
const conf = ref(null);
const limits = ref({});
//: 挡位选项：**枚举来自后端**（`limits.jvm_depth`），后端没给才退回默认文案
const depthOpts = computed(() => depthOptions(limits.value));
//: 本次跑批的参数（可编辑）。种子来自 `settings.values.jvm`——那是用户在设置页
//: 时代留下的那套值，仍然当基准用；这里改了只影响这一次
const run = ref({ keyword: "我", timeout: 25, concurrency: 8, depth: "search", limit: 0 });
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
    limits.value = s.limits || {};
    // 种子：设置里那套值（没有的键用后端默认值兜底）
    run.value = {
      keyword: conf.value.keyword || s.defaults?.jvm?.keyword || "我",
      timeout: conf.value.timeout ?? s.defaults?.jvm?.timeout ?? 25,
      concurrency: conf.value.concurrency ?? s.defaults?.jvm?.concurrency ?? 8,
      depth: conf.value.depth || s.defaults?.jvm?.depth || "search",
      limit: conf.value.limit ?? s.defaults?.jvm?.limit ?? 0,
    };
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

//: 预计耗时。**全量那条是实测值**（搜索档 3774 条约 17 分钟）；跑一部分时按同一份实测
//: 折算每源耗时，并说清「起停是固定成本」——不然「20 条怎么也要十几秒」看起来像 bug。
const costText = computed(() => {
  if (props.scope !== "all") {
    return "起停约十几秒，外加每源约 0.3 秒（按全量实测折算）";
  }
  return depthCost(run.value.depth);
});

onMounted(load);
//: 父组件在「开始校验」时取走**本次参数**（形状与 `/api/jvm/run` 的 `params` 一致）
defineExpose({ reload: load, params: () => ({ ...run.value }) });
</script>

<style scoped>
.row { display: flex; align-items: center; gap: 6px; }
.hint { font-size: 12px; margin-top: 2px; }
</style>
