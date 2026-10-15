<script setup>
import { domainOf } from "../utils/domain";

const props = defineProps({
    items: { type: Array, default: () => [] },
    pagination: {
        type: Object,
        default: () => ({ page: 1, pageSize: 20, total: 0 }),
    },
    selected: { type: Object, required: true },
    selectedCount: { type: Number, default: 0 },
    loading: { type: Boolean, default: false },
    detailsLoading: { type: Boolean, default: false },
    display: { type: Object, default: () => ({}) },
    preview: { type: Function, default: null },
});
const emit = defineEmits(["toggle", "toggle-page", "clear-selection", "page"]);

function updatedAtOf(item) {
    const value = item[props.display.updated_at];
    if (typeof value === "string" && value && !/^\d+(\.\d+)?$/.test(value)) {
        return value;
    }
    const timestamp = Number(value);
    if (!Number.isFinite(timestamp) || timestamp <= 0) return "更新时间未知";
    const date = new Date(timestamp < 1e12 ? timestamp * 1000 : timestamp);
    return Number.isNaN(date.getTime())
        ? "更新时间未知"
        : date.toLocaleString();
}
</script>

<template>
    <div v-if="items.length" class="repository-results">
        <div class="repository-results-toolbar">
            <span class="muted"
                >共 {{ pagination.total }} 条，已选 {{ selectedCount }} 条（最多
                20 条）</span
            >
            <div>
                <el-button link size="small" @click="emit('toggle-page')"
                    >选中本页</el-button
                >
                <el-button
                    v-if="selectedCount"
                    link
                    size="small"
                    @click="emit('clear-selection')"
                    >清空选择</el-button
                >
            </div>
        </div>
        <div class="repository-source-grid">
            <el-card
                v-for="item in items"
                :key="item.id"
                shadow="never"
                class="repository-card"
                :class="{ 'is-selected': selected.has(item.id) }"
            >
                <div class="repository-card-main">
                    <el-checkbox
                        :model-value="selected.has(item.id)"
                        @change="(checked) => emit('toggle', item, checked)"
                    />
                    <div class="repository-card-copy">
                        <div class="repository-card-title">
                            <span>{{ item[display.title] || item.name }}</span>
                            <el-tag
                                v-if="item.in_library"
                                size="small"
                                type="info"
                            >
                                已存在
                            </el-tag>
                            <el-tag v-else size="small" type="success">
                                未入库
                            </el-tag>
                        </div>
                        <div class="repository-card-meta">
                            <span
                                v-if="display.domain && item[display.domain]"
                                >{{ domainOf(item[display.domain]) }}</span
                            >
                            <span
                                v-if="
                                    display.updated_at &&
                                    item[display.updated_at]
                                "
                                >{{ updatedAtOf(item) }}</span
                            >
                            <span v-if="display.author && item[display.author]"
                                >用户：{{ item[display.author] }}</span
                            >
                            <span
                                v-if="display.version && item[display.version]"
                                >{{ item[display.version] }}</span
                            >
                            <span
                                v-if="
                                    display.features && item[display.features]
                                "
                                >{{ item[display.features] }}</span
                            >
                            <span
                                v-if="
                                    display.downloads && item[display.downloads]
                                "
                                >下载：{{ item[display.downloads] }}</span
                            >
                        </div>
                    </div>
                </div>
            </el-card>
        </div>
        <div class="repository-results-footer">
            <el-pagination
                small
                background
                layout="total, prev, pager, next"
                :current-page="pagination.page"
                :page-count="
                    Math.max(
                        1,
                        Math.ceil(pagination.total / pagination.pageSize),
                    )
                "
                :page-size="pagination.pageSize"
                :total="pagination.total"
                :disabled="loading"
                @current-change="(value) => emit('page', value)"
            />
            <el-button
                type="primary"
                :loading="detailsLoading"
                :disabled="!selectedCount"
                @click="preview?.()"
                >获取所选详情（{{ selectedCount }}）</el-button
            >
        </div>
    </div>
    <el-empty v-else-if="loading" description="正在加载…" />
</template>

<style scoped>
.repository-results {
    display: flex;
    flex-direction: column;
    gap: 8px;
}
.repository-results-toolbar,
.repository-results-footer {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
}
.repository-card-main {
    display: flex;
    align-items: center;
    gap: 12px;
}
.repository-card-copy {
    min-width: 0;
    flex: 1;
}
.repository-card-title,
.repository-card-meta {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
}
.repository-card-title > span {
    overflow: hidden;
    font-weight: 600;
    text-overflow: ellipsis;
    white-space: nowrap;
}
.repository-card-meta {
    justify-content: flex-start;
    margin-top: 6px;
    color: var(--el-text-color-secondary);
    font-size: 12px;
}
.repository-results-footer {
    flex-wrap: wrap;
    margin-top: 8px;
}

.repository-source-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 6px 10px;
}
.repository-card {
    min-width: 0;
    margin: 0;
    border-radius: 7px;
    transition: border-color 0.15s ease;
}
.repository-card:hover {
    border-color: var(--el-color-primary-light-5);
}
.repository-card.is-selected {
    border-color: var(--el-color-primary-light-3);
    background: var(--el-color-primary-light-9);
}
.repository-card :deep(.el-card__body) {
    padding: 7px 10px;
}
.repository-card-main {
    min-height: 48px;
    gap: 8px;
}
.repository-card :deep(.el-checkbox) {
    min-height: 48px;
    width: 36px;
    flex: 0 0 36px;
    justify-content: center;
    margin-right: 0;
}
.repository-card-title > span {
    min-width: 0;
    flex: 1;
}
.repository-card-meta {
    min-width: 0;
    gap: 8px;
    flex-wrap: wrap;
}
.repository-card-meta > span {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}
.repository-card-meta > span:first-child {
    min-width: 0;
    flex: 1;
}
.repository-card-meta > span:last-child {
    flex: 0 0 auto;
}
.repository-results-footer {
    position: sticky;
    bottom: 0;
    z-index: 4;
    padding: 8px 0;
    border-top: 1px solid var(--el-border-color-lighter);
    background: var(--el-bg-color);
}
.repository-results-footer .el-button {
    min-height: 40px;
}
@media (max-width: 640px) {
    .repository-source-grid {
        grid-template-columns: minmax(0, 1fr);
        gap: 6px;
    }
    .repository-results-toolbar {
        align-items: flex-start;
        flex-direction: column;
        gap: 4px;
    }
    .repository-results-footer {
        display: grid;
        grid-template-columns: minmax(0, 1fr);
        justify-items: stretch;
        gap: 8px;
    }
    .repository-results-footer .el-pagination {
        justify-content: center;
    }
    .repository-results-footer .el-button {
        width: 100%;
    }
}
</style>
