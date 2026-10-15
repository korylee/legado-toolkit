<script setup>
import { computed, ref, watch } from "vue";
import { ElMessage } from "element-plus";
import { getDetail, listSources, saveSource } from "../api/sources";
import { normalizeForSave } from "../utils/sourceSave";
import {
    assignRenameBadges,
    renamedSourceName,
    RENAME_BADGES,
} from "../utils/renameBadges";

const sources = ref([]);
const loading = ref(false);
const saving = ref(false);
const query = ref("");
const page = ref(1);
const total = ref(0);
const props = defineProps({ initialQuery: { type: String, default: "" } });
const emit = defineEmits(["changed"]);
const selected = ref(new Set());
const selectedOrder = ref([]);
const knownNames = ref(new Map());
const pageSize = 10;
const bulkRenameVisible = ref(false);
const bulkRenameSuffix = ref("");
const badgeMode = ref("incremental");
const manualBadges = ref({});
const previewVisible = ref(false);
const previewRows = ref([]);
const draggedUrl = ref("");
const manualBadgeOptions = ["", ...RENAME_BADGES];
let sourceRequest = 0;
const sourceNames = computed(() => new Map(knownNames.value));
const availableSources = computed(() =>
    sources.value.filter((source) => !selected.value.has(source.source_url)),
);
const selectedSources = computed(() =>
    selectedOrder.value
        .filter((url) => selected.value.has(url))
        .map((url) => ({
            source_url: url,
            name: sourceNames.value.get(url) || url,
        })),
);
const renamePreview = computed(() => {
    const assigned = assignRenameBadges(
        selectedOrder.value.filter((url) => selected.value.has(url)),
        badgeMode.value,
        manualBadges.value,
    );
    return selectedSources.value.map((source, index) => ({
        ...source,
        badge: assigned[index],
        newName: renamedSourceName(
            source.name,
            bulkRenameSuffix.value,
            assigned[index],
        ),
    }));
});
function toggleAvailable(url, checked) {
    const next = new Set(selected.value);
    if (checked) {
        next.add(url);
        if (!selectedOrder.value.includes(url))
            selectedOrder.value = [...selectedOrder.value, url];
    } else {
        next.delete(url);
        selectedOrder.value = selectedOrder.value.filter(
            (item) => item !== url,
        );
    }
    selected.value = next;
    previewRows.value = [];
    previewVisible.value = false;
}
function toggleSelected(url) {
    previewRows.value = [];
    previewVisible.value = false;
    const next = new Set(selected.value);
    next.delete(url);
    selected.value = next;
    selectedOrder.value = selectedOrder.value.filter((item) => item !== url);
    const manual = { ...manualBadges.value };
    delete manual[url];
    manualBadges.value = manual;
}
function moveSelected(url, targetUrl) {
    previewRows.value = [];
    previewVisible.value = false;
    const order = [...selectedOrder.value];
    const from = order.indexOf(url);
    const to = order.indexOf(targetUrl);
    if (from < 0 || to < 0 || from === to) return;
    order.splice(from, 1);
    order.splice(to, 0, url);
    selectedOrder.value = order;
}
function beginRename() {
    if (!selected.value.size) return ElMessage.warning("先勾选要改名的书源");
    bulkRenameSuffix.value = "";
    badgeMode.value = "incremental";
    manualBadges.value = {};
    previewRows.value = [];
    previewVisible.value = false;
    bulkRenameVisible.value = true;
}
function setManualBadge(url, badge) {
    manualBadges.value = { ...manualBadges.value, [url]: badge || "" };
    previewRows.value = [];
    previewVisible.value = false;
}
async function loadSources() {
    const requestId = ++sourceRequest;
    if (!query.value.trim()) {
        sources.value = [];
        total.value = 0;
        loading.value = false;
        return;
    }
    loading.value = true;
    try {
        const res = await listSources({
            q: query.value,
            limit: pageSize,
            offset: (page.value - 1) * pageSize,
            order: "name",
        });
        if (requestId !== sourceRequest) return;
        sources.value = res.items || [];
        total.value = res.total || 0;
        const names = new Map(knownNames.value);
        for (const source of sources.value)
            names.set(source.source_url, source.name || source.source_url);
        knownNames.value = names;
    } catch (e) {
        if (requestId === sourceRequest)
            ElMessage.error("读取书源失败：" + e.message);
    } finally {
        if (requestId === sourceRequest) loading.value = false;
    }
}
async function search() {
    page.value = 1;
    await loadSources();
}
watch(
    () => props.initialQuery,
    async (value) => {
        query.value = value;
        page.value = 1;
        selected.value = new Set();
        selectedOrder.value = [];
        knownNames.value = new Map();
        await loadSources();
    },
    { immediate: true },
);
function resetSelection() {
    selected.value = new Set();
    selectedOrder.value = [];
    bulkRenameSuffix.value = "";
    manualBadges.value = {};
    previewRows.value = [];
    previewVisible.value = false;
}
async function prepareRenamePreview() {
    if (!selectedSources.value.length)
        return ElMessage.warning("先勾选要改名的书源");
    previewRows.value = [];
    previewVisible.value = false;
    saving.value = true;
    try {
        const rows = [];
        for (const picked of renamePreview.value) {
            const response = await getDetail(picked.source_url);
            const source = response?.source;
            if (!source) throw new Error(`书源不存在：${picked.source_url}`);
            const currentName = String(source.bookSourceName || "");
            rows.push({
                url: picked.source_url,
                source,
                oldName: currentName,
                newName: renamedSourceName(
                    currentName,
                    bulkRenameSuffix.value,
                    picked.badge,
                ),
                badge: picked.badge,
            });
        }
        // 冲突判据：新名与**库中其他在用源**或**本批另一行的目标名**相撞。
        // 一次拉全量名册本地比对——原来逐个新名发一次搜索请求（20 条 = 20 个
        // 串行往返），且旧判据漏了一种误报：新名恰好等于本批另一条的**旧名**
        // （那条即将改名让出这个名字），会被当成「库里已存在」标红。
        const takenNames = new Set();
        for (let offset = 0; ; offset += 500) {
            const page = await listSources({
                limit: 500, offset, order: "id",
            });
            for (const item of page.items || []) takenNames.add(item.name);
            if ((page.items || []).length < 500) break;
        }
        // 本次改名会把这批旧名让出来：旧名不该算「已占用」
        const releasedNames = new Set(rows.map((row) => row.oldName));
        const targetCount = new Map();
        for (const row of rows)
            targetCount.set(row.newName, (targetCount.get(row.newName) || 0) + 1);
        const conflictOf = (newName) =>
            targetCount.get(newName) > 1
            || (takenNames.has(newName) && !releasedNames.has(newName));
        previewRows.value = rows.map((row) => ({
            ...row,
            conflict: conflictOf(row.newName),
        }));
        previewVisible.value = true;
    } catch (error) {
        ElMessage.error("读取最新书源名称失败：" + error.message);
    } finally {
        saving.value = false;
    }
}
async function applyRenamePreview() {
    saving.value = true;
    const succeeded = [];
    const failed = [];
    try {
        // 并发保存：每条是独立的 upsert 单行事务，没有顺序依赖——
        // 原来逐条串行，20 条就是 20 次串行往返，中途网慢要干等十几秒。
        const outcomes = await Promise.allSettled(
            previewRows.value.map((row) =>
                saveSource(
                    normalizeForSave({
                        ...row.source,
                        bookSourceName: row.newName,
                    }),
                ).then(() => row),
            ),
        );
        for (let i = 0; i < outcomes.length; i += 1) {
            const outcome = outcomes[i];
            const row = previewRows.value[i];
            if (outcome.status === "fulfilled") succeeded.push(row);
            else
                failed.push({
                    url: row.url,
                    error: outcome.reason?.message || String(outcome.reason),
                });
        }
        if (succeeded.length) {
            const names = new Map(knownNames.value);
            for (const row of succeeded) names.set(row.url, row.newName);
            knownNames.value = names;
            sources.value = sources.value.map((source) => {
                const saved = succeeded.find(
                    (row) => row.url === source.source_url,
                );
                return saved ? { ...source, name: saved.newName } : source;
            });
            emit("changed");
        }
        previewVisible.value = false;
        bulkRenameVisible.value = false;
        if (failed.length) {
            ElMessage.error(
                `已保存 ${succeeded.length} 条，${failed.length} 条失败：${failed.map((row) => row.url + "（" + row.error + "）").join("；")}`,
            );
        } else {
            ElMessage.success(`已改名 ${succeeded.length} 条书源`);
        }
    } finally {
        saving.value = false;
    }
}
</script>

