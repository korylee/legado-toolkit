<template>
  <div class="jvm-panel">
    <!-- 配置 -->
    <el-form label-width="120px" size="small" @submit.prevent>
      <el-form-item label="App 源码目录">
        <el-input v-model="conf.app_repo" placeholder="legado-with-MD3 仓库的本地路径，如 D:\Documents\GitHub\legado-with-MD3"
                  clearable :disabled="running" />
        <div class="muted" style="font-size: 12px; margin-top: 2px">
          其余环境（JDK / Android SDK / Gradle 目录）由「自检」自动推导，不需要填
        </div>
      </el-form-item>
      <el-form-item label="测试关键词">
        <el-input v-model="conf.keyword" style="width: 200px" :disabled="running" />
      </el-form-item>
      <el-form-item label="超时(秒)/并发">
        <el-input-number v-model="conf.timeout" :min="limits.jvm_timeout?.[0] ?? 5"
                         :max="limits.jvm_timeout?.[1] ?? 120" :disabled="running" />
        <el-input-number v-model="conf.concurrency" :min="limits.jvm_concurrency?.[0] ?? 1"
                         :max="limits.jvm_concurrency?.[1] ?? 32" class="ml8" :disabled="running" />
      </el-form-item>
      <!-- 标签**不能也叫「探测深度」**：本地回放那一页已经有一个同名的（那是
           1/2/3/4 一根轴，与这里 search/toc/content 完全不同的两根轴）。
           同名不同物＝用户按一个改、另一个没动，还以为是 bug -->
      <el-form-item label="校验深度">
        <!-- 取值来自后端 limits.jvm_depth（AGENTS #8：枚举只在后端定义） -->
        <el-select v-model="conf.depth" style="width: 220px" :disabled="running">
          <el-option v-for="d in depthOptions" :key="d.value" :value="d.value" :label="d.label" />
        </el-select>
        <div class="muted" style="font-size: 12px; margin-top: 2px">
          {{ depthHint }}
        </div>
      </el-form-item>
      <el-form-item label="条数上限">
        <el-input-number v-model="conf.limit" :min="0" :max="100000" :disabled="running" />
        <span class="muted ml8" style="font-size: 12px">0 = 全部在用源</span>
      </el-form-item>
    </el-form>

    <div style="display: flex; gap: 8px; margin: 4px 0 12px">
      <el-button size="small" :loading="checking" @click="doSelftest">自检环境</el-button>
      <el-button size="small" type="primary" :loading="running"
                 :disabled="!selftestOk || running" @click="doRun">
        {{ running ? "校验中…" : "跑 JVM 校验" }}
      </el-button>
      <span v-if="running" class="muted" style="align-self: center; font-size: 12px">
        {{ depthCostHint }}；此页会一直等到跑完，期间可切到别的页面
      </span>
    </div>

    <!-- 自检结果 -->
    <el-alert v-if="selftest && !selftest.ok" type="error" :closable="false" title="环境自检未通过" />
    <el-table v-if="selftest" :data="selftest.checks" size="small" style="margin: 8px 0">
      <el-table-column label="检查项" prop="name" width="220" />
      <el-table-column label="状态" width="70">
        <template #default="{ row }">
          <el-tag :type="row.ok ? 'success' : 'danger'" size="small">{{ row.ok ? "✓" : "✗" }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="找到 / 说明">
        <template #default="{ row }">
          <div>{{ row.found || row.hint }}</div>
          <div v-if="row.detail" class="muted" style="font-size: 12px">{{ row.detail }}</div>
        </template>
      </el-table-column>
    </el-table>

    <!-- 最近一次跑批结果 -->
    <template v-if="lastRun">
      <el-divider content-position="left">最近一次跑批</el-divider>
      <el-descriptions :column="3" size="small" border>
        <el-descriptions-item label="批次">{{ lastRun.batch || "—" }}</el-descriptions-item>
        <el-descriptions-item label="结论数">{{ lastRun.count ?? r2.count }}</el-descriptions-item>
        <el-descriptions-item label="分布">
          <el-tag v-for="(v, k) in (lastRun.dist || r2.dist)" :key="k" size="small" class="mr4"
                  :type="k === 'ok' ? 'success' : (k === 'empty_js_shell' || k === 'login_wall')
                    ? 'warning' : 'info'">
            {{ k }} {{ v }}
          </el-tag>
        </el-descriptions-item>
      </el-descriptions>
      <div class="muted" style="font-size: 12px; margin-top: 4px">
        结论存在管理库 meta 表（jvm_check:批次:URL）。列表页的「JVM」列读取的就是这里。
      </div>
    </template>
  </div>
</template>

<script setup>
// JVM 校验设置页（S2）。原则：
//  - 只让用户填「算不出来的」（App 源码目录），环境推导交给自检接口
//  - 自检不过就禁用跑批按钮——跑一半 OOM 比不能跑更糟
import { ref, reactive, computed, onMounted } from "vue";
import { ElMessage } from "element-plus";
import { getSettings, patchSettings } from "../api/settings";
import { jvmSelftest, jvmRun, jvmResults } from "../api/jvm.js";

const conf = reactive({ app_repo: "", keyword: "我", timeout: 25, concurrency: 8,
                        limit: 0, depth: "search" });

//: 深度的文案与代价。**耗时是实测值不是估算**（lessons §四十七：性能结论必须实测）：
//: 搜索档全量 3774 条 17 分钟；目录/正文段每源多 2-4 个请求，实测 100 条抽样里
//: 正文段中位耗时 1.5s/源（p90 5.3s），且慢站会撞 App 自己的 60s 读超时。
const DEPTH_TEXT = {
  search: { label: "搜索档（最快）", cost: "全量约 17 分钟（实测）" },
  toc: { label: "搜索 + 目录", cost: "每源多 2 个请求，全量预计 30 分钟以上" },
  content: { label: "搜索 + 目录 + 正文（最准）", cost: "每源再多 1 个请求，全量预计 1 小时以上" },
};
const DEPTH_HINT = {
  search: "只看「搜得到吗」：最快，但验证不出目录/正文是否可用",
  toc: "多验一层目录页能不能解析出章节（对「目录在独立页上」的源最有价值）",
  content: "再抓一章正文——最接近「这本书我能读吗」，也最慢",
};
const limits = ref({});
const selftest = ref(null);
const checking = ref(false);
const running = ref(false);
const lastRun = ref(null);
const r2 = ref({ count: 0, dist: {} });

const selftestOk = computed(() => !!selftest.value?.ok);

//: 选项来自后端下发的枚举；后端没给（旧版）时退回三档默认文案，
//: 保证界面不空——但**值本身**永远以后端为准
const depthOptions = computed(() => {
  const vals = limits.value.jvm_depth || Object.keys(DEPTH_TEXT);
  return vals.map((v) => ({ value: v, label: DEPTH_TEXT[v]?.label || v }));
});
const depthHint = computed(() => DEPTH_HINT[conf.depth] || "");
const depthCostHint = computed(() =>
  (DEPTH_TEXT[conf.depth]?.cost || "全量跑批") + "；跑批期间不要关闭后端");

onMounted(async () => {
  try {
    const s = await getSettings();
    Object.assign(conf, s.values.jvm || {});
    limits.value = s.limits || {};
  } catch (e) { /* 设置接口挂了就保持默认，自检按钮仍可用 */ }
  try {
    r2.value = await jvmResults();
    if (r2.value.count) lastRun.value = { dist: r2.value.dist, count: r2.value.count };
  } catch (e) { /* 无历史结果 */ }
});

async function doSelftest() {
  checking.value = true;
  try {
    // 先保存路径，自检读的是后端设置
    await patchSettings({ jvm: { app_repo: conf.app_repo } });
    selftest.value = await jvmSelftest();
  } catch (e) {
    ElMessage.error("自检失败: " + e);
  } finally {
    checking.value = false;
  }
}

async function doRun() {
  running.value = true;
  try {
    await patchSettings({ jvm: { app_repo: conf.app_repo, keyword: conf.keyword,
                               timeout: conf.timeout, concurrency: conf.concurrency,
                               limit: conf.limit, depth: conf.depth } });
    const r = await jvmRun();
    if (!r.started) {
      // 没起来的两种原因要分开说：另一个 JVM 任务在跑（带 reason）、自检没过（带 selftest）。
      // 都说成「自检未通过」会把原因指反——用户会去查环境，而其实只要等对方跑完。
      if (r.reason) {
        ElMessage.warning(r.reason);
        return;
      }
      selftest.value = r.selftest;
      ElMessage.warning("自检未通过，不能开跑");
      return;
    }
    lastRun.value = r;
    r2.value = await jvmResults();
    ElMessage.success(`完成：${r.count} 条结论已入库`);
  } catch (e) {
    ElMessage.error("跑批失败: " + (e?.message || e));
  } finally {
    running.value = false;
  }
}
</script>

<style scoped>
.ml8 { margin-left: 8px; }
.mr4 { margin-right: 4px; }
.muted { color: var(--el-text-color-secondary); }
</style>
