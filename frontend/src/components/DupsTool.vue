<script setup>
// 重复筛选：一个组件包住全部状态——进抽屉时读分组，离开抽屉状态就没了。
// 重复合并判据由后端重算（core/dups），前端只提交分组与保留条：这一步是删源，
// 不信前端算出来的东西。
import { ref, computed } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { listDups, mergeSources, undoMerge } from "../api/sources";
import {
    healthCell,
    depthClass,
    depthText,
    triToInt,
} from "../utils/sourceRow";

const emit = defineEmits(["changed"]);

const dupsLoading = ref(false);
const dupGroups = ref([]);
const picks = ref({}); // 组 key → 保留条在 members 里的下标
const selected = ref(new Set()); // 勾选要合并的组 key
const mergeTags = ref(true);
const mergeComment = ref(false);
const merging = ref(false);
const mergeResult = ref(null); // { groups: [...合并过的组结果...], merged }

async function loadDups() {
    dupsLoading.value = true;
    mergeResult.value = null;
    try {
        const res = await listDups({ kinds: "mergeable", limit: 500 });
        dupGroups.value = res.groups || [];
        picks.value = {};
        selected.value = new Set();
        for (const g of dupGroups.value) {
            picks.value[g.key] = g.keep; // 后端给的「建议保留」
        }
    } catch (e) {
        ElMessage.error("读取重复源失败：" + e.message);
    } finally {
        dupsLoading.value = false;
    }
}
loadDups();

const selectedGroups = computed(() =>
    dupGroups.value.filter((g) => selected.value.has(g.key)),
);
const willDrop = computed(() =>
    selectedGroups.value.reduce((n, g) => n + (g.members.length - 1), 0),
);

function toggleGroup(key, on) {
    const next = new Set(selected.value);
    if (on) next.add(key);
    else next.delete(key);
    selected.value = next;
}

//: 成员来自 `core/dups._member`：`toc_complete` / `content_ok` 是 bool|null
//: （`Store.checks_map` 的三态），而 `depthText` / `depthClass` 判的是 0/1/null——
//: **不转就会静默说反**：`true === 1` 不成立，一条正文通过的源会显示成「正文 ✗」。
//: 未校验（没有结论行）时 `probe_depth` 为 0，两种判据都空 → 由调用方写「未校验」。
function depthRow(m) {
    return {
        probe_depth: m.probe_depth,
        toc_complete: triToInt(m.toc_complete),
        content_ok: triToInt(m.content_ok),
    };
}
//: 验到哪一步 / 验得怎么样：**结果优先、深度兜底**（`depthText` 的口径，与列表页同一份）
function verifyText(m) {
    return depthText(depthRow(m)) || "未校验";
}
function verifyClass(m) {
    return depthClass(depthRow(m));
}

function groupBody(g) {
    return {
        keep: g.members[picks.value[g.key]].url,
        drop: g.members
            .filter((_m, i) => i !== picks.value[g.key])
            .map((m) => m.url),
        merge_tags: mergeTags.value,
        merge_comment: mergeComment.value,
    };
}

async function runMerge() {
    const groups = selectedGroups.value;
    if (!groups.length) return ElMessage.warning("先勾选要合并的组");
    merging.value = true;
    try {
        // 先干跑，把「将发生的三件事」摆出来再问 —— 确认框里不该只有"确定吗"
        const plan = [];
        for (const g of groups) {
            plan.push(await mergeSources({ ...groupBody(g), dry_run: true }));
        }
        const tags = plan.reduce((n, p) => n + (p.tags_added || []).length, 0);
        const comments = plan.filter((p) => p.comment).length;
        await ElMessageBox.confirm(
            "将合并 " +
                groups.length +
                " 组：软删除 " +
                willDrop.value +
                " 条" +
                "（移入回收站，可恢复）；" +
                (mergeTags.value
                    ? "并过 " + tags + " 个标签；"
                    : "不并标签；") +
                (mergeComment.value
                    ? "改写 " + comments + " 条备注；"
                    : "不动备注。"),
            "合并重复源",
            { type: "warning", confirmButtonText: "合并" },
        );

        const done = [];
        for (const g of groups) {
            done.push(await mergeSources(groupBody(g)));
        }
        mergeResult.value = {
            groups: done,
            merged: done.reduce((n, r) => n + r.merged, 0),
        };
        ElMessage.success("已合并 " + mergeResult.value.merged + " 条");
        emit("changed");
        await loadDups();
    } catch (e) {
        if (e !== "cancel" && e !== "close")
            ElMessage.error("合并失败：" + e.message);
    } finally {
        merging.value = false;
    }
}

async function undoMergeAll() {
    const done = (mergeResult.value && mergeResult.value.groups) || [];
    let restored = 0;
    try {
        for (const r of done) {
            const res = await undoMerge({
                keep: r.keep,
                restore_urls: r.restore_urls,
                tags_added: r.tags_added,
                prev_comment: r.prev_comment,
            });
            restored += res.restored;
        }
        mergeResult.value = null;
        ElMessage.success("已还原 " + restored + " 条");
        emit("changed");
        await loadDups();
    } catch (e) {
        ElMessage.error("撤销失败：" + e.message);
    }
}
</script>

