<script setup>
import { computed, onMounted, ref } from "vue";
import { ElMessage } from "element-plus";
import {
    getImportRepositoryDetails,
    listImportRepositories,
    listImportRepositoryCollections,
    getImportRepositoryCollection,
    browseImportRepository,
} from "../api/importRepositories";
import RepositoryCollectionsView from "./RepositoryCollectionsView.vue";
import RepositoryBrowseView from "./RepositoryBrowseView.vue";

const emit = defineEmits(["preview"]);
const DETAIL_BATCH_LIMIT = 20;

function emptyBrowseResult() {
    return { items: [], pagination: { page: 1, pageSize: 20, total: 0 } };
}

const repositories = ref([]);
const repositoryId = ref("");
const query = ref("");
const mode = ref("");
const collections = ref([]);
const collectionPagination = ref({ page: 1, pageSize: 20, total: 0 });
const result = ref(emptyBrowseResult());
const pagination = computed(() => result.value.pagination);
const loading = ref(false);
const detailsLoading = ref(false);
const selected = ref(new Map());
// 请求序号守卫：翻页/切仓库/切视图都会并发触发 browsePage，慢的旧响应
// 晚到会覆盖新结果（表现：点第 2 页却显示第 1 页的内容）。与 ImportDialog
// 的 previewRequestId 同一模式——序号不匹配的响应直接丢弃。
let browseRequestId = 0;
const repository = computed(
    () =>
        repositories.value.find((item) => item.id === repositoryId.value) ||
        null,
);
const selectedCount = computed(() => selected.value.size);
const availableViews = computed(() => repository.value?.views || []);
const activeView = computed(
    () => availableViews.value.find((view) => view.id === mode.value) || null,
);

async function loadCollections(nextPage = 1) {
    if (activeView.value?.type !== "collections") return;
    loading.value = true;
    try {
        const response = await listImportRepositoryCollections(
            repositoryId.value,
            nextPage,
        );
        collections.value = response.items || [];
        collectionPagination.value = response.pagination || {
            page: nextPage,
            pageSize: Math.max(collections.value.length, 1),
            total: collections.value.length,
        };
    } catch (error) {
        ElMessage.error("读取书源集失败：" + error.message);
    } finally {
        loading.value = false;
    }
}

async function setMode(nextViewId) {
    if (
        !availableViews.value.some((view) => view.id === nextViewId) ||
        mode.value === nextViewId
    )
        return;
    mode.value = nextViewId;
    query.value = "";
    result.value = emptyBrowseResult();
    selected.value = new Map();
    if (
        activeView.value?.type === "browse" &&
        repository.value?.capabilities?.list
    ) {
        await browsePage(1);
    } else if (
        activeView.value?.type === "collections" &&
        !collections.value.length
    ) {
        await loadCollections();
    }
}

async function selectCollection(collection) {
    detailsLoading.value = true;
    try {
        const response = await getImportRepositoryCollection(
            repositoryId.value,
            collection,
        );
        emit("preview", {
            sources: response.sources || [],
            sourceName: repository.value.name + " / " + collection.name,
        });
    } catch (error) {
        ElMessage.error("获取书源合集失败：" + error.message);
    } finally {
        detailsLoading.value = false;
    }
}

async function loadRepositories() {
    if (repositories.value.length) return repositories.value;
    const response = await listImportRepositories();
    repositories.value = response.items || [];
    repositoryId.value = repositories.value[0]?.id || "";
    const views = repositories.value[0]?.views || [];
    mode.value = views[0]?.id || "";
    if (views[0]?.type === "browse" && repository.value?.capabilities?.list) {
        await browsePage(1);
    } else if (views[0]?.type === "collections") {
        await loadCollections();
    }
    return repositories.value;
}

async function browsePage(nextPage = 1, { resetSelection = false } = {}) {
    if (activeView.value?.type !== "browse") return;
    const queryText = query.value.trim();
    const capabilities = repository.value?.capabilities || {};
    if (resetSelection) {
        selected.value = new Map();
        result.value = emptyBrowseResult();
    }
    if (queryText && !capabilities.search) return;
    if (!queryText && !capabilities.list) {
        result.value = emptyBrowseResult();
        return;
    }
    loading.value = true;
    const requestId = ++browseRequestId;
    try {
        const response = await browseImportRepository(repositoryId.value, {
            query: queryText,
            page: nextPage,
        });
        if (requestId === browseRequestId) result.value = response;
    } catch (error) {
        if (requestId === browseRequestId)
            ElMessage.error(
                (queryText ? "搜索书源失败：" : "读取书源列表失败：") +
                    error.message,
            );
    } finally {
        if (requestId === browseRequestId) loading.value = false;
    }
}

