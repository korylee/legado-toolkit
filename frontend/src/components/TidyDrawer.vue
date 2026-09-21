<script setup>
// 整理源：三步流水线。
//
//   1 名称清洗  展示名去噪（不动地址）
//   2 重复梳理  同站点 + 行为指纹相同的合掉；同域名/同名只读给人看
//   3 收尾      重跑一次校验
//
// 为什么是这个顺序：清洗让重名暴露得更充分（实测同名组 407 → 618），而「同名/同站点」
// 正是第 2 步找重复的判据之一；而改名会改 fingerprint（它含 `bookSourceName`），
// 那些源的校验缓存随之失效——**把重跑集中到最后一步**，而不是边改边跑。
//
// 第 2 步的判据（同站点 + 行为指纹相同）由后端重算，前端只提交分组与保留条：
// 这一步是删源，不信前端算出来的东西。
import { ref, computed, watch } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import {
  applyNames, listDups, mergeSources, previewNames, undoNames, undoMerge,
} from "../api/sources";
import { HEALTH_LABELS } from "../utils/health";
const props = defineProps({ modelValue: { type: Boolean, default: false } });
const emit = defineEmits(["update:modelValue", "changed", "requestCheck"]);

const visible = computed({
  get: () => props.modelValue,
  set: (v) => emit("update:modelValue", v),
});

//: 默认勾选的置信度门槛。0.95 = 确定性规则；0.85 那一档（去装饰符号）留给人过目
const AUTO_CONFIDENCE = 0.95;
const STEPS = ["names", "dups", "finish"];
const step = ref("names");
const stepIndex = computed(() => STEPS.indexOf(step.value));

// ---------------------------------------------------------------- 第 1 步：名称清洗
const loading = ref(false);
const applying = ref(false);
const rows = ref([]);
const total = ref(0);
const reasonLabels = ref({});
const checked = ref(new Set());
const showAll = ref(false);
const nameResult = ref(null);
const nameUndone = ref(false);


//: 默认只看高置信那一档：1651 条一次性铺开会把「我到底在批什么」淹掉
const listRows = computed(() => showAll.value
  ? rows.value
  : rows.value.filter((r) => Number(r.confidence) >= AUTO_CONFIDENCE));
const highCount = computed(
  () => rows.value.filter((r) => Number(r.confidence) >= AUTO_CONFIDENCE).length);
const checkedRows = computed(() => rows.value.filter((r) => checked.value.has(r.url)));

function confType(c) {
  const n = Number(c) || 0;
  if (n >= 0.98) return "success";
  if (n >= 0.95) return "primary";
  if (n >= 0.85) return "warning";
  return "info";
}

function reasonText(r) {
  return (r.reasons || []).map((x) => reasonLabels.value[x] || x).join("、");
}

async function loadNames() {
  loading.value = true;
  nameResult.value = null;
  nameUndone.value = false;
  try {
    const res = await previewNames();
    rows.value = res.items || [];
    total.value = res.total || 0;
    reasonLabels.value = res.reason_labels || {};
    // 默认只勾确定性规则那一档——0.85 那批会去掉分享者签名，那个决定必须由人来做
    checked.value = new Set(
      rows.value.filter((r) => Number(r.confidence) >= AUTO_CONFIDENCE).map((r) => r.url));
    showAll.value = false;
  } catch (e) {
    ElMessage.error("预演失败: " + e.message);
  } finally {
    loading.value = false;
  }
}

function toggle(row) {
  const next = new Set(checked.value);
  if (next.has(row.url)) next.delete(row.url);
  else next.add(row.url);
  checked.value = next;
}

function checkAll(inList) {
  const next = new Set(checked.value);
  listRows.value.forEach((r) => (inList ? next.add(r.url) : next.delete(r.url)));
  checked.value = next;
}

