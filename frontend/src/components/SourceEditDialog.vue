<script setup>
// 新建 / 编辑书源：对话框形态。跳独立页面会丢掉列表的筛选和分页状态。
import { ref, computed, watch, nextTick } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { api, subscribeJob } from "../api/client";
import { getDetail, listTags, saveSource, sourceExists } from "../api/sources";
import { mergeGroup, splitSystemUser } from "../utils/tags";
import { useMobile } from "../composables/useMobile";
import RuleDebugDrawer from "./RuleDebugDrawer.vue";

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  sourceUrl: { type: String, default: "" },   // 空 = 新建
});
const emit = defineEmits(["update:modelValue", "saved"]);
const isMobile = useMobile();

const visible = computed({
  get: () => props.modelValue,
  set: (v) => emit("update:modelValue", v),
});
const isNew = computed(() => !props.sourceUrl);
const loading = ref(false);
const testing = ref(false);
const testKey = ref("我的");
const testDetailUrl = ref("");
const testResult = ref(null);
const testStale = ref(false);      // 规则已改动，结果过期
const debugVisible = ref(false);   // 调试抽屉
const debugStep = ref("");         // 抽屉打开时定位到哪一步
const systemTags = ref([]);
const manualStatus = ref("");
const userTags = ref([]);
const userTagOptions = ref([]);
const quickUrl = ref("");
const quickDetailUrl = ref("");
const quickDiscover = ref(false);
const quickProbe = ref(true);
const quickLoading = ref(false);
const quickProgress = ref("");
const quickVerify = ref(null);
let quickStop = null;

const TYPE_KEYS = { 0: "novel", 1: "audio", 2: "manga", 3: "video" };
const TYPE_TAGS = { 0: "📖小说", 1: "🎧听书", 2: "🎨漫画", 3: "🎬视频", 4: "❓未知" };
const TYPE_TAG_SET = new Set(Object.values(TYPE_TAGS));
const STATUS_TAGS = ["可用", "待验证", "已失效", "需代理复检"];
const STATUS_TAG_SET = new Set(STATUS_TAGS);

const activeTab = ref("quick");
const activeRuleTab = ref("search");
const extActive = ref(["request"]);
const rawJsonText = ref("");
const rawJsonError = ref("");
const savedSnapshot = ref("");     // 打开弹窗 / 保存成功时的表单快照，用于脏检查
let rawDirty = false;              // 用户是否手改过「原始 JSON」文本域

function blank() {
  return {
    bookSourceName: "", bookSourceUrl: "", bookSourceType: 0,
    bookSourceGroup: "", bookSourceComment: "",
    enabled: true, enabledExplore: false, charset: "",
    customOrder: 0, weight: 0,
    searchUrl: "", header: "",
    loginUrl: "", loginCheckJs: "", jsLib: "", concurrentRate: 1,
    customButton: false, eventListener: false, variableComment: "",
    exploreUrl: "", ruleExplore: {},
    ruleSearch: { bookList: "", name: "", bookUrl: "", coverUrl: "", author: "", intro: "" },
    ruleBookInfo: { name: "", coverUrl: "", author: "", intro: "", lastChapter: "", tocUrl: "" },
    ruleToc: { chapterList: "", chapterName: "", chapterUrl: "", nextTocUrl: "" },
    ruleContent: { content: "", nextContentUrl: "", imageStyle: "", webView: false, webJs: "" },
  };
}
const form = ref(blank());

const displaySystemTags = computed(() => {
  // 类型标签以当前表单类型为准；健康状态可手动覆盖；规则完整沿用详情数据。
  if (!systemTags.value.length && isNew.value) return [];
  const typeTag = TYPE_TAGS[Number(form.value.bookSourceType)] || TYPE_TAGS[4];
  const rest = systemTags.value.filter(
    (t) => !TYPE_TAG_SET.has(t) && !STATUS_TAG_SET.has(t),
  );
  const status = manualStatus.value
    || systemTags.value.find((t) => STATUS_TAG_SET.has(t))
    || "";
  return [typeTag, ...(status ? [status] : []), ...rest];
});

