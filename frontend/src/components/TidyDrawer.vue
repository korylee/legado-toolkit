<script setup>
// 书源整理工具：重复筛选、批量改名与名称清洗彼此独立，各自是一个自足的组件——
// 本体只管抽屉壳子（工具切换 + 三栏骨架），不碰任何工具的内部状态。
import { ref, computed, watch } from "vue";
import DupsTool from "./DupsTool.vue";
import SourceFamilyManager from "./SourceFamilyManager.vue";
import NameCleanTool from "./NameCleanTool.vue";

const props = defineProps({
    modelValue: { type: Boolean, default: false },
    initialQuery: { type: String, default: "" },
});
const emit = defineEmits(["update:modelValue", "changed"]);

const visible = computed({
    get: () => props.modelValue,
    set: (v) => emit("update:modelValue", v),
});

const TOOLS = [
    { id: "dups", label: "重复筛选" },
    { id: "rename", label: "批量改名" },
    { id: "names", label: "名称清洗" },
];
const step = ref("dups");
const renameQuery = ref("");

function goStep(s) {
    step.value = s;
    if (s === "rename") renameQuery.value = props.initialQuery;
}

watch(
    () => props.modelValue,
    (v) => {
        if (!v) return;
        step.value = props.initialQuery ? "rename" : "dups";
        renameQuery.value = props.initialQuery;
    },
);
</script>

<template>
    <el-drawer
        v-model="visible"
        title="整理源"
        size="900px"
        destroy-on-close
        class="tidy-drawer"
    >
        <el-radio-group v-model="step" class="tidy-nav" @change="goStep">
            <el-radio-button
                v-for="tool in TOOLS"
                :key="tool.id"
                :value="tool.id"
            >
                {{ tool.label }}
            </el-radio-button>
        </el-radio-group>

        <!-- 工具页共用一副上中下三栏骨架：.tidy-head 固定 / .tidy-main 唯一滚动区 /
         .tidy-foot 固定。让工具导航与底部按钮永远在视野里——列表有 1600+ 条，
         整页滚动时「我批到哪了」和「下一步按钮在哪」都会滚丢。
         抽屉 teleport 到 body，三栏的布局规则写在下面不带 scoped 的样式块里
         （AGENTS #15），各工具组件只管自己内容块的样式。 -->

        <DupsTool v-if="step === 'dups'" @changed="emit('changed')" />

        <SourceFamilyManager
            v-else-if="step === 'rename'"
            :initial-query="renameQuery"
            @changed="emit('changed')"
        />

        <NameCleanTool
            v-else-if="step === 'names'"
            @changed="emit('changed')"
        />
    </el-drawer>
</template>

<style scoped>
.tidy-nav {
    flex: 0 0 auto;
    margin-bottom: 12px;
}
</style>

<!-- 抽屉 body 是 el-drawer 自己的内部结构，scoped 够不到它——连 `:deep()` 也不行：
     那个带 `.tidy-drawer` class 的元素身上没有 `data-v-*`（el-drawer 的根是 Teleport，
     父组件的 scope id 落不到里面真正带 class 的元素上），前缀永远匹配不上。
     所以单开一个**不带 scoped** 的块，用 class 前缀限定，别污染其它抽屉。
     （跨组件共用的（如 `.dot` 系列）才上全局 styles.css。）
     三栏骨架规则也在这里：三个工具组件都以 .tidy-head / .tidy-main / .tidy-foot
     声明自己在骨架里的位置，规则一处定义、三处生效。
     底部操作是 flex 的固定末项，不做 sticky——sticky 在 teleport 的 drawer body 里
     会与 body 自身的滚动叠加，表现为「贴底但一直遮内容」。 -->
<style>
.tidy-drawer .el-drawer__body {
    display: flex;
    flex-direction: column;
    overflow: hidden;
}
.tidy-drawer .tidy-head {
    flex: 0 0 auto;
}
.tidy-drawer .tidy-main {
    flex: 1 1 auto;
    min-height: 0;
    overflow-y: auto;
}
.tidy-drawer .tidy-foot {
    flex: 0 0 auto;
    display: flex;
    flex-direction: column;
    gap: 8px;
    margin-top: 10px;
}
</style>
