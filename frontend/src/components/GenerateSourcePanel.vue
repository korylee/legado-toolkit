<script setup>
// 一键生成源的四段面板：**新建源对话框与任务明细弹窗共用这一份**。
//
// 两处看的是同一次生成，抄第二份的后果不是报错，是「对话框说跑到目录、明细说停搜索」
// （与 `useAgentRun` 同一类教训）。差别只在能不能提交：
//   - 新建源对话框：整份面板（输入区 + 四段 + 结果）
//   - 任务明细弹窗：只读那份（没有输入区，`readonly`）
//
// **本件不解析事件流**。进度全部来自任务行的既有字段——`phase`（当前段名）、
// `progress` / `total`（第几段 / 共几段）、`elapsed_ms`（用时），四个数由
// `backend/jobs/generate_source_job.py` 每段开跑时写一次（`runner.update_stage`）。
// 这一套别的任务也在用，界面不必把后端算过的东西在浏览器里重算一遍：那份重算
// 实现（按 `seq` 增量折叠事件）一天之内踩了三个 bug——`generate_started` /
// `generate_done` 都没有 `stage` 字段被「无 stage 就跳过」挡掉，逐帧刷新又把
// 终态回调当成了每帧回调。判据挪回产它的那一侧，那些坑就不存在了。
import { computed, onMounted, onUnmounted, ref, watch } from "vue";

import { cancelJob, getJobDetail } from "../api/jobs";
import { generateSource } from "../api/rules";
import { jvmDaemonStatus, prepareJvmDaemon } from "../api/jvm";
import { useJobs } from "../composables/useJobs";
import { STEP_LABELS } from "../utils/steps";
import TaskProgress from "./TaskProgress.vue";

const props = defineProps({
  //: 只读模式（任务明细弹窗里用）：不显示输入区，只渲染已有任务的进度
  jobId: { type: String, default: "" },
  readonly: { type: Boolean, default: false },
});
const emit = defineEmits(["generated", "debug"]);

const { watchJob, jobById } = useJobs();

//: 段名 → 中文。**不在本件写第二份**：`utils/steps.js` 的 `STEP_LABELS` 是唯一那份
//:（那两个组件曾各存一份逐字相同的映射，改一处忘一处就出现同一步骤两种叫法）。
const stageLabel = (name) => (name ? STEP_LABELS[name] || name : "");

//: 终态集合。`generate_source` 跑失败也是 `done`（结果体里 `passed=false`）——
//: 所以判「结束没结束」看 status，判「成没成」看 result。
const TERMINAL = ["done", "failed", "cancelled"];

const url = ref("");
const bookSourceType = ref(2);
const submitting = ref(false);
const cancelling = ref(false);
const currentJob = ref(props.jobId || "");
//: 取消 / 提交失败这类**不在任务行里**的原因；与结果体的原因取并（谁有用谁）
const localStop = ref("");
//: 结果体（终态时取一次）：四段的最终结论与源都在这里
const result = ref(null);
//: 从详情接口取回的任务行。**兜底用**：`jobById` 只认识「开着观测流」的任务，
//: 而明细弹窗可以打开任何一条历史任务——那时它在登记表里没有，进度就一个都不显示。
const fetchedJob = ref(null);
//: 四段的**名字与顺序**，由 `/jobs/{id}/detail` 的 `stages` 下发（后端 `STAGES` 是权威）。
//: 有它才能给每一段配上名字——只靠 `phase` 时只有当前那段有名字，前面几段是光秃秃的点。
const stageNames = ref([]);
let stop = null;

const job = computed(() => {
  const live = currentJob.value ? jobById(currentJob.value) : null;
  return live || fetchedJob.value || null;
});
const status = computed(() => String((job.value && job.value.status) || ""));
const done = computed(() => TERMINAL.includes(status.value));
const busy = computed(() => submitting.value || (!!currentJob.value && !done.value));
const passed = computed(() => !!(result.value && result.value.passed) && !localStop.value);

//: 跑完后各段的最终结论（`[{stage, ok, reason, fields}]`）
const finalStages = computed(() => (result.value && result.value.stages) || []);

//: 段卡片与「在等什么」都交给 `TaskProgress`（与明细弹窗共用一份）：
//: 它读任务行的 `progress`/`total`/`phase_ms`，再用低频拉的当前段事件说「正在做什么」。