async function selectRepository(id) {
    if (!id) return;
    repositoryId.value = id;
    query.value = "";
    result.value = emptyBrowseResult();
    collections.value = [];
    collectionPagination.value = { page: 1, pageSize: 20, total: 0 };
    selected.value = new Map();
    const views = repository.value?.views || [];
    mode.value = views[0]?.id || "";
    if (views[0]?.type === "browse" && repository.value?.capabilities?.list) {
        await browsePage(1);
    } else if (views[0]?.type === "collections") {
        await loadCollections();
    }
}

function clearSelection() {
    selected.value = new Map();
}

function togglePage() {
    const next = new Map(selected.value);
    const visible = result.value.items || [];
    const allSelected =
        visible.length > 0 && visible.every((item) => next.has(item.id));
    if (allSelected) {
        for (const item of visible) next.delete(item.id);
    } else {
        for (const item of visible) {
            if (next.has(item.id)) continue;
            if (next.size >= DETAIL_BATCH_LIMIT) {
                ElMessage.warning(
                    "一次最多选择 " + DETAIL_BATCH_LIMIT + " 个书源",
                );
                break;
            }
            next.set(item.id, item);
        }
    }
    selected.value = next;
}

function toggle(item, checked) {
    const next = new Map(selected.value);
    if (checked) {
        if (next.size >= DETAIL_BATCH_LIMIT) {
            ElMessage.warning("一次最多选择 " + DETAIL_BATCH_LIMIT + " 个书源");
            return;
        }
        next.set(item.id, item);
    } else {
        next.delete(item.id);
    }
    selected.value = next;
}

async function fetchSelectedSources() {
    if (!repository.value || !selected.value.size)
        throw new Error("先选择书源");
    detailsLoading.value = true;
    try {
        const ids = [...selected.value.keys()];
        const response = await getImportRepositoryDetails(
            repositoryId.value,
            ids,
        );
        const byId = new Map(
            (response.items || []).map((item) => [item.id, item.sources || []]),
        );
        const missing = ids.filter((id) => !byId.has(id));
        if (missing.length) {
            throw new Error(
                "有 " +
                    missing.length +
                    " 条书源详情未返回；选择已保留，可重试",
            );
        }
        const sources = ids.flatMap((id) => byId.get(id));
        if (!sources.length) throw new Error("选中的条目没有返回书源");
        return {
            sources,
            sourceName: repository.value.name + "（" + sources.length + " 条）",
            items: [...selected.value.values()],
        };
    } finally {
        detailsLoading.value = false;
    }
}

const selectedView = computed(() =>
    availableViews.value.find((view) => view.id === mode.value),
);

onMounted(async () => {
    try {
        await loadRepositories();
    } catch (error) {
        ElMessage.error("读取书源仓库列表失败：" + error.message);
    }
});

async function onRepositoryChange(id) {
    try {
        await selectRepository(id);
    } catch (error) {
        ElMessage.error("切换书源仓库失败：" + error.message);
    }
}

async function onModeChange(id) {
    try {
        await setMode(id);
    } catch (error) {
        ElMessage.error("切换仓库浏览方式失败：" + error.message);
    }
}

async function previewSelected() {
    try {
        emit("preview", await fetchSelectedSources());
    } catch (error) {
        ElMessage.error("获取仓库书源失败：" + error.message);
    }
}
</script>

<template>
    <div v-loading="loading || detailsLoading" class="repository-browser">
        <el-tabs v-model="repositoryId" @tab-change="onRepositoryChange">
            <el-tab-pane
                v-for="repository in repositories"
                :key="repository.id"
                :label="repository.name"
                :name="repository.id"
            />
        </el-tabs>

        <template v-if="repository">
            <el-tabs
                v-if="availableViews.length > 1"
                v-model="mode"
                @tab-change="onModeChange"
            >
                <el-tab-pane
                    v-for="view in availableViews"
                    :key="view.id"
                    :label="view.label"
                    :name="view.id"
                />
            </el-tabs>
            <RepositoryBrowseView
                v-if="selectedView?.type === 'browse'"
                v-model:query="query"
                :capabilities="repository?.capabilities || {}"
                :result="result"
                :pagination="pagination"
                :loading="loading"
                :details-loading="detailsLoading"
                :selected="selected"
                :selected-count="selectedCount"
                :display="repository?.display || {}"
                :on-preview="previewSelected"
                @browse="browsePage(1, { resetSelection: true })"
                @toggle="toggle"
                @toggle-page="togglePage"
                @clear-selection="clearSelection"
                @page="browsePage($event)"
            />
            <RepositoryCollectionsView
                v-else-if="selectedView?.type === 'collections'"
                :collections="collections"
                :pagination="collectionPagination"
                :loading="loading"
                :details-loading="detailsLoading"
                @select-collection="selectCollection"
                @collection-page="loadCollections"
            />
            <el-empty
                v-else-if="repository"
                description="该仓库没有已实现的浏览方式"
            />
        </template>
        <el-empty v-else-if="!loading" description="没有可用的书源仓库" />
    </div>
</template>

<style scoped>
.repository-browser {
    min-height: 300px;
}
</style>
