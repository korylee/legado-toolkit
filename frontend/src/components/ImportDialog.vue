<script setup>
// 书源导入：多种输入方式共用预览与导入流程
import { ref, computed, watch } from "vue";
import { ElMessage } from "element-plus";
import { api } from "../api/client";
import ImportRepositoryBrowser from "./ImportRepositoryBrowser.vue";

const props = defineProps({ modelValue: { type: Boolean, default: false } });
const emit = defineEmits(["update:modelValue", "imported"]);

const visible = computed({
    get: () => props.modelValue,
    set: (v) => emit("update:modelValue", v),
});
const file = ref(null);
const url = ref("");
const sourceName = ref("");
const pasteText = ref("");
const activeTab = ref("file");
const previewTab = ref("");
const resultTab = ref("");
const rawText = ref("");
const preview = ref(null);
const loading = ref(false);
const result = ref(null);
let previewRequestId = 0;

function clearPreview() {
    previewRequestId += 1;
    rawText.value = "";
    preview.value = null;
    result.value = null;
    previewTab.value = "";
    resultTab.value = "";
}

function onTabChange() {
    clearPreview();
}

function onFileChange(f) {
    file.value = f.raw;
    sourceName.value = f.name || "";
    clearPreview();
}
function beforeUpload() {
    return false;
} // 手动解析，不自动上传

async function onRepositoryPreview({ sources, sourceName: repositorySource }) {
    loading.value = true;
    clearPreview();
    try {
        const count = await setPreview(
            JSON.stringify(sources),
            repositorySource,
            "repository",
        );
        ElMessage.success("已读取 " + count + " 条书源，确认预览后再导入");
    } catch (error) {
        ElMessage.error("仓库书源预览失败：" + error.message);
    } finally {
        loading.value = false;
    }
}

//: 同 URL 但规则不同时怎么办。**只对「在用」的那行有意义**——回收站里的同 URL
//: 历史版本不构成冲突，导入会直接新建一行在用的（所以「先删掉旧的再导入新版」
//: 这条路现在也通）。默认 keep：外部源默认不该覆盖你已经在用的规则。
const strategy = ref("keep");

watch(strategy, async () => {
    if (!rawText.value || !previewTab.value) return;
    const text = rawText.value;
    const source = sourceName.value;
    const tab = previewTab.value;
    loading.value = true;
    try {
        await setPreview(text, source, tab);
    } catch (error) {
        ElMessage.error("更新导入预览失败：" + error.message);
    } finally {
        loading.value = false;
    }
});

const IMPORT_STATUS_LABELS = {
    new: "新增",
    duplicate: "重复",
    conflict: "规则冲突",
    update: "将覆盖",
    invalid: "无效",
};

function domainOf(url) {
    const value = String(url || "");
    try {
        return new URL(value).host;
    } catch {
        return value || "域名未知";
    }
}

function importStatusLabel(status) {
    return IMPORT_STATUS_LABELS[status] || "未知";
}

function importStatusType(status) {
    return status === "new"
        ? "success"
        : status === "conflict" || status === "invalid"
          ? "warning"
          : "info";
}

async function setPreview(text, source, tab) {
    const requestId = ++previewRequestId;
    const data = JSON.parse(text);
    if (!Array.isArray(data)) throw new Error("不是 JSON 数组");
    const rules = await api.post("/import/preview", {
        content: text,
        conflict_strategy: strategy.value,
    });
    if (requestId !== previewRequestId) return 0;
    rawText.value = text;
    sourceName.value = source || "";
    previewTab.value = tab;
    preview.value = {
        count: data.length,
        counts: rules.counts || {},
        items: rules.items || [],
        rulesIncomplete: Number(rules.counts?.rules_incomplete || 0),
    };
    return data.length;
}

async function parseLocal() {
    if (!file.value) return ElMessage.warning("先选文件");
    loading.value = true;
    try {
        const text = await file.value.text();
        const count = await setPreview(text, file.value.name, "file");
        ElMessage.success("解析成功，" + count + " 条");
    } catch (e) {
        ElMessage.error("解析失败: " + e.message);
        preview.value = null;
    } finally {
        loading.value = false;
    }
}

async function parseUrl() {
    if (!url.value.trim()) return ElMessage.warning("先输入 JSON 链接");
    loading.value = true;
    clearPreview();
    try {
        const fetched = await api.post("/import/url", {
            url: url.value.trim(),
        });
        const count = await setPreview(
            fetched.content,
            fetched.source || url.value.trim(),
            "url",
        );
        ElMessage.success("读取成功，" + count + " 条");
    } catch (e) {
        ElMessage.error("读取 JSON 链接失败：" + e.message);
    } finally {
        loading.value = false;
    }
}

