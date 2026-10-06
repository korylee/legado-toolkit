<!-- 书源工作区壳：自绘顶栏（关闭 / 模式 / 源身份 / 保存动作）+ 两个模式视图。
     加载、保存、脏守卫都收在这一层——历史上编辑弹窗与调试工作台各养了一条保存
     路径和一份脏跟踪，口径一漂，同一次编辑在两侧会得出不同的「未保存」。 -->
<script setup>
import { computed, defineAsyncComponent, inject, ref, watch } from "vue";
import { Close } from "@element-plus/icons-vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { getDetail, saveSource, sourceExists } from "../api/sources";
import { ensureTagMeta, splitSystemUser } from "../utils/tags";
import { normalizeForSave } from "../utils/sourceSave";
import { useDebugSession } from "../composables/useDebugSession";
import { SOURCE_WORKSPACE_KEY } from "../composables/useSourceWorkspace";
import SourceEditDialog from "./SourceEditDialog.vue";

const DebugWorkbenchView = defineAsyncComponent(
  () => import("../views/DebugWorkbenchView.vue"),
);
const workspace = inject(SOURCE_WORKSPACE_KEY);
if (!workspace)
  throw new Error("SourceWorkspaceDrawer must be mounted under MainLayout");
const { resetDebugState, loadEnvironment } = useDebugSession();

const state = workspace.state;
const canDebug = workspace.canDebug;
const saving = ref(false);

const editorVisible = computed(() => state.visible && state.mode === "edit");
const debugVisible = computed(() => state.visible && state.mode === "debug");
const sourceName = computed(
  () => state.source?.bookSourceName || (state.loading ? "加载中…" : "新建源"),
);
const sourceUrl = computed(
  () => String(state.source?.bookSourceUrl || state.sourceUrl || ""),
);

function selectMode(mode) {
  if (mode === "debug" && !canDebug.value) {
    ElMessage.warning("请先填写并保存书源 URL，再进入调试");
    return;
  }
  workspace.setMode(mode);
}

// 打开时复位调试会话、预热引擎环境、把详情拉进草稿。只在 visible 的
// false→true 跳变上做：模式来回切不清会话——结果是否作数由
// resultRevision 对 draftRevision 判（调试页里那条「规则已修改」提示）
watch(
  () => state.visible,
  (visible, old) => {
    if (!visible || old) return;
    resetDebugState();
    void loadEnvironment();
    void ensureLoaded();
  },
);

// 草稿地址变化（新建时填 URL、另存腾空/还原）只补加载，不动会话
watch(
  () => state.sourceUrl,
  (url, old) => {
    if (state.visible && url !== old) void ensureLoaded();
  },
);

//: 详情只加载一次：草稿里已是同一条 URL（编辑器交接的生成草稿）就跳过，
//: 否则会把正在编辑的草稿静默覆盖掉。打开时 visible 与 sourceUrl 两个 watch
//: 都会叫到它，同一 URL 的在途加载直接共用一份
let loadInflight = null;
let loadInflightUrl = "";
async function ensureLoaded() {
  const url = String(state.sourceUrl || "").trim();
  if (!url) return;
  if (state.source && String(state.source.bookSourceUrl || "") === url) return;
  if (loadInflight && loadInflightUrl === url) return loadInflight;
  loadInflightUrl = url;
  loadInflight = (async () => {
    try {
      workspace.setLoading(true);
      const d = await getDetail(url);
      // 拆系统/用户标签依赖枚举就绪；失败不阻塞编辑，标签按原样展示
      try {
        await ensureTagMeta();
      } catch (e) {
        /* 枚举加载失败不阻塞 */
      }
      workspace.replaceSource(d.source);
      const parsed = splitSystemUser(d.source.bookSourceGroup || "");
      workspace.setUserTags(parsed.user);
      workspace.setStatusLocked(!!d.system_tags_locked);
    } catch (e) {
      ElMessage.error("加载源失败：" + e.message);
    } finally {
      workspace.setLoading(false);
      loadInflight = null;
      loadInflightUrl = "";
    }
  })();
  return loadInflight;
}

// 点 ×、ESC、点遮罩都走到这里。脏着直接关会丢掉几十条规则的修改，必须拦一道
function requestCloseGuarded() {
  if (workspace.requestClose()) return;
  ElMessageBox.confirm("有未保存的修改，确定要关闭吗？", "未保存", {
    confirmButtonText: "丢弃修改",
    cancelButtonText: "继续编辑",
    type: "warning",
  })
    .then(() => {
      workspace.discard();
      workspace.close();
    })
    .catch(() => {});
}

