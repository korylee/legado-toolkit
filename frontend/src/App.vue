<script setup>
import zhCn from "element-plus/es/locale/lang/zh-cn";
import MainLayout from "./layouts/MainLayout.vue";
</script>

<template>
  <el-config-provider :locale="zhCn">
    <MainLayout />
  </el-config-provider>
</template>
