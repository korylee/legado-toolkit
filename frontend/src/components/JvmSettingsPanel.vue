<template>
  <div class="jvm-panel">
    <!-- 配置 -->
    <el-form label-width="120px" size="small" @submit.prevent>
      <el-form-item label="App 源码目录">
        <el-input v-model="conf.app_repo" placeholder="legado-with-MD3 仓库的本地路径，如 D:\Documents\GitHub\legado-with-MD3"
                  clearable :disabled="saving" />
        <div class="muted" style="font-size: 12px; margin-top: 2px">
          其余环境（JDK / Android SDK / Gradle 目录）由「自检」自动推导，不需要填
        </div>
      </el-form-item>
      <!-- 代理：**这台机器怎么出去**（环境类配置，不是"这次怎么跑"）。两条引擎路
           （调试 / 跑批）与生成后的验证都读它——放弹框里会出现"跑批走了代理、调试没走"
           这种查不出来的不一致（十-3） -->
      <el-form-item label="代理">
        <el-input v-model="conf.proxy" placeholder="http://127.0.0.1:7890（留空 = 直连）"
                  clearable :disabled="saving" />
        <div class="muted" style="font-size: 12px; margin-top: 2px">
          只认 http://（填 host:port 会自动补成 http://）；socks 与 https 不支持——
          上游与本地抓取两处都要能用，只认它们公共的那一种
        </div>
      </el-form-item>
      <!-- **跑批参数不在这里**（2026-09-20 搬走）：测试关键词 / 超时 / 并发 / 挡位 /
           条数上限都是"这次怎么跑"，跟着动作走——它们在书源列表的「全量校验」弹框里，
           改一次只影响那一次。这一页只留**配置**：环境 + 代理 + 自检 + 最近一次的结果 -->
    </el-form>

    <div style="display: flex; gap: 8px; margin: 4px 0 12px; align-items: center">
      <el-button size="small" :loading="checking" @click="doSelftest">自检环境</el-button>
      <el-button size="small" type="primary" :loading="saving" @click="save">保存配置</el-button>
      <span v-if="saving" class="muted" style="font-size: 12px">保存中…</span>
    </div>

    <!-- 跑批入口**不在这里**：结论喂的是列表页的健康档位与星级，所以触发也在那一页
         （书源列表的「全量校验」/「校验选中」/ 列表行的「校验」）。这里只配参数 +
         看最近一次的结果。 -->
    <el-alert type="info" :closable="false" show-icon style="margin-bottom: 12px"
              title="跑批入口在书源列表那边（「全量校验」/「校验选中」/ 列表行的「校验」）">
      <template #default>
        <div style="font-size: 12px">
          本页只管配置参数；改完记得点「保存配置」，跑批读的就是这里的值。
        </div>
      </template>
    </el-alert>

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
      <el-button size="small" link type="primary" @click="refreshLast">刷新</el-button>
    </template>
  </div>
</template>

<script setup>
// JVM 校验设置页（S2）。原则：
//  - 只让用户填「算不出来的」（App 源码目录），环境推导交给自检接口
//  - **这一页只管「配」**：参数存下来、环境自检通过。**跑批的入口不在这里**——
//    结论同时写进 `checks`（健康档位 / 星级 / 深度）与 `meta`，所以触发也在那一页的
//    「全量校验」弹框里
//    （引擎选「本机引擎」）。动作与结果同屏，理由见 CheckJvmForm.vue 的注释。
//  - 因此参数改完要**显式保存**：原先是在「跑批」那一下顺手存进去的，按钮搬走之后
//    不保存就等于白填（且没有任何提示）。
import { ref, reactive, computed, onMounted } from "vue";
import { ElMessage } from "element-plus";
import { getSettings, patchSettings } from "../api/settings";
import { jvmSelftest, jvmResults } from "../api/jvm.js";

//: 这一页只剩**配置**一项（App 源码目录）；跑批参数在列表页的弹框里，不在这
const conf = reactive({ app_repo: "", proxy: "" });   // proxy 属 network 段（这台机器怎么出去）
const selftest = ref(null);
const checking = ref(false);
const saving = ref(false);
const lastRun = ref(null);
const r2 = ref({ count: 0, dist: {} });

onMounted(async () => {
  try {
    const s = await getSettings();
    conf.app_repo = (s.values.jvm || {}).app_repo || "";
    conf.proxy = (s.values.network || {}).proxy || "";
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
    await patchSettings({ jvm: { app_repo: conf.app_repo },
                          network: { proxy: conf.proxy } });
    selftest.value = await jvmSelftest();
  } catch (e) {
    ElMessage.error("自检失败: " + e);
  } finally {
    checking.value = false;
  }
}

async function save() {
  saving.value = true;
  try {
    // 以后端收敛后的值为准（填了越界的值会被收掉，界面要跟着变）
    // 只发**这一页编辑的**那一个键：PATCH 的 exclude_unset 语义保证其余键不动
    // （跑批参数那几项仍然存在设置里，只是不再有界面）
    const s = await patchSettings({ jvm: { app_repo: conf.app_repo },
                                    network: { proxy: conf.proxy } });
    conf.app_repo = (s.values.jvm || {}).app_repo || "";
    conf.proxy = (s.values.network || {}).proxy || "";
    ElMessage.success("已保存，下次跑批生效");
  } catch (e) {
    ElMessage.error("保存失败：" + e);
  } finally {
    saving.value = false;
  }
}

async function refreshLast() {
  try {
    r2.value = await jvmResults();
  } catch (e) { /* 无历史结果 */ }
}
</script>

<style scoped>
.ml8 { margin-left: 8px; }
.mr4 { margin-right: 4px; }
.muted { color: var(--el-text-color-secondary); }
</style>
