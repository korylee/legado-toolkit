<script setup>
// 渲染 checksFields.js 里的一个字段。两个面板共用，字段的**渲染方式**也只有
// 这一份——否则「设置」里改了控件，「本次覆盖」里还是旧的，两边看起来是两个东西。
import { computed } from "vue";
import { DEPTH_LABELS } from "../utils/checkFields";

const props = defineProps({
  field: { type: Object, required: true },
  modelValue: { default: undefined },
  limits: { type: Object, default: () => ({}) },
  //: 紧凑模式：覆盖表单那个 popover 只有 330px，控件占满整行、不显示长说明
  compact: { type: Boolean, default: false },
});
const emit = defineEmits(["update:modelValue"]);

const value = computed({
  get: () => props.modelValue,
  set: (v) => emit("update:modelValue", v),
});
const width = computed(() => (props.compact ? "100%" : "160px"));
</script>

<template>
  <el-form-item :label="field.label">
    <el-input-number v-if="field.type === 'number'" v-model="value" size="small"
                     controls-position="right" :style="{ width }"
                     :min="limits[field.key]?.[0]" :max="limits[field.key]?.[1]" />
    <el-select v-else-if="field.type === 'depth'" v-model="value" size="small"
               :style="{ width: compact ? '100%' : '260px' }">
      <el-option v-for="d in (limits.probe_depth || [1, 2, 3])" :key="d" :value="d"
                 :label="DEPTH_LABELS[d] || ('深度 ' + d)" />
    </el-select>
    <el-switch v-else-if="field.type === 'bool'" v-model="value" size="small" />
    <el-input v-else v-model="value" size="small" clearable
              :placeholder="field.placeholder" :style="{ width: compact ? '100%' : '260px' }" />

    <span v-if="field.suffix && !compact" class="muted" style="margin-left: 8px">
      {{ field.suffix }}
    </span>
    <div v-if="field.hint && !compact" class="muted">{{ field.hint }}</div>
  </el-form-item>
</template>
