<script setup>
// 「设置」抽屉的「校验」页签：书源校验参数的全局默认值。
// 上下界一律用后端下发的 limits 渲染，不在 JS 里再写一份（AGENTS.md 硬性约定 #7）。
import { ref, onMounted } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { getSettings, patchSettings, resetSettings } from "../api/settings";

const form = ref(null);
const limits = ref(null);
const loading = ref(false);
const saving = ref(false);

//: 探测深度的说明文案（值本身来自后端 limits，这里只负责显示成什么）
const DEPTH_LABELS = {
  1: "1 · 浅探测（只验域名与搜索）",
  2: "2 · 验目录（比对章节数）",
  3: "3 · 验正文（抓一章全文）",
};

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
    <el-form v-if="form && limits" label-width="110px" size="small">
      <el-form-item label="并发数">
        <el-input-number v-model="form.concurrency" controls-position="right"
                         :min="limits.concurrency?.[0]" :max="limits.concurrency?.[1]"
                         style="width: 160px" />
        <span class="muted" style="margin-left: 8px">同时发出的请求数</span>
      </el-form-item>

      <el-form-item label="单请求超时">
        <el-input-number v-model="form.timeout" controls-position="right"
                         :min="limits.timeout?.[0]" :max="limits.timeout?.[1]"
                         style="width: 160px" />
        <span class="muted" style="margin-left: 8px">秒</span>
      </el-form-item>

      <el-form-item label="探测深度">
        <el-select v-model="form.probe_depth" style="width: 260px">
          <el-option v-for="d in limits.probe_depth" :key="d" :value="d"
                     :label="DEPTH_LABELS[d] || ('深度 ' + d)" />
        </el-select>
      </el-form-item>

      <el-form-item label="搜索探测">
        <el-switch v-model="form.probe_search" />
        <span class="muted" style="margin-left: 8px">关掉只测域名连通，不验搜索</span>
      </el-form-item>

      <el-form-item label="校验 SSL 证书">
        <el-switch v-model="form.verify_ssl" />
        <span class="muted" style="margin-left: 8px">
          关掉可绕过自签名证书报错，代价是不再校验 TLS 身份
        </span>
      </el-form-item>

      <el-form-item label="代理">
        <el-input v-model="form.proxy" placeholder="http://127.0.0.1:7890"
                  style="width: 260px" clearable />
        <div class="muted">
          留空直连。只支持 http:// 与 https://（socks5 需要额外依赖，本项目未装）。
          <b>填了就是所有校验请求都走它</b>，直连能通的源也会绕一圈。
        </div>
      </el-form-item>

      <el-form-item label="缓存有效期">
        <el-input-number v-model="form.cache_ttl_ok" controls-position="right"
                         :min="limits.cache_ttl_ok?.[0]" :max="limits.cache_ttl_ok?.[1]"
                         style="width: 140px" />
        <span class="muted">天（可用源）</span>
        <el-input-number v-model="form.cache_ttl_other" controls-position="right"
                         :min="limits.cache_ttl_other?.[0]" :max="limits.cache_ttl_other?.[1]"
                         style="width: 140px; margin-left: 16px" />
        <span class="muted">天（其他状态）</span>
        <div class="muted">
          有效期内直接复用校验结果、不重新请求。可用源留久一点；
          其他状态（待验证/需验证/需代理复检）留短一点，免得旧结论一直挂着。
          要这一次全部重测，用工具栏「校验参数 → 忽略缓存」。
        </div>
      </el-form-item>
    </el-form>

    <div class="toolbar" style="margin-top: 12px">
      <el-button size="small" type="danger" plain @click="restore">恢复默认</el-button>
      <span class="grow" />
      <el-button size="small" type="primary" :loading="saving" @click="save">保存</el-button>
    </div>
  </div>
</template>
