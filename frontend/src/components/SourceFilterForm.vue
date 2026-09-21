<script setup>
// 筛选表单：桌面工具条那一行与移动端底部抽屉**同一个组件**。
//
// 为什么要合：六项条件 + 排序原来在桌面与移动各写一遍。两份的差异只有显示项
// （占位名 / 宽度 / 要不要字段名），而**字段本身与取值来源是一件事**——
// 「加一个筛选项只改了一处」不报错，表现是两个入口能筛出来的东西不一样；
// 文案机检也抓不到这种兄弟不一致（AGENTS #18 写明了那个盲区）。
//
// 根是**片段**（不套 wrapper）：两处宿主各自的布局（`.bar-row` 的横排、
// `.sheet` 的竖排 gap）原来就作用在这些字段上，多包一层会把间距吃掉。
// 样式沿用原有的全局类（`.w-*` / `.fld` / `.btns`，见 styles.css），
// 所以这个组件自己没有 `<style>`——它只决定“有哪些字段、取值从哪来”。
import { computed } from "vue";
import { Search } from "@element-plus/icons-vue";
import { HEALTH_OPTIONS } from "../utils/health";
import { sourceTypes } from "../utils/tags";

const props = defineProps({
  // 列表页的查询对象（reactive）。**就地读写**：它就是列表查询本身，不是本组件的
  // 局部状态——两处宿主共用同一个对象，所以在桌面改完条件、切到移动端看到的是同一套
  query: { type: Object, required: true },
  groups: { type: Array, default: () => [] },
  tags: { type: Array, default: () => [] },
  // bar = 桌面工具条（固定宽度、紧凑）；sheet = 移动端底部抽屉（带字段名、满宽）
  variant: { type: String, default: "bar" },
});
const emit = defineEmits(["search", "reset"]);

const isBar = computed(() => props.variant === "bar");

//: 下拉里的「名字 (N 条)」：分组与用户标签两处共用这一种写法
const withCount = (name, count) => name + " (" + count + ")";

const userTagOptions = computed(() => props.tags
  .filter((t) => t.kind === "user")
  .map((t) => ({ value: t.tag, label: withCount(t.tag, t.count) })));

//: 排序键必须与列里显示的东西一致：列里显示的是**结果**（正文 ✓ / 目录 ✗），
//: 所以排序也按结果（验过且通过 → 验了没过 → 还没验到），而不是按深度。
//: 星级不再成列，于是它只是可选排序。
const ORDER_OPTIONS = [
  { value: "-verified", label: "验证结果 ↓" },
  { value: "verified", label: "验证结果 ↑" },
  { value: "-checked_at", label: "校验时间 ↓" },
  { value: "name", label: "名称 ↑" },
];

//: 字段清单**只有这一份**：两种形态共用字段、绑定与取值来源，只差那几个显示项。
//: `barPh` / `sheetPh` 是两种形态各自的占位名（桌面那行靠占位名认字段，
//: 抽屉里每项都有字段名，占位名只说「不填就是不筛」）。
const fields = computed(() => [
  {
    key: "q", label: "关键词", kind: "input", icon: Search,
    barCls: "w-search", barPh: "搜名称 / 域名", sheetPh: "名称 / 域名",
  },
  {
    key: "type", label: "类型", kind: "select",
    barCls: "w-type", barPh: "类型", sheetPh: "全部",
    options: sourceTypes.value.map((t) => ({ value: t.value, label: t.tag })),
  },
  {
    key: "health", label: "健康度", kind: "select",
    barCls: "w-health", barPh: "健康度", sheetPh: "全部",
    options: HEALTH_OPTIONS,
  },
  {
    key: "group", label: "分组", kind: "select", filterable: true,
    barCls: "w-group", barPh: "分组", sheetPh: "全部",
    options: props.groups.map((g) => ({ value: g.group, label: withCount(g.group, g.count) })),
  },
  {
    key: "tag", label: "用户标签", kind: "select", filterable: true,
    barCls: "w-group", barPh: "用户标签", sheetPh: "全部",
    options: userTagOptions.value,
  },
  { key: "order", label: "排序", kind: "select", barCls: "w-order", options: ORDER_OPTIONS },
]);
</script>

<template>
  <template v-for="f in fields" :key="f.key">
    <!-- 桌面：一行不换行、宽度固定，回车即查询 -->
    <el-input v-if="isBar && f.kind === 'input'" v-model="query[f.key]" :class="f.barCls"
              :placeholder="f.barPh" :prefix-icon="f.icon" clearable size="small"
              @keyup.enter="emit('search')" />
    <el-select v-else-if="isBar" v-model="query[f.key]" :class="f.barCls" :placeholder="f.barPh"
               clearable :filterable="!!f.filterable" size="small">
      <el-option v-for="o in f.options" :key="o.value" :value="o.value" :label="o.label" />
    </el-select>

    <!-- 移动端：每项带字段名、满宽（抽屉里是竖排的，没有字段名就不知道这格是什么） -->
    <div v-else class="fld">
      <label>{{ f.label }}</label>
      <el-input v-if="f.kind === 'input'" v-model="query[f.key]" :placeholder="f.sheetPh"
                clearable />
      <el-select v-else v-model="query[f.key]" :placeholder="f.sheetPh" clearable
                 :filterable="!!f.filterable" style="width: 100%">
        <el-option v-for="o in f.options" :key="o.value" :value="o.value" :label="o.label" />
      </el-select>
    </div>

    <!-- 按钮跟着最后一个字段走，好保持它们是同一层 flex 子项（顺序与原来一致：
         桌面「查询 重置」，抽屉「重置 查询」） -->
    <template v-if="isBar && f.key === 'order'">
      <el-button type="primary" size="small" @click="emit('search')">查询</el-button>
      <el-button size="small" @click="emit('reset')">重置</el-button>
    </template>
    <div v-if="!isBar && f.key === 'order'" class="btns">
      <el-button @click="emit('reset')">重置</el-button>
      <el-button type="primary" @click="emit('search')">查询</el-button>
    </div>
  </template>
</template>

<style scoped>
/* 筛选字段的样式跟着字段走：`.fld` 是这里渲染的，`.w-*` 也只有这里的 bar 形态用
   （通过 fields 里的 barCls 挂上去）。放 scoped 的意思是不必再靠 class 前缀自律。
   注：Vue 只给选择器的**最后一段**加 scope 属性，所以 `.sheet .fld > label` 里的
   `.sheet`（宿主写的）照常作为祖先匹配，不需要它同属本组件。 */
.sheet .fld > label { display: block; font-size: 13px; color: #606266; margin-bottom: 6px; }

/* 桌面工具条那一行的固定宽度：每个字段各占一档，靠占位名认字段 */
.w-search { width: 210px; }
.w-type   { width: 116px; }
.w-health { width: 124px; }
.w-group  { width: 190px; }
.w-order  { width: 126px; }
</style>