//: 结果表：按段分组把最终规则铺平。**键名取源里的那个键**（`bookList` / `chapterName`…），
//: 不是我们的字段名——用户要拿它对着源 JSON 看。
const RULE_KEYS = {
  book_list: "bookList", book_url: "bookUrl", name: "name", author: "author",
  intro: "intro", cover_url: "coverUrl", kind: "kind", word_count: "wordCount",
  last_chapter: "lastChapter",
  chapter_list: "chapterList", chapter_name: "chapterName", chapter_url: "chapterUrl",
  content: "content", webJs: "webJs",
};
const resultRows = computed(() => finalStages.value.flatMap((s) => (s.fields || [])
  .filter((f) => f && f.rule)
  .map((f) => ({
    step: stageLabel(s.stage),
    key: RULE_KEYS[f.field] || f.field,
    rule: String(f.rule),
  }))));

//: 失败时**没跑到的段**（一行说清，不用逐段列）
//: 失败时**没跑到的段**：结果体里只记跑到的那几段，后面的一律算没跑到。
const unrunLabels = computed(() => stageNames.value
  .slice(finalStages.value.length)
  .map((n) => stageLabel(n))
  .join("、"));

const stopReason = computed(() => {
  if (localStop.value) return localStop.value;
  const r = result.value || {};
  return String(r.stop_reason || "");
});

//: 引擎预热的状态短句（面板顶部一行灰字）。空 = 不显示。
const engineHint = ref("");
let warmTimer = null;

/**
 * 打开「快速生成」就先点燃引擎。
 *
 * 引擎冷启动 41.5 秒（实测），而用户填地址、选类型总要几十秒——先点着就把这段
 * 完全盖掉。**尽力而为**：拿不到引擎（正被别的任务用）就跳过，不打扰用户，
 * 因为那时它本来就是热的。
 */
async function warmEngine() {
  if (props.readonly) return;
  engineHint.value = "检查引擎…";
  try {
    await prepareJvmDaemon();
  } catch (e) {
    // 409 = 引擎正被任务占着：本来热的，不需要预热，也不必让用户看到「失败」
    engineHint.value = "";
    return;
  }
  engineHint.value = "正在准备引擎（首次约 40 秒，之后秒开）";
  let tries = 0;
  warmTimer = setInterval(async () => {
    tries += 1;
    try {
      const st = await jvmDaemonStatus();
      if (st && (st.responsive || st.ready)) {
        engineHint.value = "引擎已就绪";
        clearInterval(warmTimer);
        warmTimer = null;
        // 就绪提示留一会儿再收，避免一闪而过
        setTimeout(() => { engineHint.value = ""; }, 4000);
        return;
      }
    } catch (e) { /* 状态查不到就继续等 */ }
    if (tries >= 40) {   // 有界：最多等 80 秒
      engineHint.value = "";
      clearInterval(warmTimer);
      warmTimer = null;
    }
  }, 2000);
}

onMounted(warmEngine);
onUnmounted(() => { if (warmTimer) { clearInterval(warmTimer); warmTimer = null; } });

function reset() {
  localStop.value = "";
  result.value = null;
  fetchedJob.value = null;
  stageNames.value = [];
  cancelling.value = false;
}

async function loadDetail(id) {
  const detail = await getJobDetail(id).catch(() => ({}));
  // 详情就是任务行本身，`result` 挂在它下面
  const row = detail && (detail.job || detail);
  if (row && row.id) fetchedJob.value = row;
  if (detail && Array.isArray(detail.stages) && detail.stages.length) {
    stageNames.value = detail.stages.map(String);
  }
  result.value = (detail && detail.result) || null;
  // 生成结果只有**由本面板发起**那次才回填表单（只读模式是别人在管源）
  if (!props.readonly && result.value && result.value.source) emit("generated", result.value);
}

/** 盯住任务行。状态一变（`status`）就走收尾——注意这是**状态**的变化，
 *  不是事件流的每帧，所以不需要任何去重/顺序处理。 */
/** 盯住一个任务。
 *
 * 收尾**不能靠「状态变成 done」**：`useJobs` 的 `active` 只存在跑任务（`pending` /
 * `running` / `cancel_requested`），一到终态就把整行删掉——`jobById` 当场变 null，
 * `status` 变空串，那个判据永远等不到终态（实测 2026-10-27：跑完什么都不显示）。
 * `watchJob` 的回调**保证在终态响一次**，用它收尾。
 */
function follow(id) {
  currentJob.value = id;
  if (stop) stop();
  stop = watchJob(id, () => { void loadDetail(id); });
  // 开始就先取一次：任务行（进度四个数）与段名清单都不必等首帧
  void loadDetail(id);
}

