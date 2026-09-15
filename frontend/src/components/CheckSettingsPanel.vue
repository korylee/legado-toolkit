<script setup>
// 「设置」抽屉的「校验」页签：书源校验参数的全局默认值。
//
// 字段本身与它们的渲染方式都来自 utils/checkFields.js + CheckFieldRow——
// 这个文件只负责"读全量、存全量"，不再自己列一遍字段。
// 上下界用后端下发的 limits（AGENTS.md 硬性约定 #7）。
import { ref, onMounted } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { getSettings, patchSettings, resetSettings } from "../api/settings";
import { CHECK_FIELDS } from "../utils/checkFields";
import CheckFieldRow from "./CheckFieldRow.vue";

const form = ref(null);
const limits = ref(null);
const loading = ref(false);
const saving = ref(false);

async function load() {
  loading.value = true;
  try {
    const s = await getSettings();
    form.value = { ...s.values.check };
    limits.value = s.limits;
  } catch (e) {
    ElMessage.error("加载设置失败: " + e.message);
  } finally {
    loading.value = false;
  }
}

onMounted(load);

async function save() {
  saving.value = true;
  try {
    const s = await patchSettings({ check: { ...form.value } });
    // 以后端收敛后的值为准：填了 socks5 之类会被后端拒绝或收敛，界面要跟着变，
    // 不然用户以为存进去了
    form.value = { ...s.values.check };
    ElMessage.success("已保存，下次校验生效");
  } catch (e) {
    ElMessage.error("保存失败: " + e.message);
  } finally {
    saving.value = false;
  }
}

async function restore() {
  try {
    await ElMessageBox.confirm("把校验参数恢复为默认值？", "恢复默认", { type: "warning" });
  } catch (e) {
    return;  // 取消
  }
  try {
    const s = await resetSettings();
    form.value = { ...s.values.check };
    ElMessage.success("已恢复默认");
  } catch (e) {
    ElMessage.error("恢复失败: " + e.message);
  }
}
</script>

<template>
  <div v-loading="loading">
    <el-form v-if="form && limits" label-width="150px" size="small">
      <CheckFieldRow v-for="f in CHECK_FIELDS" :key="f.key" :field="f" :limits="limits"
                     v-model="form[f.key]" />
    </el-form>

    <div class="toolbar" style="margin-top: 12px">
      <el-button size="small" type="danger" plain @click="restore">恢复默认</el-button>
      <span class="grow" />
      <el-button size="small" type="primary" :loading="saving" @click="save">保存</el-button>
    </div>
  </div>
</template>