<template>
    <div class="rename-workspace">
        <header class="rename-header">
            <div class="rename-title-group">
                <p>搜索后勾选书源；右侧拖动排序，排序决定递增角标分配顺序。</p>
            </div>
            <el-button :disabled="!selected.size" @click="resetSelection"
                >重置选择</el-button
            >
        </header>
        <div class="rename-panels">
            <section class="rename-panel">
                <div class="rename-panel-head">
                    <b>搜索书源</b
                    ><span>{{
                        query ? `命中 ${total} 条` : "每页 10 条"
                    }}</span>
                </div>
                <div class="rename-search">
                    <el-input
                        v-model="query"
                        clearable
                        placeholder="输入名称或地址"
                        @keyup.enter="search"
                        @clear="search"
                    />
                    <el-button @click="search">搜索</el-button>
                </div>
                <div v-loading="loading" class="rename-list">
                    <label
                        v-for="source in availableSources"
                        :key="source.source_url"
                        class="rename-row"
                    >
                        <el-checkbox
                            :model-value="selected.has(source.source_url)"
                            @change="toggleAvailable(source.source_url, $event)"
                        />
                        <span class="rename-source"
                            ><b>{{ source.name || "（无名）" }}</b
                            ><small>{{ source.source_url }}</small></span
                        >
                    </label>
                    <div v-if="!query" class="rename-empty">
                        输入名称或地址搜索书源
                    </div>
                    <div
                        v-else-if="!loading && !availableSources.length"
                        class="rename-empty"
                    >
                        没有可添加的搜索结果
                    </div>
                </div>
                <el-pagination
                    v-if="total"
                    v-model:current-page="page"
                    :page-size="pageSize"
                    :total="total"
                    layout="total, prev, pager, next"
                    @current-change="loadSources"
                />
            </section>
            <section class="rename-panel">
                <div class="rename-panel-head">
                    <b>已选书源</b
                    ><span>{{ selectedOrder.length }} 条 · 拖动排序</span>
                </div>
                <div class="rename-list rename-selected-list">
                    <div
                        v-for="(url, index) in selectedOrder"
                        :key="url"
                        class="rename-row rename-selected-row"
                        draggable="true"
                        @dragstart="draggedUrl = url"
                        @dragover.prevent
                        @drop="moveSelected(draggedUrl, url)"
                    >
                        <el-checkbox
                            :model-value="selected.has(url)"
                            @change="toggleSelected(url)"
                        />
                        <span class="rename-order">{{ index + 1 }}</span>
                        <span class="rename-source"
                            ><b>{{ sourceNames.get(url) || url }}</b
                            ><small>{{ url }}</small></span
                        >
                        <el-button
                            link
                            :disabled="index === 0"
                            @click="moveSelected(url, selectedOrder[index - 1])"
                            >↑</el-button
                        >
                        <el-button
                            link
                            :disabled="index === selectedOrder.length - 1"
                            @click="moveSelected(url, selectedOrder[index + 1])"
                            >↓</el-button
                        >
                    </div>
                    <div v-if="!selectedOrder.length" class="rename-empty">
                        从左侧勾选书源
                    </div>
                </div>
            </section>
        </div>
        <div class="rename-actions">
            <el-button :disabled="!selected.size" @click="beginRename"
                >批量改名与角标</el-button
            >
        </div>
        <el-dialog
            v-model="bulkRenameVisible"
            title="批量改名与角标"
            width="620px"
            append-to-body
        >
            <el-input
                v-model="bulkRenameSuffix"
                placeholder="可选：给所有名称追加文本"
                maxlength="60"
            />
            <el-radio-group v-model="badgeMode" class="rename-badge-mode">
                <el-radio value="incremental">按右侧顺序递增</el-radio>
                <el-radio value="manual">逐条手工指定</el-radio>
            </el-radio-group>
            <div v-if="badgeMode === 'manual'" class="rename-manual-list">
                <div
                    v-for="source in selectedSources"
                    :key="source.source_url"
                    class="rename-manual-row"
                >
                    <span>{{ source.name }}</span>
                    <el-select
                        :model-value="manualBadges[source.source_url] || ''"
                        placeholder="不加角标"
                        clearable
                        @change="setManualBadge(source.source_url, $event)"
                    >
                        <el-option
                            v-for="badge in manualBadgeOptions"
                            :key="badge || 'none'"
                            :label="badge || '不加角标'"
                            :value="badge"
                        />
                    </el-select>
                </div>
            </div>
            <div class="rename-preview-actions">
                <el-button :loading="saving" @click="prepareRenamePreview"
                    >重新查询并预览名称</el-button
                >
            </div>
            <div v-if="previewVisible" class="rename-preview-list">
                <div
                    v-for="row in previewRows"
                    :key="row.url"
                    class="rename-preview-row"
                >
                    <span>{{ row.oldName }}</span
                    ><span>→</span><b>{{ row.newName }}</b>
                    <el-tag v-if="row.conflict" type="danger" size="small"
                        >名称冲突</el-tag
                    >
                </div>
            </div>
            <template #footer>
                <el-button @click="bulkRenameVisible = false">取消</el-button>
                <el-button
                    type="primary"
                    :loading="saving"
                    :disabled="!previewVisible"
                    @click="applyRenamePreview"
                    >保存改名</el-button
                >
            </template>
        </el-dialog>
    </div>