// 保存：**唯一**的落库出口。清洗与 enabledExplore 推导在 utils/sourceSave
// （唯一一份）；探测与覆盖确认只在「地址不同于已存基线」时发生
async function save() {
  if (saving.value) return;
  if (!state.source) return ElMessage.warning("还没有可保存的内容");
  const s = normalizeForSave(state.source);
  if (!String(s.bookSourceName || "").trim()) return ElMessage.warning("名称不能为空");
  if (!String(s.bookSourceUrl || "").trim()) return ElMessage.warning("源地址不能为空");
  if (state.saveAsMode && s.bookSourceUrl === state.savedUrl) {
    return ElMessage.warning("另存为必须使用不同的源地址");
  }
  saving.value = true;
  try {
    if (s.bookSourceUrl !== state.savedUrl) {
      // 新地址先探存在：upsert 按 URL 主键静默覆盖，撞上同名源时用户全程无感
      try {
        const r = await sourceExists(s.bookSourceUrl);
        if (r.exists) {
          try {
            await ElMessageBox.confirm(
              `源地址已存在（源名：${r.name || "（无名）"}），继续将覆盖其全部规则。`,
              "确认覆盖",
              { type: "warning", confirmButtonText: "覆盖", cancelButtonText: "取消" },
            );
          } catch (e) {
            return; // 用户取消：中止保存，不能吞掉确认结果继续走
          }
        }
      } catch (e) {
        // 探测失败不阻塞保存，只在控制台留痕
        console.warn("exists 探测失败", e);
      }
    }
    await saveSource(s, state.userTags, state.statusLocked);
    workspace.markSaved(s);
    ElMessage.success("已保存");
  } catch (e) {
    ElMessage.error("保存失败：" + e.message);
  } finally {
    saving.value = false;
  }
}
</script>

<template>
  <el-drawer
    :model-value="state.visible"
    class="source-workspace"
    direction="rtl"
    size="min(1180px, 96vw)"
    append-to-body
    destroy-on-close
    :with-header="false"
    @update:model-value="(value) => !value && requestCloseGuarded()"
  >
    <div class="ws-topbar">
      <el-button
        class="ws-close"
        :icon="Close"
        text
        aria-label="关闭"
        @click="requestCloseGuarded"
      />
      <el-radio-group
        class="ws-mode"
        size="small"
        :model-value="state.mode"
        @update:model-value="selectMode"
      >
        <el-radio-button value="edit">编辑</el-radio-button>
        <el-radio-button value="debug" :disabled="!canDebug">调试</el-radio-button>
      </el-radio-group>
      <div class="ws-identity">
        <b>{{ sourceName }}</b>
        <span v-if="sourceUrl" class="mono muted ws-url" :title="sourceUrl">{{
          sourceUrl
        }}</span>
      </div>
      <div class="ws-actions">
        <el-tag v-if="workspace.dirty.value" size="small" type="warning"
          >有未保存修改</el-tag
        >
        <el-button v-if="state.saveAsMode" size="small" @click="workspace.cancelSaveAs()"
          >取消另存</el-button
        >
        <el-button
          v-else-if="state.savedUrl"
          size="small"
          @click="workspace.startSaveAs()"
          >另存为</el-button
        >
        <el-button size="small" type="primary" :loading="saving" @click="save">{{
          state.saveAsMode ? "保存为新源" : "保存"
        }}</el-button>
      </div>
    </div>

    <div v-loading="state.loading" class="ws-body">
      <SourceEditDialog v-if="editorVisible" />
      <DebugWorkbenchView
        v-if="debugVisible"
        :initial-source="state.source"
        :source-url="state.sourceUrl"
        :initial-step="state.initialStep"
        :initial-query="state.initialQuery"
      />
    </div>
  </el-drawer>
</template>

<style>
/* el-drawer 与内部控件样式不能依赖 scoped 属性（AGENTS #15）；.ws- 前缀限定本壳 */
.source-workspace .el-drawer__body {
  padding: 0;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}
.ws-topbar {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 16px;
  border-bottom: 1px solid var(--app-border-light);
  background: var(--app-surface);
}
.ws-close {
  flex: 0 0 auto;
}
.ws-mode {
  flex: 0 0 auto;
}
.ws-identity {
  display: flex;
  align-items: baseline;
  gap: 8px;
  flex: 1 1 auto;
  min-width: 0;
  padding-left: 4px;
}
.ws-identity b {
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 40%;
}
.ws-url {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ws-actions {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  gap: 8px;
}
.ws-body {
  flex: 1 1 auto;
  min-height: 0;
  /* 抽屉体是唯一滚动容器：el-drawer__body 已 overflow:hidden 定住顶栏，这里不滚
     的话折叠线以下全部够不着（2026-10-05 验收 F1 实测）。横向锁死，防宽内容
     把壳撑出横向滚动条——折行交给内容自身的 word-break */
  overflow-y: auto;
  overflow-x: hidden;
}
@media (max-width: 720px) {
  .source-workspace.el-drawer {
    width: 100% !important;
  }
  .ws-topbar {
    flex-wrap: wrap;
    padding: 8px 10px;
  }
  .ws-identity {
    order: 5;
    flex-basis: 100%;
  }
  .ws-identity b {
    max-width: 50%;
  }
  .ws-actions {
    margin-left: auto;
  }
}
</style>
