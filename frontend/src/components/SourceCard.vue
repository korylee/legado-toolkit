<script setup>
// 手机卡片：桌面表格那一行的移动端等价物。
//
// 为什么单独成文件：手机上的每一处布局调整都落在这块，而它原来长在 1200+ 行的
// SourcesView 里——改一次要先把那个文件读进来定位。搬出来之后，改卡片只读这个文件。
//
// 它是**纯展示**：勾选、正在校验都由 props 进来，动作 emit 回去。卡片**不持有也不推断**
// 勾选状态——手机上的勾选是跨页的显式 URL 列表（见 SourcesView 的 selected），
// 所以它拿不到"这一行是不是选中"以外的任何信息。
import { Refresh, EditPen, Delete } from "@element-plus/icons-vue";
import { engineLabel } from "../utils/health";
import { depthClass, depthText, hasAnyTag, healthCell, tagCellsOf,
         typeLabel } from "../utils/sourceRow";

const props = defineProps({
  row: { type: Object, required: true },
  selected: { type: Boolean, default: false },
  //: 这一行是否正在校验。**要按行区分**：全量时所有行都在测，逐条时只有命中的那些
  checking: { type: Boolean, default: false },
});
//: 四个动作都把 row 带回去，由父组件收口；卡片自己不碰请求、不碰状态
const emit = defineEmits(["toggle", "check", "edit", "remove"]);

//: 整张卡可点 = 勾选（拇指友好），复选框是同一件事的显式入口
const toggle = () => emit("toggle", props.row);
</script>

<template>
  <div class="src-card" :class="{ sel: selected }" @click="toggle">
    <div class="chk" @click.stop>
      <el-checkbox :model-value="selected" @change="toggle" />
    </div>
    <div class="main">
      <div class="row1">
        <span class="nm">{{ row.name || "（无名）" }}</span>
        <!-- 深度结论留在这里、**带红绿着色**（与桌面「验证」列同口径）：卡片上
             扫一眼找坏源全靠这个颜色，删掉它等于把这项能力收走。
             所以另一份要去掉——原来 meta 行还并排写着「目录✓ 正文✗」，同一件事
             在一张卡上写两遍（实测 200 条里 150 条会同时出现），既挤掉书名又
             白占一行；明细差别（目录过了但正文没过）在桌面上由 tooltip 兜住 -->
        <span class="muted nowrap" v-if="row.probe_depth" :class="depthClass(row)">
          验到{{ depthText(row) }}
        </span>
      </div>
      <div class="host mono">{{ row.source_url }}</div>
      <div class="meta">
        <el-tag size="small">{{ typeLabel(row.source_type) }}</el-tag>
        <el-tag v-if="healthCell(row)" size="small" :type="healthCell(row).type">
          {{ healthCell(row).label }}
        </el-tag>
        <!-- 结论**来自谁**（与表格 tooltip 同一件事）挪到这一行：它几乎每张卡都是
             同一个值（样本 200 条全是 local），却和书名挤在同一条最紧的行上。
             不删——手机卡片没有 tooltip，删了「结论来自谁」在手机上就彻底看不到 -->
        <span class="muted nowrap" v-if="row.engine">{{ engineLabel(row.engine) }}</span>
      </div>
      <div class="grp">
        <el-tag v-for="t in tagCellsOf(row)" :key="t.key" size="small" :type="t.type">
          {{ t.label }}
        </el-tag>
        <span v-if="!hasAnyTag(row)" class="muted">（无标签）</span>
      </div>
    </div>
    <!-- 动作**竖排**：横排三个图标按钮要 ~120px（另有两个 10px 的 gap），而卡片上
         最该宽的是书名——横排时名字只剩 ~30px，书名中位 4 字被截成 2 字。
         竖列之后这部分固定开销只 ~40px；删除放最下，离拇指更远、误触更少 -->
    <div class="acts">
      <el-button link round :icon="Refresh" :loading="checking" aria-label="校验这一条"
                 @click.stop="emit('check', row)" />
      <!-- 编辑用笔，不用漏斗：漏斗是列表页那个「筛选」按钮的图标，
           同一个图标指两件事比换个图标糟得多 -->
      <el-button link :icon="EditPen" aria-label="编辑" @click.stop="emit('edit', row)" />
      <!-- 与表格操作栏同一组动作：卡片是移动端的等价物，少一个就会
           「手机上没有删除入口、只能先勾选再走批量条」 -->
      <el-button link type="danger" :icon="Delete" aria-label="移入回收站"
                 @click.stop="emit('remove', row)" />
    </div>
  </div>
</template>

<style scoped>
/* 卡片自己的样式全在这儿（`.card-list` 是列表容器，留在 SourcesView）。
   这些元素都是本组件渲染的，所以 scoped 够得到，不必上全局（AGENTS #15）。 */
.src-card {
  background: #fff;
  border: 1px solid #e4e7ed;
  border-radius: 8px;
  padding: 10px 12px;
  display: flex; gap: 10px; align-items: flex-start;
}
.src-card.sel { border-color: #409eff; background: #ecf5ff; }
.src-card .chk { flex: 0 0 auto; padding-top: 1px; }
.src-card .main { flex: 1 1 auto; min-width: 0; }
.src-card .row1 { display: flex; align-items: baseline; gap: 8px; }
.src-card .nm {
  font-weight: 600; font-size: 15px; flex: 1 1 auto; min-width: 0;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
/* 动作竖排：横排三个图标按钮要 ~120px（另有两个 10px 的 gap），而卡片上最该宽的是
   书名——横排时名字只剩 ~30px（书名中位 4 字被截成 2 字）。竖列后固定开销只 ~40px。
   间距 2px：三个 32px 高的链接按钮 + 2 个间距正好落在卡片内容高（~103px）之内 */
.src-card .acts { flex: 0 0 auto; display: flex; flex-direction: column; gap: 2px; }
.src-card .acts .el-button { margin-left: 0; margin-bottom: 4px; }
.src-card .host {
  font-size: 12px; color: #909399; margin-top: 2px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.src-card .meta { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 7px; align-items: center; }
.src-card .grp { font-size: 12px; color: #606266; word-break: break-all; margin-top: 6px; }
</style>