async function parsePaste() {
    if (!pasteText.value.trim()) return ElMessage.warning("先粘贴书源 JSON");
    loading.value = true;
    clearPreview();
    try {
        const parsed = JSON.parse(pasteText.value);
        const data = Array.isArray(parsed) ? parsed : [parsed];
        const count = await setPreview(
            JSON.stringify(data),
            "粘贴内容",
            "paste",
        );
        ElMessage.success("解析成功，" + count + " 条");
    } catch (e) {
        ElMessage.error("解析失败: " + e.message);
    } finally {
        loading.value = false;
    }
}

async function doImport() {
    if (!rawText.value || previewTab.value !== activeTab.value) return;
    loading.value = true;
    result.value = null;
    try {
        const r = await api.post("/import", {
            content: rawText.value,
            source: sourceName.value,
            conflict_strategy: strategy.value,
        });
        result.value = r;
        resultTab.value = activeTab.value;
        ElMessage.success(
            `新增 ${r.new_count} / 更新 ${r.updated_count || 0} / 重复 ${r.duplicate_count} / 冲突 ${r.conflict_count}`,
        );
        emit("imported");
    } catch (e) {
        ElMessage.error("导入失败: " + e.message);
        result.value = { error: String(e.message) };
        resultTab.value = activeTab.value;
    } finally {
        loading.value = false;
    }
}
</script>

<template>
    <el-dialog
        v-model="visible"
        title="导入书源"
        width="min(820px, 96vw)"
        top="16px"
        class="source-import-dialog"
        modal-class="source-import-overlay"
        destroy-on-close
    >
        <el-tabs
            v-model="activeTab"
            class="source-import-tabs"
            @tab-change="onTabChange"
        >
            <el-tab-pane label="文件" name="file">
                <el-upload
                    drag
                    :auto-upload="false"
                    :limit="1"
                    accept=".json"
                    :on-change="onFileChange"
                    :before-upload="beforeUpload"
                >
                    <div style="padding: 18px 0">
                        <div style="font-size: 30px">📄</div>
                        <div>选择或拖入书源 JSON 文件</div>
                        <div class="muted">包括阅读 App 导出的书源文件</div>
                    </div>
                </el-upload>
                <div class="toolbar" style="margin-top: 12px">
                    <el-button
                        type="primary"
                        :loading="loading"
                        :disabled="!file"
                        @click="parseLocal"
                    >
                        解析预览
                    </el-button>
                </div>
            </el-tab-pane>

            <el-tab-pane label="链接" name="url">
                <div class="muted" style="margin-bottom: 8px">
                    读取 HTTPS JSON 链接，不会自动导入。
                </div>
                <div class="import-input-row">
                    <el-input
                        v-model="url"
                        placeholder="https://example.com/sources.json"
                        clearable
                        @keyup.enter="parseUrl"
                    />
                    <el-button
                        type="primary"
                        :loading="loading"
                        :disabled="!url.trim()"
                        @click="parseUrl"
                        >读取预览</el-button
                    >
                </div>
            </el-tab-pane>

            <el-tab-pane label="粘贴" name="paste">
                <div class="muted" style="margin-bottom: 8px">
                    粘贴一个书源对象或书源 JSON 数组。
                </div>
                <el-input
                    v-model="pasteText"
                    type="textarea"
                    :rows="9"
                    placeholder='[{"bookSourceName":"示例","bookSourceUrl":"https://…"}]'
                />
                <div class="toolbar" style="margin-top: 12px">
                    <el-button
                        type="primary"
                        :loading="loading"
                        :disabled="!pasteText.trim()"
                        @click="parsePaste"
                        >解析预览</el-button
                    >
                </div>
            </el-tab-pane>

            <el-tab-pane label="书源仓库" name="repository">
                <ImportRepositoryBrowser @preview="onRepositoryPreview" />
            </el-tab-pane>
        </el-tabs>

        <div
            v-if="activeTab !== 'repository' || previewTab === 'repository'"
            class="conflict-options"
        >
            <span class="conflict-label">同 URL 不同规则</span>
            <div>
                <el-radio-group v-model="strategy">
                    <el-radio value="keep">保留现有的，导入的进待审</el-radio>
                    <el-radio value="overwrite">用导入的覆盖</el-radio>
                </el-radio-group>
                <div class="muted conflict-note">
                    覆盖只换规则，不清掉用户标签；被替换的版本会留存以便回查。
                </div>
            </div>
        </div>

        <div v-if="preview && previewTab === activeTab" class="preview-block">
            <div class="muted import-source">来源：{{ sourceName }}</div>
            <el-descriptions :column="3" border size="small">
                <el-descriptions-item label="书源数">{{
                    preview.count
                }}</el-descriptions-item>
                <el-descriptions-item label="新增">{{
                    preview.counts.new || 0
                }}</el-descriptions-item>
                <el-descriptions-item label="重复">{{
                    preview.counts.duplicate || 0
                }}</el-descriptions-item>
                <el-descriptions-item label="规则冲突">{{
                    preview.counts.conflict || 0
                }}</el-descriptions-item>
                <el-descriptions-item label="将覆盖">{{
                    preview.counts.update || 0
                }}</el-descriptions-item>
                <el-descriptions-item label="无效">{{
                    preview.counts.invalid || 0
                }}</el-descriptions-item>
                <el-descriptions-item label="规则不完整">{{
                    preview.rulesIncomplete
                }}</el-descriptions-item>
            </el-descriptions>
            <el-table
                v-if="preview.items.length"
                :data="preview.items"
                size="small"
                max-height="210"
                style="margin-top: 10px"
            >
                <el-table-column
                    prop="name"
                    label="书源"
                    min-width="160"
                    show-overflow-tooltip
                />
                <el-table-column
                    prop="url"
                    label="域名"
                    min-width="160"
                    show-overflow-tooltip
                >
                    <template #default="scope">{{
                        domainOf(scope.row.url)
                    }}</template>
                </el-table-column>
                <el-table-column label="处理方式" width="100">
                    <template #default="scope">
                        <el-tag
                            size="small"
                            :type="importStatusType(scope.row.status)"
                        >
                            {{ importStatusLabel(scope.row.status) }}
                        </el-tag>
                    </template>
                </el-table-column>
                <el-table-column label="规则" width="110">
                    <template #default="scope">
                        <el-tag
                            v-if="scope.row.rules_incomplete"
                            size="small"
                            type="warning"
                        >
                            规则不完整
                        </el-tag>
                        <span v-else class="muted">—</span>
                    </template>
                </el-table-column>
            </el-table>
        </div>

        <div
            v-if="preview && previewTab === activeTab"
            class="toolbar"
            style="margin-top: 12px"
        >
            <el-button
                type="success"
                :loading="loading"
                :disabled="!rawText"
                @click="doImport"
            >
                确认导入
            </el-button>
        </div>

        <el-descriptions
            v-if="result && resultTab === activeTab && !result.error"
            :column="4"
            border
            size="small"
            style="margin-top: 12px"
        >
            <el-descriptions-item label="新增">{{
                result.new_count
            }}</el-descriptions-item>
            <el-descriptions-item label="已更新">{{
                result.updated_count || 0
            }}</el-descriptions-item>
            <el-descriptions-item label="重复">{{
                result.duplicate_count
            }}</el-descriptions-item>
            <el-descriptions-item label="冲突待审">{{
                result.conflict_count
            }}</el-descriptions-item>
        </el-descriptions>
        <el-alert
            v-else-if="result && resultTab === activeTab"
            type="error"
            :closable="false"
            style="margin-top: 10px"
            :title="result.error"
        />
    </el-dialog>
