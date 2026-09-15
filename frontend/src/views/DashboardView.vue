<script setup>
import { ref, onMounted } from "vue";
import { getStats } from "../api/sources";

const stats = ref(null);
const HEALTH_CN = { ok: "✅可用", dead: "❌失效", auth: "🔒需验证", gfw: "🌐需翻墙", None: "未校验" };
// 3 = Legado 的「只提供下载服务的网站」；Legado 无 4，不设该项
const TYPE_CN = { 0: "📖小说", 1: "🎧听书", 2: "🎨漫画", 3: "📥下载" };

onMounted(async () => { try { stats.value = await getStats(); } catch (e) {} });
</script>

<template>
  <div class="page">
    <el-alert type="info" :closable="false" show-icon style="margin-bottom: 12px">
      这里之后放 <b>diagnose 四类归因</b>（死站 / 规则漂移 / 站点转型 / 需验证）
      和 <b>AI 修复 diff 审阅</b>（左旧规则 / 中新规则 / 右回放结果）。
      数据源已就绪：<code>diagnosis</code> 与 <code>repairs</code> 表。
    </el-alert>

    <el-row :gutter="16" v-if="stats">
      <el-col :span="8">
        <el-card shadow="never" header="总览">
          <p>源总数：<b>{{ stats.sources }}</b></p>
          <p>校验记录：<b>{{ stats.checks }}</b></p>
        </el-card>
      </el-col>
      <el-col :span="8">
        <el-card shadow="never" header="类型分布">
          <p v-for="(n, k) in stats.types" :key="k">
            {{ TYPE_CN[k] || k }}：<b>{{ n }}</b>
          </p>
        </el-card>
      </el-col>
      <el-col :span="8">
        <el-card shadow="never" header="健康度分布">
          <p v-for="(n, k) in stats.health" :key="k">
            {{ HEALTH_CN[k] || k }}：<b>{{ n }}</b>
          </p>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>