async function applyNameChanges() {
  const changes = checkedRows.value.map((r) => ({ url: r.url, name: r.new_name }));
  if (!changes.length) return ElMessage.warning("先勾选要改的条目");
  try {
    await ElMessageBox.confirm(
      "将改 " + changes.length + " 条源的名字。改的是展示名（地址不动）；"
      + "导出的 JSON 会同步，而这些源的校验结果会失效——最后一步会统一重跑。",
      "应用改名", { type: "warning", confirmButtonText: "应用" });
  } catch (e) { return; }
  applying.value = true;
  try {
    const res = await applyNames(changes);
    nameResult.value = { applied: res.applied, missing: res.missing, prev: res.prev || [] };
    nameApplied.value += res.applied;
    ElMessage.success("已改 " + res.applied + " 条");
    emit("changed");
    const res2 = await previewNames();
    rows.value = res2.items || [];
    reasonLabels.value = res2.reason_labels || {};
    checked.value = new Set();
  } catch (e) {
    ElMessage.error("应用失败: " + e.message);
  } finally {
    applying.value = false;
  }
}

async function undoNameChanges() {
  if (!nameResult.value || !nameResult.value.prev.length) return;
  try {
    const res = await undoNames(nameResult.value.prev);
    nameUndone.value = true;
    nameApplied.value = Math.max(0, nameApplied.value - res.restored);
    ElMessage.success("已还原 " + res.restored + " 条");
    emit("changed");
    await loadNames();
  } catch (e) {
    ElMessage.error("撤销失败: " + e.message);
  }
}

// ---------------------------------------------------------------- 第 2 步：重复梳理
const dupsLoading = ref(false);
const dupGroups = ref([]);
const picks = ref({});          // 组 key → 保留条在 members 里的下标
const selected = ref(new Set());  // 勾选要合并的组 key
const mergeTags = ref(true);
const mergeComment = ref(false);
const merging = ref(false);
const mergeResult = ref(null);  // { groups: [...合并过的组结果...], merged }
//: 本次会话的累计（第 3 步要用它说清「需要重跑多少条」）
const nameApplied = ref(0);
const mergedCount = ref(0);

async function loadDups() {
  dupsLoading.value = true;
  mergeResult.value = null;
  try {
    const res = await listDups({ kinds: "mergeable", limit: 500 });
    dupGroups.value = res.groups || [];
    picks.value = {};
    selected.value = new Set();
    for (const g of dupGroups.value) {
      picks.value[g.key] = g.keep;      // 后端给的「建议保留」
    }
  } catch (e) {
    ElMessage.error("读取重复源失败: " + e.message);
  } finally {
    dupsLoading.value = false;
  }
}

const selectedGroups = computed(
  () => dupGroups.value.filter((g) => selected.value.has(g.key)));
const willDrop = computed(
  () => selectedGroups.value.reduce((n, g) => n + (g.members.length - 1), 0));

function toggleGroup(key, on) {
  const next = new Set(selected.value);
  if (on) next.add(key);
  else next.delete(key);
  selected.value = next;
}

function memberLabel(m) {
  return [m.name || "（无名）", m.host, HEALTH_LABELS[m.health] || m.health || "未校验",
          m.stars ? m.stars + "★" : "", m.checked_at || ""].filter(Boolean).join(" · ");
}

function groupBody(g) {
  return {
    keep: g.members[picks.value[g.key]].url,
    drop: g.members.filter((_m, i) => i !== picks.value[g.key]).map((m) => m.url),
    merge_tags: mergeTags.value,
    merge_comment: mergeComment.value,
  };
}

async function runMerge() {
  const groups = selectedGroups.value;
  if (!groups.length) return ElMessage.warning("先勾选要合并的组");
  merging.value = true;
  try {
    // 先干跑，把「将发生的三件事」摆出来再问 —— 确认框里不该只有"确定吗"
    const plan = [];
    for (const g of groups) {
      plan.push(await mergeSources({ ...groupBody(g), dry_run: true }));
    }
    const tags = plan.reduce((n, p) => n + (p.tags_added || []).length, 0);
    const comments = plan.filter((p) => p.comment).length;
    await ElMessageBox.confirm(
      "将合并 " + groups.length + " 组：软删除 " + willDrop.value + " 条"
      + "（移入回收站，可恢复）；"
      + (mergeTags.value ? "并过 " + tags + " 个标签；" : "不并标签；")
      + (mergeComment.value ? "改写 " + comments + " 条备注；" : "不动备注。"),
      "合并重复源", { type: "warning", confirmButtonText: "合并" });

    const done = [];
    for (const g of groups) {
      done.push(await mergeSources(groupBody(g)));
    }
    mergeResult.value = { groups: done, merged: done.reduce((n, r) => n + r.merged, 0) };
    mergedCount.value += mergeResult.value.merged;
    ElMessage.success("已合并 " + mergeResult.value.merged + " 条");
    emit("changed");
    await loadDups();
  } catch (e) {
    if (e !== "cancel" && e !== "close") ElMessage.error("合并失败: " + e.message);
  } finally {
    merging.value = false;
  }
}