function filled(name) {
  const f = form.value;
  if (name === "basic") return true;
  if (name === "search") return !!(f.searchUrl || (f.ruleSearch && f.ruleSearch.bookList));
  if (name === "detail") return !!(f.ruleBookInfo && Object.values(f.ruleBookInfo).some(Boolean));
  if (name === "toc") return !!(f.ruleToc && f.ruleToc.chapterList);
  if (name === "content") return !!(f.ruleContent && (f.ruleContent.content || f.ruleContent.webJs));
  if (name === "request") return !!(f.header || f.loginUrl || f.loginCheckJs || f.jsLib);
  if (name === "discover") return !!(f.exploreUrl || (f.ruleExplore && Object.keys(f.ruleExplore).length));
  if (name === "interaction") return !!(f.customButton || f.eventListener || f.variableComment);
  if (name === "raw") return !!rawJsonText.value;
  return false;
}

function syncRawFromForm() {
  rawJsonText.value = JSON.stringify(form.value, null, 2);
  rawJsonError.value = "";
  // 文本域已被整份重写成表单内容，不再算「手改过」。
  // 不复位的话这份状态会跨「关闭→再打开」残留：下次打开时黄色提示条会无故出现，
  // 且 watch(form) 会一直拒绝刷新快照，P0-③ 的修复在第二次会话里直接失效
  rawDirty = false;
}

// 表单是否有未保存改动
function isDirty() {
  if (!savedSnapshot.value) return false;
  return JSON.stringify(form.value) !== savedSnapshot.value;
}

// 点遮罩/取消/ESC 都走这里。改了几十条规则误点空白处就全丢，
// 这个代价太大，必须拦一道
function handleBeforeClose(done) {
  if (!isDirty()) return done();
  ElMessageBox.confirm("有未保存的修改，确定要关闭吗？", "未保存", {
    confirmButtonText: "丢弃修改",
    cancelButtonText: "继续编辑",
    type: "warning",
  }).then(() => done()).catch(() => {});
}

function applyRawJson() {
  try {
    const obj = JSON.parse(rawJsonText.value || "{}");
    if (!obj || typeof obj !== "object" || Array.isArray(obj)) {
      throw new Error("必须是 JSON 对象");
    }
    const apply = () => {
      form.value = { ...blank(), ...obj };
      const parsed = splitSystemUser(form.value.bookSourceGroup || "");
      systemTags.value = parsed.system;
      manualStatus.value = "";
      userTags.value = parsed.user;
      rawJsonError.value = "";
      rawDirty = false;               // 已应用到表单，文本域与表单一致了
      ElMessage.success("已应用到表单");
    };
    // 手改过就确认一次：这一步会整份替换表单，不可撤销
    if (rawDirty) {
      ElMessageBox.confirm("将用这段 JSON 整份替换当前表单，确定吗？", "应用到表单", {
        type: "warning", confirmButtonText: "替换", cancelButtonText: "取消",
      }).then(apply).catch(() => {});
      return;
    }
    apply();
  } catch (e) {
    rawJsonError.value = e.message;
    ElMessage.error("JSON 解析失败: " + e.message);
  }
}

function tabDot(name) {
  if (name === "quick") {
    // 与试跑卡片同口径，四级优先（从最 alarming 往下）：fail 红；有附注（通过了但有疑点）黄；
    // unknown 灰（我们的工具回放不了，无法判定——显示成绿色就是绿灯撒谎）；其余绿。
    // **不能再读 all_ok**——all_ok 只看 fail，会把「pass + 附注」显示成绿
    const steps = (quickVerify.value && quickVerify.value.steps) || [];
    if (!steps.length) return "";
    if (steps.some((s) => s.verdict === "fail")) return "err";
    if (steps.some((s) => s.has_notes)) return "warn";
    if (steps.some((s) => s.verdict === "unknown")) return "unknown";
    return "ok";
  }
  if (name === "basic") return "ok";
  if (name === "rules") {
    // 四级优先：有 fail 红；无 fail 但有附注（通过了但有疑点）黄；
    // unknown 灰（无法判定，与徽章 tagTypeOf 的灰色 info 对齐，不能落到绿色）；
    // 都没有才看规则是否填全
    const steps = (testResult.value && testResult.value.steps) || [];
    if (steps.some((s) => s.verdict === "fail")) return "err";
    if (steps.some((s) => s.has_notes)) return "warn";
    if (steps.some((s) => s.verdict === "unknown")) return "unknown";
    return ["search", "detail", "toc", "content"].every(filled) ? "ok" : "warn";
  }
  if (name === "ext") {
    return ["request", "discover", "interaction"].some(filled) ? "ok" : "";
  }
  if (name === "raw") return rawJsonText.value ? "ok" : "";
  return "";
}

