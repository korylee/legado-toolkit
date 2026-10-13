<script setup>
defineProps({
  collections: { type: Array, default: () => [] },
  loading: { type: Boolean, default: false },
  detailsLoading: { type: Boolean, default: false },
});
const emit = defineEmits(["select-collection"]);
</script>

<template>
  <div class="repository-collections">
    <el-empty v-if="!collections.length && !loading" description="暂无书源合集" />
    <el-card
      v-for="collection in collections"
      :key="collection.id"
      shadow="hover"
      class="repository-collection-card"
    >
      <div class="collection-copy">
        <strong>{{ collection.name }}</strong>
        <span v-if="collection.description" class="muted collection-description">
          {{ collection.description }}
        </span>
        <span class="muted collection-count">
          <template v-if="collection.total != null">{{ collection.total }} 条书源</template>
          <template v-if="collection.author"> · {{ collection.author }}</template>
          <template v-if="collection.category"> · {{ collection.category }}</template>
          <template v-if="collection.updated_at"> · 更新于 {{ collection.updated_at }}</template>
        </span>
      </div>
      <el-button
        type="primary"
        :loading="detailsLoading"
        :disabled="detailsLoading"
        @click="emit('select-collection', collection)"
      >
        预览并导入
      </el-button>
    </el-card>
  </div>
</template>

<style scoped>
.repository-collections {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.repository-collection-card {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.repository-collection-card :deep(.el-card__body) {
  display: flex;
  width: 100%;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 12px 14px;
}
.collection-copy {
  display: flex;
  min-width: 0;
  flex-direction: column;
  gap: 4px;
}
.collection-description {
  display: -webkit-box;
  overflow: hidden;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
  white-space: pre-line;
}
.collection-count {
  font-size: 12px;
}
@media (max-width: 560px) {
  .repository-collection-card :deep(.el-card__body) {
    align-items: flex-start;
  }
  .repository-collection-card :deep(.el-button) {
    flex: 0 0 auto;
  }
}
</style>
