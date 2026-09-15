<script setup>
// 设置抽屉：只负责外壳与页签，各页签的内容与数据加载都在各自的面板组件里。
// 原 LLMSettingsDrawer.vue 拆成了这个外壳 + LLMSettingsPanel.vue。
import { ref, computed } from "vue";
import CheckSettingsPanel from "./CheckSettingsPanel.vue";
import LLMSettingsPanel from "./LLMSettingsPanel.vue";

const props = defineProps({ modelValue: { type: Boolean, default: false } });
const emit = defineEmits(["update:modelValue"]);

const visible = computed({
  get: () => props.modelValue,
  set: (v) => emit("update:modelValue", v),
});

// 记住上次看的页签。抽屉是 destroy-on-close，但外壳自己的状态留着
const activeTab = ref("check");
</script>

<template>
  <el-drawer v-model="visible" title="设置" direction="rtl" size="640px" destroy-on-close>
    <!-- lazy：页签首次激活才挂载，之后不再卸载。
         所以打开设置不会把模型配置和校验设置都拉一遍；在「校验」里改到一半
         切到「模型」再回来，输入也不会被重置 -->
    <el-tabs v-model="activeTab">
      <el-tab-pane label="校验" name="check" lazy>
        <CheckSettingsPanel />
      </el-tab-pane>
      <el-tab-pane label="模型" name="llm" lazy>
        <LLMSettingsPanel />
      </el-tab-pane>
      <el-tab-pane label="App 连接" name="app" lazy>
        <!-- 还没做。宁可写清楚，也不放一个点了没反应的空表单 -->
        <el-empty :image-size="70"
                  description="尚未实现：App 的 IP / 调试端口 / 连接超时会在这一页配置" />
        <div class="muted" style="text-align: center">
          当前 App 的 IP 在「编辑书源」弹窗里填，端口固定用默认值 1123
        </div>
      </el-tab-pane>
    </el-tabs>
  </el-drawer>
</template>