// 三态徽章：fail 红 / unknown 灰 / pass 且有附注 黄 / 纯 pass 绿
function tagTypeOf(step) {
  // 兜底：快速生成的结果存在 SQLite 里，可能是改动前产生的旧结果（无 verdict）。
  // 不给兜底的话 `undefined === "fail"` 全不成立，旧结果的 fail 步会渲染成绿色
  if (!step.verdict) return step.ok ? "success" : "danger";
  if (step.verdict === "fail") return "danger";
  if (step.verdict === "unknown") return "info";
  return step.has_notes ? "warning" : "success";
}
const STEP_LABELS = { search: "搜索", bookUrl: "详情链接", toc: "目录", content: "正文" };

// 「全部通过」按 verdict 算，不再用 all_ok。**有附注的 pass 仍算通过**
// 试跑与快速生成两份结果共用这一个判定，避免同一件事写两处
function allPassed(steps) {
  const list = steps || [];
  return list.length > 0 && list.every((s) => s.verdict === "pass");
}

function expandTestFailures(res) {
  if (!res || !Array.isArray(res.steps)) return;
  // 失败和有附注的都要把人带到对应页签——附注是「通过了但有疑点」，
  // 不引导过去的话用户根本不会看到
  const interesting = res.steps.filter((s) => s.verdict === "fail" || s.has_notes);
  if (!interesting.length) return;
  const map = { search: "search", bookUrl: "search", toc: "toc", content: "content" };
  const target = interesting.map((s) => map[s.name]).filter(Boolean);
  if (!target.length) return;
  activeTab.value = "rules";
  activeRuleTab.value = target[0];
}

async function loadTagOptions() {
  try {
    const tags = await listTags();
    userTagOptions.value = tags.filter((t) => t.kind === "user");
  } catch (e) {
    userTagOptions.value = [];
    ElMessage.warning("标签列表加载失败，请确认后端已重启，并检查 /api/sources/tags");
  }
}

watch(() => props.modelValue, (show) => {
  if (!show && quickStop) {
    quickStop();
    quickStop = null;
    quickLoading.value = false;
  }
});

// 规则一改，上一轮试跑结论就作废了。不标过期的话，用户改了规则
// 还看到绿色的「全部通过」，会据此保存——这正是本次要消除的误导。
watch(form, () => {
  if (testResult.value) testStale.value = true;
  // 用户没动过文本域时保持快照新鲜——否则「应用到表单」会把表单改动全部回滚
  if (!rawDirty) rawJsonText.value = JSON.stringify(form.value, null, 2);
}, { deep: true });

watch(() => [props.modelValue, props.sourceUrl], async ([show, url]) => {
  if (!show) return;
  testResult.value = null;
  // 关弹窗时抽屉会被 destroy-on-close 卸载，但 debugVisible 会留在 true，
  // 下次打开就会自己弹出来；而且挂载时 modelValue 已是 true，
  // 抽屉里那个没有 immediate 的 watch 不触发，:initial-step 会被忽略
  debugVisible.value = false;
  loadTagOptions();
  activeTab.value = url ? "basic" : "quick";
  activeRuleTab.value = "search";
  extActive.value = ["request"];
  if (!url) {
    form.value = blank();
    systemTags.value = [];
    manualStatus.value = "";
    userTags.value = [];
    quickVerify.value = null;
    quickProgress.value = "";
    syncRawFromForm();
    savedSnapshot.value = JSON.stringify(form.value);   // 新建：记下初始快照
    return;
  }
  loading.value = true;
  try {
    const d = await getDetail(url);
    form.value = { ...blank(), ...d.source };
    const parsed = splitSystemUser(d.source.bookSourceGroup || "");
    systemTags.value = parsed.system;
    manualStatus.value = d.system_tags_locked
      ? (parsed.system.find((t) => STATUS_TAG_SET.has(t)) || "")
      : "";
    userTags.value = parsed.user;
    syncRawFromForm();
    savedSnapshot.value = JSON.stringify(form.value);   // 编辑：以加载到的源为基线
  } catch (e) {
    ElMessage.error("加载源失败: " + e.message);
  } finally {
    loading.value = false;
  }
});

async function applyGenerated(result) {
  const src = result.source || {};
  form.value = { ...blank(), ...src };
  const parsed = splitSystemUser(src.bookSourceGroup || "");
  systemTags.value = parsed.system;
  manualStatus.value = "";
  userTags.value = parsed.user;
  quickVerify.value = result.verify || null;
  activeTab.value = "rules";
  activeRuleTab.value = "search";
  syncRawFromForm();
  expandTestFailures(result.verify || null);
  ElMessage.success("已生成候选源，可在下方手动修改规则后保存");
  await nextTick();
  const el = document.querySelector(".edit-dialog .main-rule-form");
  if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
}