<template>
    <!-- 上中下三栏骨架：.tidy-head 固定 / .tidy-main 唯一滚动区 / .tidy-foot 固定。
         骨架与三栏布局类在父抽屉的 unscoped 样式块里定义（teleport 到 body，
         这里的 scoped 够不到 .el-drawer__body，但能管到自己的这些类）。 -->
    <div class="tidy-head">
        <div class="toolbar" style="margin-bottom: 10px">
            <el-button @click="loadDups" :loading="dupsLoading"
                >重新读取</el-button
            >
            <el-checkbox v-model="mergeTags" size="small"
                >并过被删条的标签</el-checkbox
            >
            <el-checkbox v-model="mergeComment" size="small"
                >备注一并保留</el-checkbox
            >
            <span class="grow" />
            <span class="muted">
                {{ dupGroups.length }} 组 · 建议合并 {{ willDrop }} 条
            </span>
        </div>
    </div>

    <div v-loading="dupsLoading" class="list tidy-main">
        <div
            v-for="g in dupGroups"
            :key="g.key"
            class="card"
            :class="{ on: selected.has(g.key) }"
        >
            <div class="card-head">
                <el-checkbox
                    :model-value="selected.has(g.key)"
                    @change="(v) => toggleGroup(g.key, v)"
                />
                <span class="site mono">{{ g.site }}</span>
                <span class="muted"
                    >{{ g.members.length }} 条，可精简
                    {{ g.redundant }} 条</span
                >
            </div>
            <!-- 成员两行：上行是「这是什么」（名称 + 状态标签），下行是「凭什么」——
               地址与校验证据。原来挤在一行里，名称与 host 重复了两遍（host 就在
               地址里），而后端 `_member` 已经算好的深度结论、搜索命中、署名与端口
               一个都没显示：判「留哪条」的依据全丢了。 -->
            <div
                v-for="(m, i) in g.members"
                :key="m.url"
                class="member"
                :class="{ keep: picks[g.key] === i }"
                @click="picks[g.key] = i"
            >
                <el-radio
                    :model-value="picks[g.key]"
                    :value="i"
                    @change="picks[g.key] = i"
                >
                    <span class="muted">保留</span>
                </el-radio>
                <div class="member-main">
                    <div class="member-line">
                        <span class="new">{{
                            m.name || "（无名）"
                        }}</span>
                        <el-tag
                            v-if="healthCell(m)"
                            size="small"
                            :type="healthCell(m).type"
                        >
                            {{ healthCell(m).label }}
                        </el-tag>
                        <span v-else class="muted">未校验</span>
                        <el-tag
                            v-if="m.has_fragment"
                            size="small"
                            type="warning"
                            title="地址尾部带分享署名——同一条源被转发几次就会这样"
                        >
                            带署名
                        </el-tag>
                        <el-tag
                            v-if="m.has_port"
                            size="small"
                            type="warning"
                            title="地址里写了端口（含端口写空的畸形地址）"
                            >带端口</el-tag
                        >
                    </div>
                    <div class="member-line">
                        <span class="mono member-url">{{ m.url }}</span>
                        <span class="muted" :class="verifyClass(m)">{{
                            verifyText(m)
                        }}</span>
                        <span v-if="m.search_hit" class="muted"
                            >命中《{{ m.search_hit }}》</span
                        >
                        <span v-if="m.checked_at" class="muted">{{
                            m.checked_at
                        }}</span>
                    </div>
                    <div v-if="m.comment" class="member-line muted">
                        备注：{{ m.comment }}
                    </div>
                </div>
            </div>
        </div>
        <el-empty
            v-if="!dupsLoading && !dupGroups.length"
            description="没有可合并的重复源"
            :image-size="70"
        />
    </div>

    <div class="tidy-foot">
        <div v-if="mergeResult" class="result">
            <span
                >已合并 <b>{{ mergeResult.merged }}</b> 条</span
            >
            <el-button link type="primary" @click="undoMergeAll"
                >撤销本次合并</el-button
            >
        </div>

        <div class="foot">
            <el-button
                type="primary"
                :loading="merging"
                :disabled="!selected.size"
                @click="runMerge"
            >
                合并选中（{{ selectedGroups.length }} 组 · 删
                {{ willDrop }} 条）
            </el-button>
        </div>
    </div>
</template>

<style scoped>
/* 列表只是那个「框」：滚动交给 .tidy-main，免得一层套一层两个滚动条 */
.list {
    border: 1px solid #ebeef5;
    border-radius: 4px;
}
/* 成员行高度随内容变，不加 content-visibility：占位估错反而让滚动条抖
   （名称清洗那份在 NameCleanTool 里）。 */
.card {
    border-bottom: 1px solid #f0f2f5;
    padding: 6px 0 10px;
}
.card.on {
    background: #f7fbff;
}
.card-head {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 4px 10px;
}
.card-head .site {
    font-weight: 600;
}
.member {
    display: flex;
    align-items: flex-start;
    gap: 8px;
    padding: 5px 10px 5px 34px;
    cursor: pointer;
}
.member:hover {
    background: #fafcff;
}
.member.keep {
    background: #f0f9eb;
}
.member-main {
    flex: 1 1 auto;
    min-width: 0;
    display: flex;
    flex-direction: column;
}
.member-line {
    display: flex;
    align-items: baseline;
    flex-wrap: wrap;
    gap: 8px;
    min-width: 0;
}
.member-url {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}
/* 深度结论的着色：绿=验过且通过、红=验了没过、不着色=还没结论。
   判据是共用那份（`utils/sourceRow.depthClass`），样式在列表页是 scoped 的，
   这里够不到，所以按同一套语义令牌（styles.css 的 `--app-status-*`）再声明一次。 */
.member-line .v-ok {
    color: var(--app-status-pass);
}
.member-line .v-bad {
    color: var(--app-status-fail);
}
.result {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 8px 12px;
    background: #f0f9eb;
    border-radius: 4px;
}
.foot {
    display: flex;
    flex-direction: row-reverse;
    gap: 8px;
    flex-wrap: wrap;
}
</style>
