<script setup>
import { canonicalTag, sourceTypes } from "../utils/tags";

const props = defineProps({
  source: { type: Object, required: true },
  userTags: { type: Array, default: () => [] },
  userTagOptions: { type: Array, default: () => [] },
  isNew: { type: Boolean, default: false },
  urlEditable: { type: Boolean, default: false },
});
const emit = defineEmits(["update:source", "update:userTags"]);
function set(field, value) {
  emit("update:source", { ...props.source, [field]: value });
}
function setUserTags(value) {
  const normalized = [...new Set((value || []).map((tag) => canonicalTag(tag)).filter(Boolean))];
  emit("update:userTags", normalized);
}
</script>

<template>
  <el-form label-position="top" size="small" class="source-fields">
    <el-form-item label="源名称"><el-input :model-value="source.bookSourceName || ''" @update:model-value="set('bookSourceName', $event)" /></el-form-item>
    <el-form-item label="源地址">
      <el-input :model-value="source.bookSourceUrl || ''" :disabled="!(urlEditable || isNew)" @update:model-value="set('bookSourceUrl', $event)" />
      <span v-if="!(urlEditable || isNew)" class="muted">已有源的地址不可直接修改；需要更换请使用另存为。</span>
    </el-form-item>
    <el-form-item label="书源类型"><el-select :model-value="Number(source.bookSourceType) || 0" @update:model-value="set('bookSourceType', Number($event))"><el-option v-for="t in sourceTypes" :key="t.value" :value="t.value" :label="t.tag" /></el-select></el-form-item>
    <slot name="editor-fields"></slot>
    <el-form-item label="用户标签"><el-select :model-value="userTags" multiple filterable allow-create default-first-option :reserve-keyword="false" style="width: 100%" placeholder="多个标签用逗号分隔" @update:model-value="setUserTags"><el-option v-for="t in userTagOptions" :key="t.tag || t" :value="t.tag || t" :label="t.tag ? t.tag + ' (' + t.count + ')' : t" /></el-select></el-form-item>
    <el-form-item label="发现页 URL"><el-input :model-value="source.exploreUrl || ''" @update:model-value="set('exploreUrl', $event)" /></el-form-item>
    <el-form-item label="登录页 URL"><el-input :model-value="source.loginUrl || ''" @update:model-value="set('loginUrl', $event)" /></el-form-item>
  </el-form>
</template>

<style scoped>
.source-fields .muted { display: block; margin-top: 4px; color: var(--el-text-color-secondary); font-size: 12px; }
</style>