async function undoMergeAll() {
  const done = (mergeResult.value && mergeResult.value.groups) || [];
  let restored = 0;
  try {
    for (const r of done) {
      const res = await undoMerge({
        keep: r.keep, restore_urls: r.restore_urls,
        tags_added: r.tags_added, prev_comment: r.prev_comment,
      });
      restored += res.restored;
    }
    mergeResult.value = null;
    mergedCount.value = Math.max(0, mergedCount.value - restored);
    ElMessage.success("已还原 " + restored + " 条");
    emit("changed");
    await loadDups();
  } catch (e) {
    ElMessage.error("撤销失败: " + e.message);
  }
}

// ---------------------------------------------------------------- 步骤切换
function goStep(s) {
  step.value = s;
  if (s === "names" && !rows.value.length) loadNames();
  if (s === "dups" && !dupGroups.value.length) loadDups();
}

watch(() => props.modelValue, (v) => {
  if (!v) return;
  nameApplied.value = 0;
  mergedCount.value = 0;
  mergeResult.value = null;
  nameResult.value = null;
  dupGroups.value = [];
  step.value = "names";
  loadNames();
});
</script>

<template>
  <el-drawer v-model="visible" title="整理源" size="900px" destroy-on-close
             class="tidy-drawer">
    <el-steps :active="stepIndex" simple style="margin-bottom: 12px">
      <el-step title="1 名称清洗" @click.native="goStep('names')" />
      <el-step title="2 重复梳理" @click.native="goStep('dups')" />
      <el-step title="3 收尾重跑" @click.native="goStep('finish')" />
    </el-steps>

    <!-- 三步共用一副上中下三栏骨架：.tidy-head 固定 / .tidy-main 唯一滚动区 /
         .tidy-foot 固定。让步骤条与底部按钮永远在视野里——列表有 1600+ 条，
         整页滚动时「我批到哪了」和「下一步按钮在哪」都会滚丢。
         三栏的样式写在全局 styles.css：抽屉 teleport 到 body，组件内 scoped 够不到
         .el-drawer__body（AGENTS #15）。 -->

    <!-- ---------------------------------------------------------- 第 1 步 -->
    <template v-if="step === 'names'">
      <div class="tidy-head">
        <el-alert type="info" :closable="false" show-icon style="margin-bottom: 12px">
          <template #title>
            清洗的是<b>展示名</b>，<b>不动地址</b>（地址是源的身份，改了在 App 里等于换了一条源）。
          </template>
        </el-alert>

        <div class="toolbar" style="margin-bottom: 10px">
          <el-button @click="loadNames" :loading="loading">重新预演</el-button>
          <el-switch v-model="showAll" size="small"
                     :active-text="'显示全部 ' + rows.length + ' 条'" inactive-text="只看高置信" />
          <el-button size="small" @click="checkAll(true)">本页全选</el-button>
          <el-button size="small" @click="checkAll(false)">本页全不选</el-button>
          <span class="grow" />
          <span class="muted">
            共 {{ total }} 条 · 建议改 {{ rows.length }} 条 · 确定性规则 {{ highCount }} 条
          </span>
        </div>
      </div>

      <div v-loading="loading" class="list tidy-main">
        <div v-for="r in listRows" :key="r.url" class="row"
             :class="{ on: checked.has(r.url) }" @click="toggle(r)">
          <el-checkbox :model-value="checked.has(r.url)" @click.prevent.stop="toggle(r)" />
          <div class="names">
            <div class="old">{{ r.old_name || "（无名）" }}</div>
            <div class="new">{{ r.new_name }}</div>
          </div>
          <el-tag size="small" :type="confType(r.confidence)">
            {{ Math.round((Number(r.confidence) || 0) * 100) }}%
          </el-tag>
          <el-tooltip v-if="r.collides"
                      content="改完与库里另一条源同名——第 2 步「重复梳理」里能看到它俩"
                      placement="top">
            <el-tag size="small" type="danger">同名</el-tag>
          </el-tooltip>
          <span class="muted why">{{ reasonText(r) }}</span>
        </div>
        <el-empty v-if="!loading && !listRows.length"
                  description="没有需要清洗的名字" :image-size="70" />
      </div>

      <div class="tidy-foot">
        <div v-if="nameResult" class="result">
          <span>已改 <b>{{ nameResult.applied }}</b> 条</span>
          <span v-if="nameResult.missing" class="muted">
            （{{ nameResult.missing }} 条已不在库里，跳过）
          </span>
          <el-button link type="primary" :disabled="nameUndone" @click="undoNameChanges">
            {{ nameUndone ? "已撤销" : "撤销本次改名" }}
          </el-button>
        </div>

        <div class="foot">
          <el-button type="primary" :loading="applying" :disabled="!checked.size"
                     @click="applyNameChanges">
            应用改名（{{ checked.size }} 条）
          </el-button>
          <el-button @click="goStep('dups')">下一步：重复梳理</el-button>
        </div>
      </div>
    </template>

    <!-- ---------------------------------------------------------- 第 2 步 -->
    <template v-else-if="step === 'dups'">
      <div class="tidy-head">
        <el-alert type="warning" :closable="false" show-icon style="margin-bottom: 12px">
          <template #title>
            只列<b>同站点 + 规则完全相同</b>的组。同域名但规则不同、名称相同的镜像站
            <b>不在这里</b>——那些要人判断，一律不自动删。被合并的条移入回收站，可撤销。
          </template>
        </el-alert>

        <div class="toolbar" style="margin-bottom: 10px">
          <el-button @click="loadDups" :loading="dupsLoading">重新读取</el-button>
          <el-checkbox v-model="mergeTags" size="small">并过被删条的标签</el-checkbox>
          <el-checkbox v-model="mergeComment" size="small">备注一并保留</el-checkbox>
          <span class="grow" />
          <span class="muted">
            {{ dupGroups.length }} 组 · 建议合并 {{ willDrop }} 条
          </span>
        </div>
      </div>

      <div v-loading="dupsLoading" class="list tidy-main">
        <div v-for="g in dupGroups" :key="g.key" class="card" :class="{ on: selected.has(g.key) }">
          <div class="card-head">
            <el-checkbox :model-value="selected.has(g.key)"
                         @change="(v) => toggleGroup(g.key, v)" />
            <span class="site mono">{{ g.site }}</span>
            <span class="muted">{{ g.members.length }} 条，可精简 {{ g.redundant }} 条</span>
          </div>
          <div v-for="(m, i) in g.members" :key="m.url" class="member"
               :class="{ keep: picks[g.key] === i }" @click="picks[g.key] = i">
            <el-radio :model-value="picks[g.key]" :value="i" @change="picks[g.key] = i">
              <span class="muted">保留</span>
            </el-radio>
            <div class="names">
              <div class="new">{{ m.name || "（无名）" }}</div>
              <div class="muted mono url">{{ m.url }}</div>
            </div>
            <el-tag v-if="picks[g.key] === i" size="small" type="success">保留这条</el-tag>
            <span class="muted why">{{ memberLabel(m) }}</span>
            <span v-if="m.comment" class="muted why">备注：{{ m.comment }}</span>
          </div>
        </div>
        <el-empty v-if="!dupsLoading && !dupGroups.length"
                  description="没有可合并的重复源" :image-size="70" />
      </div>

      <div class="tidy-foot">
        <div v-if="mergeResult" class="result">
          <span>已合并 <b>{{ mergeResult.merged }}</b> 条</span>
          <el-button link type="primary" @click="undoMergeAll">撤销本次合并</el-button>
        </div>

        <div class="foot">
          <el-button type="primary" :loading="merging" :disabled="!selected.size"
                     @click="runMerge">
            合并选中（{{ selectedGroups.length }} 组 · 删 {{ willDrop }} 条）
          </el-button>
          <el-button @click="goStep('finish')">下一步：收尾重跑</el-button>
        </div>
      </div>
    </template>

    <!-- ---------------------------------------------------------- 第 3 步 -->
    <template v-else>
      <div class="tidy-head">
        <el-alert type="success" :closable="false" show-icon style="margin-bottom: 12px">
          <template #title>
            改名会改指纹（指纹含名称），那些源的校验结果已经失效。现在跑一次全量，
            让结论与当前的书源对齐——<b>这是整条流水线存在的理由</b>：把重跑集中到最后一次。
          </template>
        </el-alert>
      </div>

      <div class="summary tidy-main">
        <p>本次整理：</p>
        <ul>
          <li>改了 <b>{{ nameApplied }}</b> 条名字</li>
          <li>合并了 <b>{{ mergedCount }}</b> 条源（都在回收站，可恢复）</li>
        </ul>
        <p class="muted">
          下次校验时，改过名与合并过的源都会重跑；其余源仍按缓存复用。
        </p>
      </div>

      <div class="tidy-foot">
        <div class="foot">
          <el-button type="primary" @click="emit('requestCheck')">
            去跑校验
          </el-button>
          <el-button @click="emit('changed'); visible = false">先不跑，关闭</el-button>
        </div>
      </div>
    </template>
  </el-drawer>