</template>

<style scoped>
.source-import-tabs :deep(.el-tabs__header) {
    position: sticky;
    top: 0;
    z-index: 5;
    padding-top: 4px;
    background: var(--el-bg-color);
}

.source-import-tabs :deep(.el-tabs__item) {
    padding-right: 14px;
    padding-left: 14px;
}

.conflict-options {
    display: grid;
    grid-template-columns: 104px minmax(0, 1fr);
    gap: 8px;
    margin: 8px 0 4px;
}

.conflict-label {
    padding-top: 5px;
    color: var(--el-text-color-regular);
    font-size: 13px;
}

.conflict-note {
    margin-top: 4px;
}

.import-input-row {
    display: flex;
    width: 100%;
    gap: 8px;
}

.preview-block {
    margin-top: 12px;
}

.import-source {
    margin-bottom: 6px;
}

@media (max-width: 560px) {
    .import-input-row {
        align-items: stretch;
        flex-direction: column;
    }
}
</style>

<style>
.source-import-overlay .el-overlay-dialog {
    overflow: hidden;
}

.source-import-overlay .source-import-dialog .el-dialog__body {
    max-height: max(0px, calc(100dvh - 180px));
    overflow-x: hidden;
    overflow-y: auto;
    scrollbar-gutter: stable;
}

@media (max-width: 600px) {
    .source-import-overlay .source-import-dialog.el-dialog {
        display: flex;
        width: 100vw;
        height: 100dvh;
        max-height: 100dvh;
        margin: 0;
        flex-direction: column;
        overflow: hidden;
        border-radius: 0;
    }

    .source-import-overlay .source-import-dialog .el-dialog__header {
        flex: 0 0 auto;
        padding: calc(14px + env(safe-area-inset-top))
            calc(16px + env(safe-area-inset-right)) 8px
            calc(16px + env(safe-area-inset-left));
    }

    .source-import-overlay .source-import-dialog .el-dialog__body {
        max-height: none;
        flex: 1 1 auto;
        min-height: 0;
        padding: 0 calc(16px + env(safe-area-inset-right))
            calc(16px + env(safe-area-inset-bottom))
            calc(16px + env(safe-area-inset-left));
    }

    .source-import-overlay .source-import-tabs .el-tabs__item {
        min-height: 42px;
        padding-right: 10px;
        padding-left: 10px;
        font-size: 13px;
    }
}
</style>