</template>

<style scoped>
.rename-workspace {
    display: flex;
    min-width: 0;
    min-height: 0;
    height: 100%;
    flex-direction: column;
    gap: 12px;
}
.rename-header {
    display: flex;
    flex: 0 0 auto;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding-bottom: 10px;
    border-bottom: 1px solid var(--el-border-color-lighter);
}
.rename-title-group h2 {
    margin: 0;
    color: var(--el-text-color-primary);
    font-size: 19px;
    line-height: 1.3;
}
.rename-title-group p {
    margin: 3px 0 0;
    color: var(--el-text-color-secondary);
    font-size: 12px;
}
.rename-panels {
    display: grid;
    min-width: 0;
    min-height: 0;
    flex: 1 1 0;
    grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
    gap: 10px;
}
.rename-panel {
    display: flex;
    min-width: 0;
    min-height: 0;
    flex-direction: column;
    gap: 7px;
    border: 1px solid var(--el-border-color);
    border-radius: 6px;
    padding: 9px;
}
.rename-panel-head,
.rename-search {
    display: flex;
    flex: 0 0 auto;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
}
.rename-panel-head span {
    color: var(--el-text-color-secondary);
    font-size: 12px;
}
.rename-search .el-input {
    min-width: 0;
    flex: 1 1 auto;
}
.rename-list {
    display: flex;
    min-height: 0;
    flex: 1 1 0;
    flex-direction: column;
    overflow-y: auto;
    border: 1px solid var(--el-border-color-lighter);
    border-radius: 5px;
}
.rename-row {
    display: flex;
    min-height: 42px;
    flex: 0 0 auto;
    align-items: center;
    gap: 7px;
    padding: 4px 8px;
    border-bottom: 1px solid var(--el-border-color-lighter);
}
.rename-selected-row {
    cursor: grab;
}
.rename-selected-row:active {
    cursor: grabbing;
}
.rename-source {
    display: flex;
    min-width: 0;
    flex: 1 1 auto;
    flex-direction: column;
}
.rename-source b,
.rename-source small {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}
.rename-source b {
    font-size: 13px;
    line-height: 17px;
}
.rename-source small {
    color: var(--el-text-color-secondary);
    font-size: 11px;
    line-height: 14px;
}
.rename-order {
    width: 20px;
    color: var(--el-text-color-secondary);
    font-size: 11px;
    text-align: center;
}
.rename-empty {
    display: grid;
    min-height: 90px;
    flex: 1 1 auto;
    place-items: center;
    color: var(--el-text-color-secondary);
    font-size: 13px;
}
.rename-actions {
    display: flex;
    flex: 0 0 auto;
    justify-content: flex-end;
}
.rename-badge-mode {
    display: flex;
    margin: 14px 0 10px;
    gap: 16px;
}
.rename-manual-list,
.rename-preview-list {
    max-height: 220px;
    overflow-y: auto;
    border: 1px solid var(--el-border-color-lighter);
    border-radius: 5px;
}
.rename-manual-row,
.rename-preview-row {
    display: flex;
    min-height: 36px;
    align-items: center;
    gap: 8px;
    padding: 4px 8px;
    border-bottom: 1px solid var(--el-border-color-lighter);
}
.rename-manual-row span {
    min-width: 0;
    flex: 1 1 auto;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}
.rename-manual-row .el-select {
    width: 105px;
}
.rename-preview-actions {
    display: flex;
    justify-content: flex-end;
    margin: 9px 0;
}
.rename-preview-old {
    max-width: 38%;
    overflow: hidden;
    color: var(--el-text-color-secondary);
    text-overflow: ellipsis;
    white-space: nowrap;
}
.rename-preview-row b {
    min-width: 0;
    flex: 1 1 auto;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}
@media (max-width: 720px) {
    .rename-header {
        align-items: flex-start;
        flex-direction: column;
    }
    .rename-panels {
        grid-template-columns: minmax(0, 1fr);
        grid-template-rows: minmax(0, 1fr) minmax(0, 1fr);
    }
}
</style>
