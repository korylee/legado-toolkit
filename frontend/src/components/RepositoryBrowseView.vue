<script setup>
import RepositorySourceList from "./RepositorySourceList.vue";

defineProps({
    query: { type: String, default: "" },
    capabilities: { type: Object, default: () => ({}) },
    result: { type: Object, required: true },
    pagination: {
        type: Object,
        default: () => ({ page: 1, pageSize: 20, total: 0 }),
    },
    loading: { type: Boolean, default: false },
    detailsLoading: { type: Boolean, default: false },
    selected: { type: Object, required: true },
    selectedCount: { type: Number, default: 0 },
    display: { type: Object, default: () => ({}) },
});
const emit = defineEmits([
    "update:query",
    "browse",
    "toggle",
    "toggle-page",
    "clear-selection",
    "page",
    "preview",
]);
</script>

<template>
    <div v-if="capabilities.search" class="repository-browse-search">
        <el-input
            :model-value="query"
            clearable
            placeholder="搜索书源名称"
            @update:model-value="emit('update:query', $event)"
            @keyup.enter="emit('browse')"
        />
        <el-button
            type="primary"
            :loading="loading"
            :disabled="!query.trim()"
            @click="emit('browse')"
            >搜索</el-button
        >
    </div>
    <RepositorySourceList
        :items="result.items"
        :pagination="pagination"
        :selected="selected"
        :selected-count="selectedCount"
        :loading="loading"
        :details-loading="detailsLoading"
        :display="display"
        @toggle="(...args) => emit('toggle', ...args)"
        @toggle-page="emit('toggle-page')"
        @clear-selection="emit('clear-selection')"
        @page="emit('page', $event)"
        @preview="emit('preview')"
    />
    <el-empty
        v-if="!loading && !result.items.length"
        :description="
            query.trim()
                ? '没有找到书源'
                : capabilities.list
                  ? '暂无书源'
                  : '输入关键词搜索书源'
        "
    />
</template>

<style scoped>
.repository-browse-search {
    display: flex;
    gap: 8px;
    margin-bottom: 12px;
}
@media (max-width: 560px) {
    .repository-browse-search {
        gap: 6px;
    }
    .repository-browse-search :deep(.el-input) {
        min-width: 0;
    }
    .repository-browse-search :deep(.el-button) {
        min-width: 68px;
    }
}
</style>