</template>

<style scoped>
/* 三栏骨架：抽屉 body 是 flex 列（那条规则在全局 styles.css，teleport 到 body 的
   容器组件内的 scoped 样式够不到），这里只管三块各自怎么吃高度：
   头尾按内容、中间是唯一的滚动区。
   原来列表高度写死成 `calc(100vh - 430px)`（移动端 46vh）——那个 430 是猜的数，
   与抽屉真实可用高度对不上：步骤条/提示条/底部按钮一多就留白，少一点就溢出。
   第 1、2 步的中间是列表、第 3 步是一段小结，都带 `.tidy-main`。 */
.tidy-head { flex: 0 0 auto; }
.tidy-main { flex: 1 1 auto; min-height: 0; overflow-y: auto; }
.tidy-foot {
  flex: 0 0 auto;
  display: flex; flex-direction: column; gap: 8px;
  margin-top: 10px;
}
/* 列表只是那个「框」：滚动交给 .tidy-main，免得一层套一层两个滚动条 */
.list { border: 1px solid #ebeef5; border-radius: 4px; }
.row {
  display: flex; align-items: center; gap: 8px;
  padding: 6px 10px; border-bottom: 1px solid #f5f7fa; cursor: pointer;
}
.row:last-child { border-bottom: 0; }
.row:hover { background: #fafcff; }
.row.on { background: #f0f9ff; }
.names { flex: 1 1 auto; min-width: 0; display: flex; align-items: baseline; gap: 8px; }
.old { color: #909399; text-decoration: line-through; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.new { color: #303133; font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.why { flex: 0 0 auto; font-size: 12px; }
.url { font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.card { border-bottom: 1px solid #f0f2f5; padding: 6px 0 10px; }
.card.on { background: #f7fbff; }
.card-head { display: flex; align-items: center; gap: 8px; padding: 4px 10px; }
.card-head .site { font-weight: 600; }
.member { display: flex; align-items: center; gap: 8px; padding: 3px 10px 3px 34px; cursor: pointer; }
.member:hover { background: #fafcff; }
.member.keep { background: #f0f9eb; }
.member .names { flex: 0 1 auto; }
.result {
  display: flex; align-items: center; gap: 8px;
  padding: 8px 12px; background: #f0f9eb; border-radius: 4px;
}
.foot { display: flex; gap: 8px; flex-wrap: wrap; }
.summary { line-height: 1.9; }
.summary ul { margin: 4px 0; padding-left: 20px; }
</style>

<!-- 抽屉 body 是 el-drawer 自己的内部结构，scoped 够不到它——连 `:deep()` 也不行：
     那个带 `.tidy-drawer` class 的元素身上没有 `data-v-*`（el-drawer 的根是 Teleport，
     父组件的 scope id 落不到里面真正带 class 的元素上），前缀永远匹配不上。
     所以单开一个**不带 scoped** 的块，用 class 前缀限定，别污染其它抽屉。
     （跨组件共用的（如 `.dot` 系列）才上全局 styles.css。）
     底部操作是 flex 的固定末项，不做 sticky——sticky 在 teleport 的 drawer body 里
     会与 body 自身的滚动叠加，表现为「贴底但一直遮内容」。 -->
<style>
.tidy-drawer .el-drawer__body {
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
</style>