async function quickGenerate() {
  const url = quickUrl.value.trim();
  if (!url) return ElMessage.warning("请先填带真实关键词的搜索 URL");
  quickLoading.value = true;
  quickProgress.value = "提交中...";
  quickVerify.value = null;
  try {
    const r = await api.post("/jobs", {
      kind: "add",
      payload: {
        url,
        name: form.value.bookSourceName || "",
        type: TYPE_KEYS[Number(form.value.bookSourceType)] || "novel",
        detail_url: quickDetailUrl.value.trim(),
        probe: quickProbe.value,
        discover: quickDiscover.value,
        verify: true,
      },
    });
    quickProgress.value = "任务 " + r.job_id;
    if (quickStop) quickStop();
    quickStop = subscribeJob(
      r.job_id,
      (d) => {
        if (d && d.total) quickProgress.value = `${d.progress || 0}/${d.total}`;
      },
      (d) => {
        quickLoading.value = false;
        quickStop = null;
        if (d.status !== "done") {
          quickProgress.value = "";
          ElMessage.error("生成失败: " + (d.status || "未知错误"));
          return;
        }
        let result = {};
        try { result = JSON.parse(d.result_json || "{}"); } catch (e) { result = {}; }
        if (!result.ok) {
          quickProgress.value = "";
          ElMessage.error(result.error || "生成失败");
          return;
        }
        quickProgress.value = "";
        applyGenerated(result);
      },
    );
  } catch (e) {
    quickLoading.value = false;
    quickProgress.value = "";
    ElMessage.error("提交生成任务失败: " + e.message);
  }
}

async function testRun() {
  testing.value = true;
  testResult.value = null;
  testStale.value = false;          // 新一轮开始，先清过期标记
  try {
    testResult.value = await api.post("/rules/chain", {
      source: form.value,
      keyword: testKey.value || "我",
      detail_url: testDetailUrl.value || quickDetailUrl.value || "",
      pick: 1,
    });
  } catch (e) {
    testResult.value = { error: String(e.message) };
  } finally {
    testing.value = false;
  }
  expandTestFailures(testResult.value);
}

function openDebug(step) {
  debugStep.value = step || "";
  debugVisible.value = true;
}

async function save() {
  // 保存前必须 sanitize：bookSourceType 为 ""/[]/"2" 会让 Legado 导入报 IllegalStateException
  const s = JSON.parse(JSON.stringify(form.value));
  s.bookSourceType = Number(s.bookSourceType);
  if (![0, 1, 2, 3, 4].includes(s.bookSourceType)) s.bookSourceType = 0;
  if (!String(s.bookSourceName || "").trim()) return ElMessage.warning("名称不能为空");
  if (!String(s.bookSourceUrl || "").trim()) return ElMessage.warning("域名不能为空");
  // 新建书源时若填了已存在的域名，后端 upsert_sources 会按 URL 主键
  // **静默覆盖原源的全部规则**——用户全程无感。保存前先探一下。
  if (isNew.value) {
    try {
      const r = await sourceExists(s.bookSourceUrl);
      if (r.exists) {
        try {
          await ElMessageBox.confirm(
            `域名已存在（源名：${r.name || "(无名)"}），继续将覆盖其全部规则。`,
            "确认覆盖", { type: "warning", confirmButtonText: "覆盖", cancelButtonText: "取消" },
          );
        } catch (e) {
          return;   // 用户取消：必须中止保存，不能吞掉确认结果继续往下走
        }
      }
    } catch (e) {
      // 探测失败不阻塞保存，只在控制台留痕
      console.warn("exists 探测失败", e);
    }
  }
  await doSave(s);
}

async function doSave(s) {
  s.bookSourceGroup = mergeGroup(displaySystemTags.value, userTags.value);
  try {
    await saveSource(s, userTags.value, !!manualStatus.value);
    ElMessage.success("已保存");
    savedSnapshot.value = JSON.stringify(form.value);   // 保存成功后刷新快照
    emit("saved", s);
  } catch (e) {
    ElMessage.error("保存失败: " + e.message);
  }
}
</script>