async function submit() {
  const target = url.value.trim();
  if (!target) return;
  reset();
  submitting.value = true;
  if (stop) { stop(); stop = null; }
  try {
    const r = await generateSource({
      url: target, book_source_name: "", book_source_type: bookSourceType.value,
    });
    follow(r.job_id);
  } catch (e) {
    submitting.value = false;
    localStop.value = "提交失败：" + e.message;
  }
}

/** 取消。**原因写「已取消」而不是让它变成「失败」**：后端落 `cancelled` 终态，
 *  照搬状态名会读成出错。 */
async function cancel() {
  const id = currentJob.value;
  if (!id) return;
  cancelling.value = true;
  try {
    await cancelJob(id);
    if (stop) { stop(); stop = null; }
    localStop.value = "已取消";
  } catch (e) {
    localStop.value = "取消失败：" + e.message;
  } finally {
    cancelling.value = false;
  }
}

// 明细弹窗把任务号透进来：直接盯那个任务，不重新提交
watch(() => props.jobId, (id) => {
  if (!id) return;
  reset();
  follow(id);
}, { immediate: true });

defineExpose({ submit });
</script>

<template>
  <div class="gen-panel">
    <div v-if="!readonly" class="gen-form">
      <div class="gen-row">
        <el-input v-model="url" clearable :disabled="busy"
                  placeholder="粘贴带关键词的搜索页地址，如 https://site/search?q=关键词" />
        <!-- 跑一次要两三分钟：**必须能中途停**。跑起来之后主按钮换成取消，
             同一位置同一宽度，手不用找 -->
        <el-button v-if="busy" type="warning" plain :loading="cancelling" @click="cancel">
          取消
        </el-button>
        <el-button v-else type="primary" @click="submit">一键生成</el-button>
      </div>
      <div class="gen-row gen-types">
        <span class="gen-label">类型</span>
        <el-radio-group v-model="bookSourceType" size="small" :disabled="busy">
          <el-radio-button :value="0">小说</el-radio-button>
          <el-radio-button :value="1">听书</el-radio-button>
          <el-radio-button :value="2">漫画</el-radio-button>
          <el-radio-button :value="3">下载</el-radio-button>
        </el-radio-group>
        <span v-if="currentJob" class="gen-jid">{{ currentJob }}</span>
        <span v-if="engineHint" class="gen-engine">{{ engineHint }}</span>
      </div>
    </div>

    <!-- 进度/在等什么：共用组件（明细弹窗里是同一份） -->
    <TaskProgress :job="job" :stages="stageNames" :final-stages="finalStages" compact />

    <!-- 结果：四段的最终规则。**这才是这个功能的交付物**，所以常驻 -->
    <div v-if="done && result" class="gen-result">
      <div class="gen-result-head">
        <span :class="passed ? 'gen-result-ok' : 'gen-result-fail'">
          {{ passed ? "四段全部通过" : "停在：" + (stopReason || "未通过") }}
        </span>
        <span class="grow" />
        <!-- 元数据不在这里补：一键生成只补链路必需的那几格（一次问八个字段模型一个
             都不给，实测 2026-10-27）。其余字段去调试页逐步补。**只在跑通后给入口**：
             链路没通时该补的是缺口，不是元数据。 -->
        <el-button v-if="passed && !readonly" link size="small"
                   @click="emit('debug', { url })">
          去调试页补其余字段
        </el-button>
      </div>
      <div v-if="!passed && unrunLabels" class="gen-result-note">
        没跑到的段：{{ unrunLabels }}
      </div>
      <table v-if="resultRows.length" class="gen-table">
        <tbody>
          <tr v-for="row in resultRows" :key="row.step + row.key">
            <td class="gen-table-step">{{ row.step }}</td>
            <td class="gen-table-key">{{ row.key }}</td>
            <td class="gen-table-rule"><code>{{ row.rule }}</code></td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 任务行自己没给原因时才用这条兜底（`job.error` 是任务级失败，与「段没过」不同） -->
    <el-alert v-if="done && !result && (localStop || (job && job.error))" type="warning"
              :closable="false" show-icon :title="localStop || (job && job.error)" />
  </div>
</template>

<style scoped>.gen-form { display: flex; flex-direction: column; gap: 12px; margin-bottom: 16px; }.gen-row { display: flex; align-items: center; gap: 10px; }.gen-row .el-input { flex: 1; }.gen-types { gap: 12px; }
.gen-engine { font-size: 12px; color: var(--el-text-color-secondary); }.gen-label { font-size: 13px; color: var(--el-text-color-regular); min-width: 28px; }.gen-jid {
  font-size: 12px;
  color: var(--el-text-color-placeholder);
  font-variant-numeric: tabular-nums;
}</style>