<template>
  <el-dialog v-model="visible" :title="isNew ? '新建书源' : '编辑书源'"
             width="1120px" top="4vh" destroy-on-close class="edit-dialog"
             modal-class="edit-dialog-overlay"
             :close-on-click-modal="false" :before-close="handleBeforeClose">
    <el-row :gutter="16" v-loading="loading" class="main-rule-form">
      <el-col :xs="24" :sm="24" :md="15">
        <el-tabs v-model="activeTab" :tab-position="isMobile ? 'top' : 'left'"
                 class="source-tabs">
          <el-tab-pane v-if="isNew" name="quick">
            <template #label>
              <span class="tab-label">快速生成<i class="dot" :class="tabDot('quick')"></i></span>
            </template>
            <el-form size="small" label-width="76px">
              <el-form-item label="搜索 URL">
                <el-input v-model="quickUrl"
                          placeholder="https://site/search?q=关键词（必须带真实关键词）" />
              </el-form-item>
              <el-form-item label="详情页">
                <el-input v-model="quickDetailUrl"
                          placeholder="可选：详情页样例 URL，不填自动取搜索结果第一条" />
              </el-form-item>
              <el-form-item label="选项">
                <el-checkbox v-model="quickProbe">自动探测搜索端点</el-checkbox>
                <el-checkbox v-model="quickDiscover">仅发现模式</el-checkbox>
                <el-button type="primary" :loading="quickLoading" @click="quickGenerate"
                           style="margin-left: 12px">自动生成</el-button>
                <span v-if="quickProgress" class="muted" style="margin-left: 8px">{{ quickProgress }}</span>
              </el-form-item>
            </el-form>
            <div v-if="quickVerify" class="quick-verify">
              <!-- 与试跑卡片同一套三态渲染：同一个 as_step_dict 产出的数据，
                   这里若还用 ok 两态，「pass + 附注」会显示成绿色，与试跑卡片矛盾 -->
              <div v-for="s in quickVerify.steps" :key="s.name" class="quick-step">
                <el-tag size="small" :type="tagTypeOf(s)">{{ s.name }}</el-tag>
                <span class="muted">{{ s.detail }}</span>
              </div>
              <p v-if="quickVerify.steps && quickVerify.steps.length"
                 class="muted" style="margin: 6px 0 0">
                <b>{{ allPassed(quickVerify.steps) ? "全部通过" : "未全部通过" }}</b>
                <span v-if="quickVerify.steps.some((s) => s.has_notes)">（有疑点，见附注）</span>
                <span v-if="!allPassed(quickVerify.steps)">，可手动修改规则，或补充详情页 URL 后重试。</span>
              </p>
            </div>
          </el-tab-pane>
          <el-tab-pane name="basic">
            <template #label>
              <span class="tab-label">基本信息<i class="dot" :class="tabDot('basic')"></i></span>
            </template>
            <el-form :label-width="isMobile ? 'auto' : '96px'"
                     :label-position="isMobile ? 'top' : 'right'" size="small">
              <el-form-item label="名称"><el-input v-model="form.bookSourceName" /></el-form-item>
              <el-form-item label="域名">
                <el-input v-model="form.bookSourceUrl" :disabled="!isNew"
                          placeholder="https://example.com" />
                <span v-if="!isNew" class="muted">编辑模式下域名不可改；需要更换请另存为新源。</span>
              </el-form-item>
              <el-form-item label="类型">
                <el-radio-group v-model="form.bookSourceType">
                  <el-radio-button :value="0">📖小说</el-radio-button>
                  <el-radio-button :value="1">🎧听书</el-radio-button>
                  <el-radio-button :value="2">🎨漫画</el-radio-button>
                  <el-radio-button :value="3">🎬视频</el-radio-button>
                  <el-radio-button :value="4">❓未知</el-radio-button>
                </el-radio-group>
              </el-form-item>
              <el-form-item label="启用"><el-switch v-model="form.enabled" /></el-form-item>
              <el-form-item label="发现"><el-switch v-model="form.enabledExplore" /></el-form-item>
              <el-form-item label="备注">
                <el-input v-model="form.bookSourceComment" type="textarea" :rows="2" />
              </el-form-item>
              <el-form-item label="系统标签">
                <el-tag v-for="t in displaySystemTags" :key="t" size="small" style="margin-right: 6px">
                  {{ t }}
                </el-tag>
                <el-tag v-if="manualStatus" size="small" type="warning"
                        style="margin-left: 6px">手动</el-tag>
                <span v-if="!displaySystemTags.length" class="muted">保存后自动生成</span>
              </el-form-item>
              <el-form-item label="健康状态">
                <el-select v-model="manualStatus" style="width: 100%">
                  <el-option label="自动（跟随校验结果）" value="" />
                  <el-option v-for="t in STATUS_TAGS" :key="t" :label="t" :value="t" />
                </el-select>
                <span class="muted" style="margin-left: 8px">
                  手动选择后不会被后续校验覆盖
                </span>
              </el-form-item>
              <el-form-item label="用户标签">
                <el-select v-model="userTags" multiple filterable allow-create default-first-option
                           :reserve-keyword="false" style="width: 100%"
                           placeholder="选择或输入标签，如 原创、精排、R18">
                  <el-option v-for="t in userTagOptions" :key="t.tag" :value="t.tag"
                             :label="t.tag + ' (' + t.count + ')'" />
                </el-select>
              </el-form-item>
              <el-form-item label="编码">
                <el-input v-model="form.charset" placeholder="如 gbk，留空默认 utf-8" />
              </el-form-item>
              <el-form-item label="排序/权重">
                <el-input-number v-model="form.customOrder" :min="0" controls-position="right" />
                <el-input-number v-model="form.weight" :min="0" controls-position="right"
                                 style="margin-left: 8px" />
              </el-form-item>
            </el-form>
          </el-tab-pane>
          <el-tab-pane name="rules">
            <template #label>
              <span class="tab-label">规则配置<i class="dot" :class="tabDot('rules')"></i></span>
            </template>
            <el-form :label-width="isMobile ? 'auto' : '96px'"
                     :label-position="isMobile ? 'top' : 'right'" size="small">
              <el-tabs v-model="activeRuleTab" class="rule-tabs">
                <el-tab-pane name="search" label="搜索">
                  <el-form-item label="搜索 URL">
                    <el-input v-model="form.searchUrl" placeholder="/search?q={{key}}" />
                  </el-form-item>
                  <el-form-item label="bookList">
                    <el-input v-model="form.ruleSearch.bookList" placeholder="class.book-list@tag.li" />
                  </el-form-item>
                  <el-form-item label="name">
                    <el-input v-model="form.ruleSearch.name" placeholder="tag.a@text" />
                  </el-form-item>
                  <el-form-item label="bookUrl">
                    <el-input v-model="form.ruleSearch.bookUrl" placeholder="tag.a@href" />
                  </el-form-item>
                  <el-form-item label="coverUrl">
                    <el-input v-model="form.ruleSearch.coverUrl" placeholder="tag.img@data-original" />
                  </el-form-item>
                  <el-form-item label="作者">
                    <el-input v-model="form.ruleSearch.author" placeholder="class.author@text##作者：##" />
                  </el-form-item>
                  <el-form-item label="简介">
                    <el-input v-model="form.ruleSearch.intro" placeholder="class.intro@text" />
                  </el-form-item>
                </el-tab-pane>
                <el-tab-pane name="detail" label="详情">
                  <el-form-item label="name">
                    <el-input v-model="form.ruleBookInfo.name" placeholder="tag.h1@text" />
                  </el-form-item>
                  <el-form-item label="coverUrl">
                    <el-input v-model="form.ruleBookInfo.coverUrl" placeholder="class.cover@tag.img@src" />
                  </el-form-item>
                  <el-form-item label="author">
                    <el-input v-model="form.ruleBookInfo.author" placeholder="class.author@text" />
                  </el-form-item>
                  <el-form-item label="intro">
                    <el-input v-model="form.ruleBookInfo.intro" placeholder="class.intro@text" />
                  </el-form-item>
                  <el-form-item label="lastChapter">
                    <el-input v-model="form.ruleBookInfo.lastChapter" placeholder="class.last@tag.a@text" />
                  </el-form-item>
                  <el-form-item label="tocUrl">
                    <el-input v-model="form.ruleBookInfo.tocUrl" placeholder="可选，目录页 URL" />
                  </el-form-item>
                </el-tab-pane>
                <el-tab-pane name="toc" label="目录">
                  <el-form-item label="chapterList">
                    <el-input v-model="form.ruleToc.chapterList" placeholder="class.chapter@tag.a" />
                  </el-form-item>
                  <el-form-item label="chapterName">
                    <el-input v-model="form.ruleToc.chapterName" placeholder="tag.a@text" />
                  </el-form-item>
                  <el-form-item label="chapterUrl">
                    <el-input v-model="form.ruleToc.chapterUrl" placeholder="tag.a@href" />
                  </el-form-item>
                  <el-form-item label="nextTocUrl">
                    <el-input v-model="form.ruleToc.nextTocUrl" placeholder="class.next@tag.a@href" />
                  </el-form-item>
                </el-tab-pane>
                <el-tab-pane name="content" label="正文">
                  <el-form-item label="content">
                    <el-input v-model="form.ruleContent.content" placeholder="id.content@text" />
                  </el-form-item>
                  <el-form-item label="nextContentUrl">
                    <el-input v-model="form.ruleContent.nextContentUrl" placeholder="class.next@tag.a@href" />
                  </el-form-item>
                  <el-form-item label="imageStyle">
                    <el-select v-model="form.ruleContent.imageStyle" clearable style="width: 100%">
                      <el-option value="" label="默认" />
                      <el-option value="FULL" label="FULL" />
                      <el-option value="TEXT" label="TEXT" />
                    </el-select>
                  </el-form-item>
                  <el-form-item label="webView">
                    <el-switch v-model="form.ruleContent.webView" />
                  </el-form-item>
                  <el-form-item label="webJs">
                    <el-input v-model="form.ruleContent.webJs" type="textarea" :rows="3"
                              placeholder="页面内执行的 ES5 JS，返回正文内容" />
                  </el-form-item>
                </el-tab-pane>
              </el-tabs>
            </el-form>
          </el-tab-pane>
          <el-tab-pane name="ext">
            <template #label>
              <span class="tab-label">扩展配置<i class="dot" :class="tabDot('ext')"></i></span>
            </template>
            <el-form :label-width="isMobile ? 'auto' : '96px'"
                     :label-position="isMobile ? 'top' : 'right'" size="small">
              <el-collapse v-model="extActive">
                <el-collapse-item name="request" title="请求配置">
                  <el-form-item label="header">
                    <el-input v-model="form.header" type="textarea" :rows="3"
                              placeholder='{"User-Agent":"...","Referer":"..."}' />
                  </el-form-item>
                  <el-form-item label="loginUrl">
                    <el-input v-model="form.loginUrl" placeholder="登录页 URL（可选）" />
                  </el-form-item>
                  <el-form-item label="loginCheckJs">
                    <el-input v-model="form.loginCheckJs" type="textarea" :rows="2"
                              placeholder="登录检测 JS（可选）" />
                  </el-form-item>
                  <el-form-item label="并发限制">
                    <el-input-number v-model="form.concurrentRate" :min="1" controls-position="right" />
                  </el-form-item>
                </el-collapse-item>
                <el-collapse-item name="discover" title="发现配置">
                  <el-form-item label="exploreUrl">
                    <el-input v-model="form.exploreUrl" type="textarea" :rows="3"
                              placeholder='[{"title":"分类","url":"/list?page={{page}}"}]' />
                  </el-form-item>
                  <p class="muted" style="margin: 0 0 8px">
                    ruleExplore 结构较复杂，可在「原始 JSON」里编辑。
                  </p>
                </el-collapse-item>
                <el-collapse-item name="interaction" title="交互与事件">
                  <el-form-item label="customButton">
                    <el-switch v-model="form.customButton" />
                  </el-form-item>
                  <el-form-item label="eventListener">
                    <el-switch v-model="form.eventListener" />
                  </el-form-item>
                  <el-form-item label="variableComment">
                    <el-input v-model="form.variableComment" type="textarea" :rows="2"
                              placeholder="变量说明（可选）" />
                  </el-form-item>
                  <el-form-item label="jsLib">
                    <el-input v-model="form.jsLib" type="textarea" :rows="3"
                              placeholder="公共 JS 库（可选）" />
                  </el-form-item>
                </el-collapse-item>
              </el-collapse>
            </el-form>
          </el-tab-pane>

          <el-tab-pane name="raw">
            <template #label>
              <span class="tab-label">原始 JSON<i class="dot" :class="tabDot('raw')"></i></span>
            </template>
            <p class="muted" style="margin: 0 0 8px">
              原始兜底：可直接编辑整条书源 JSON。建议先点「从表单生成」再修改。
            </p>
            <el-alert v-if="rawDirty" type="warning" :closable="false" show-icon
                      style="margin-bottom: 8px"
                      title="你正在手改这段 JSON。点「从表单生成」会覆盖你的改动。" />
            <el-input v-model="rawJsonText" type="textarea" :rows="18" class="raw-json"
                      @input="rawDirty = true" />
            <p v-if="rawJsonError" style="color: #f56c6c; margin: 6px 0 0">{{ rawJsonError }}</p>
            <div class="toolbar" style="margin-top: 8px">
              <el-button size="small" @click="syncRawFromForm">从表单生成</el-button>
              <el-button size="small" type="primary" @click="applyRawJson">应用到表单</el-button>
            </div>
          </el-tab-pane>
        </el-tabs>
      </el-col>

      <el-col :xs="24" :sm="24" :md="9">
        <el-card shadow="never" header="全链路试跑" class="sticky-test">
          <div class="toolbar">
            <el-input v-model="testKey" size="small" placeholder="关键词" style="width: 110px" />
            <el-input v-model="testDetailUrl" size="small" placeholder="详情页 URL（可选）"
                      style="flex: 1 1 150px" />
            <el-button type="success" size="small" :loading="testing" @click="testRun">
              全链路试跑
            </el-button>
          </div>
          <p class="muted" style="margin: 8px 0 0">
            搜索 → 取第一个结果 → 目录 → 第一章正文；仅发现模式可填详情页 URL。
          </p>

          <div v-if="!testResult" class="muted" style="padding: 22px; text-align: center">
            改完规则点「全链路试跑」，按真实阅读路径验证。
          </div>
          <template v-else-if="!testResult.error">
            <div v-for="s in testResult.steps" :key="s.name" class="quick-step">
              <el-tag size="small" :type="tagTypeOf(s)">
                {{ STEP_LABELS[s.name] || s.name }}
              </el-tag>
              <span class="muted">{{ s.detail }}</span>
            </div>
            <el-alert v-if="testStale" type="info" :closable="false" show-icon
                      style="margin-top: 10px"
                      title="规则已改动，结果已过期，请重跑" />
            <p v-else style="margin: 10px 0 0">
              <b>{{ allPassed(testResult.steps) ? "全部通过" : "未全部通过" }}</b>
              <span v-if="testResult.steps.some((s) => s.has_notes)"
                    class="muted">（有疑点，见附注）</span>
            </p>
            <div class="toolbar" style="margin-top: 8px">
              <el-button size="small" type="primary" plain @click="openDebug()">
                查看证据
              </el-button>
            </div>
          </template>
          <pre v-else class="mono" style="margin-top: 10px">{{ testResult.error }}</pre>
        </el-card>

        <el-card shadow="never" header="语法速查" style="margin-top: 12px">
          <ul class="muted" style="margin: 0; padding-left: 18px; line-height: 1.9">
            <li>简写：class.xxx → .xxx；tag.a → a</li>
            <li>链式：class.list@tag.li@tag.a@href</li>
            <li>索引：.0 第一个、.-1 最后一个</li>
            <li>正则：规则##正则##替换（支持 $1）</li>
            <li>接口源：$.data.list[*].name</li>
            <li>取图：tag.img@src / @data-original</li>
          </ul>
        </el-card>
      </el-col>
    </el-row>

    <template #footer>
      <el-button @click="handleBeforeClose(() => (visible = false))">取消</el-button>
      <el-button type="primary" @click="save">保存</el-button>
    </template>

    <!-- 抽屉常驻挂载，靠 v-model 控制显隐。**不要改成 v-if**：抽屉里的 watch 没有
         immediate，initialStep 只在 modelValue 由 false → true 时生效，用 v-if 会让
         首帧落到 steps[0] 而忽略 :initial-step -->
    <RuleDebugDrawer v-model="debugVisible" :result="testResult"
                     :initial-step="debugStep" />
  </el-dialog>
</template>

<style scoped>
.source-tabs { min-height: 520px; }
.source-tabs :deep(.el-tabs__header) { flex: 0 0 auto; }
.tab-label { display: inline-flex; align-items: center; gap: 6px; }
.dot { width: 6px; height: 6px; border-radius: 50%; display: inline-block; background: #c0c4cc; }
.dot.ok { background: #67c23a; }
.dot.warn { background: #e6a23c; }
.dot.err { background: #f56c6c; }
.quick-verify { border-top: 1px solid #ebeef5; margin-top: 4px; padding-top: 8px; }
.quick-step { display: flex; align-items: center; gap: 8px; padding: 3px 0; }
.raw-json :deep(textarea) { font-family: Consolas, Monaco, monospace; font-size: 12px; }
@media (min-width: 993px) {
  .sticky-test { position: sticky; top: 0; }
}
</style>
