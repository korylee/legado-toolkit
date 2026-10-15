<script setup>
// 名称清洗工具：一个组件包住全部状态——进抽屉时预演，离开抽屉状态就没了。
// 判据在后端 core/name_clean（配置驱动，config/name_clean.json）；前端只筛、只勾、只应用。
import { ref, computed } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import {
    applyNames,
    getNameRules,
    previewNames,
    previewNameRules,
    undoNames,
} from "../api/sources";

const emit = defineEmits(["changed"]);

const loading = ref(false);
const applying = ref(false);
const rows = ref([]);
const total = ref(0);
const reasonLabels = ref({});
const checked = ref(new Set());
const selectedRuleIds = ref([]);
const nameResult = ref(null);
const nameUndone = ref(false);
const nameRules = ref([]);
const runtimeRules = ref([]); // 下拉里现敲现建的清除规则，只活在本次抽屉会话
const defaultConfidence = ref(0.95);
const activeRules = computed(() => [...nameRules.value, ...runtimeRules.value]);

// 规则下拉：列出**全部启用的规则**（配置 + 会话创建），命中的带计数。
// 之前只列「命中过的」，进抽屉没预演完时下拉是空的，现有规则根本选不了。
const matchedRules = computed(() => {
    const counts = new Map();
    for (const row of rows.value) {
        for (const id of row.reasons || [])
            counts.set(id, (counts.get(id) || 0) + 1);
    }
    return activeRules.value
        .filter((rule) => rule.enabled !== false)
        .map((rule) => ({
            id: rule.id,
            label: rule.name || reasonLabels.value[rule.id] || rule.id,
            count: counts.get(rule.id) || 0,
        }));
});
const listRows = computed(() => {
    const matched = rows.value.filter((row) =>
        (row.reasons || []).some((id) => selectedRuleIds.value.includes(id)),
    );
    return matched.map((row) => ({ ...row, why: reasonText(row) }));
});
const checkedRows = computed(() =>
    rows.value.filter((r) => checked.value.has(r.url)),
);
const pageFullyChecked = computed(
    () =>
        listRows.value.length > 0 &&
        listRows.value.every((r) => checked.value.has(r.url)),
);

function confType(c) {
    const n = Number(c) || 0;
    if (n >= 0.98) return "success";
    if (n >= 0.95) return "primary";
    if (n >= 0.85) return "warning";
    return "info";
}

function reasonText(r) {
    return (r.reasons || []).map((x) => reasonLabels.value[x] || x).join("、");
}

// ---------------------------------------------------------------- 会话规则
// 会话规则只活在本次抽屉会话里：下拉里现敲现建，刷新后清空。不写回 config。
// **按字面字符串清除，不当正则解析**：输入里的正则元字符先转义，用户敲
// 「[精品]」清掉的就是这四个字符，而不是一个字符类。
function escapeLiteral(s) {
    return String(s || "").replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function removeRuntimeRule(id) {
    const removed = runtimeRules.value.find((rule) => rule.id === id);
    runtimeRules.value = runtimeRules.value.filter((rule) => rule.id !== id);
    selectedRuleIds.value = selectedRuleIds.value.filter(
        (ruleId) => ruleId !== id,
    );
    if (removed) {
        reasonLabels.value = { ...reasonLabels.value };
        delete reasonLabels.value[id];
    }
    checked.value = new Set(
        listRows.value.map((row) => row.url),
    );
}

// 下拉里敲一段文字回车 = 建一条「清除这段文字」的会话规则，并立即重算清单。
// refreshRows 会把新规则送进预演、带回含新规则的 reason_labels；失败回滚。
async function createRuleByLiteral(literal) {
    const text = String(literal || "").trim();
    if (!text) return;
    const draft = {
        id: "session_" + Date.now(),
        name: "清除「" + text + "」",
        pattern: escapeLiteral(text),
        replacement: "",
        flags: "",
        enabled: true,
        confidence: 0.85,
    };
    const prevRules = runtimeRules.value;
    runtimeRules.value = [...prevRules, draft];
    try {
        await refreshRows();
        reasonLabels.value = {
            ...reasonLabels.value,
            [draft.id]: draft.name,
        };
        selectedRuleIds.value = [
            ...new Set([...selectedRuleIds.value, draft.id]),
        ];
        checked.value = new Set(rows.value.map((row) => row.url));
        ElMessage.success("规则已加入当前会话，刷新后会清空");
    } catch (error) {
        runtimeRules.value = prevRules;
        ElMessage.error("创建规则失败：" + error.message);
    }
}

// ---------------------------------------------------------------- 行清单
// 全量预演（配置规则）+ 会话规则匹配行，合成一份清单。
// 会话规则行的 new_name 用会话规则的清洗结果，config 行保留原样，两份按 url 合并。
async function refreshRows() {
    const res = await previewNames();
    let configRows = res.items || [];
    let configTotal = res.total || 0;
    if (runtimeRules.value.length) {
        const custom = await previewNameRules({
            rules: runtimeRules.value,
            sample: "",
        });
        const customRows = new Map(
            (custom.matches || []).map((row) => [row.url, row]),
        );
        const byUrl = new Map(configRows.map((row) => [row.url, { ...row }]));
        for (const [url, row] of customRows) {
            const base = byUrl.get(url);
            if (base) {
                byUrl.set(url, {
                    ...base,
                    new_name: row.new_name,
                    confidence: Math.min(base.confidence, row.confidence),
                    reasons: [
                        ...new Set([
                            ...(base.reasons || []),
                            ...(row.reasons || []),
                        ]),
                    ],
                    changed: row.new_name !== base.old_name,
                });
                continue;
            }
            byUrl.set(url, {
                ...row,
                changed: row.new_name !== row.old_name,
            });
        }
        configRows = [...byUrl.values()].filter((row) => row.changed);
        configTotal = custom.match_count ?? configTotal;
        reasonLabels.value = {
            ...res.reason_labels,
            ...(custom.reason_labels || {}),
        };
    } else {
        reasonLabels.value = res.reason_labels || {};
    }
    rows.value = configRows;
    total.value = configTotal;
    return { configRows, res };
}

async function loadNames() {
    loading.value = true;
    nameResult.value = null;
    nameUndone.value = false;
    try {
        const ruleConfig = await getNameRules();
        nameRules.value = (ruleConfig.config && ruleConfig.config.rules) || [];
        defaultConfidence.value = Number(
            ruleConfig.config?.default_confidence ?? 0.95,
        );
        const { res, configRows } = await refreshRows();
        const matched = new Set(
            configRows.flatMap((row) => row.reasons || []),
        );
        // 重新预演是刷新数据，不是重置选择：仍命中的选中规则（含会话建的）
        // 原样保留，否则会话规则一刷新就被剔除，清单跟着「还原」。
        const kept = selectedRuleIds.value.filter((id) => matched.has(id));
        if (kept.length) {
            selectedRuleIds.value = kept;
        } else {
            selectedRuleIds.value = nameRules.value
                .filter(
                    (rule) =>
                        matched.has(rule.id) &&
                        Number(rule.confidence) >= defaultConfidence.value,
                )
                .map((rule) => rule.id);
            if (!selectedRuleIds.value.length)
                selectedRuleIds.value = [...matched];
        }
        checked.value = new Set(listRows.value.map((row) => row.url));
    } catch (e) {
        ElMessage.error("预演失败：" + e.message);
    } finally {
        loading.value = false;
    }
}

// 进抽屉即预演（配合抽屉 destroy-on-close：每次打开都是最新库）。
loadNames();

function toggle(row) {
    const next = new Set(checked.value);
    if (next.has(row.url)) next.delete(row.url);
    else next.add(row.url);
    checked.value = next;
}

function checkAll(inList) {
    const next = new Set(checked.value);
    listRows.value.forEach((r) =>
        inList ? next.add(r.url) : next.delete(r.url),
    );
    checked.value = next;
}

function togglePageSelection() {
    checkAll(!pageFullyChecked.value);
}

async function applyNameChanges() {
    const changes = checkedRows.value.map((r) => ({
        url: r.url,
        name: r.new_name,
    }));
    if (!changes.length) return ElMessage.warning("先勾选要改的条目");
    try {
        await ElMessageBox.confirm(
            "将改 " +
                changes.length +
                " 条源的名字。改的是展示名（地址不动）；" +
                "导出的 JSON 会同步；这些源的校验结果会失效，可之后从书源列表按需重新校验。",
            "应用改名",
            { type: "warning", confirmButtonText: "应用" },
        );
    } catch (e) {
        return;
    }
    applying.value = true;
    try {
        const res = await applyNames(changes);
        nameResult.value = {
            applied: res.applied,
            missing: res.missing,
            prev: res.prev || [],
        };
        ElMessage.success("已改 " + res.applied + " 条");
        emit("changed");
        await refreshRows();
        selectedRuleIds.value = selectedRuleIds.value.filter((id) =>
            rows.value.some((row) => (row.reasons || []).includes(id)),
        );
        if (!selectedRuleIds.value.length)
            selectedRuleIds.value = [
                ...new Set(rows.value.flatMap((row) => row.reasons || [])),
            ];
        checked.value = new Set();
    } catch (e) {
        ElMessage.error("应用失败：" + e.message);
    } finally {
        applying.value = false;
    }
}

async function undoNameChanges() {
    if (!nameResult.value || !nameResult.value.prev.length) return;
    try {
        const res = await undoNames(nameResult.value.prev);
        nameUndone.value = true;
        ElMessage.success("已还原 " + res.restored + " 条");
        emit("changed");
        await loadNames();
    } catch (e) {
        ElMessage.error("撤销失败：" + e.message);
    }
}
</script>

<template>
    <div class="name-tool">
        <div class="name-toolbar">
            <el-button
                class="name-preview-button"
                :loading="loading"
                @click="loadNames"
                >重新预演</el-button
            >
            <el-select
                v-model="selectedRuleIds"
                multiple
                filterable
                allow-create
                default-first-option
                :reserve-keyword="false"
                collapse-tags
                collapse-tags-tooltip
                class="name-rule-filter"
                placeholder="命中规则（敲文字回车即建清除规则）"
                :max-collapse-tags="1"
                @create="createRuleByLiteral"
            >
                <el-option
                    v-for="rule in matchedRules"
                    :key="rule.id"
                    :value="rule.id"
                    :label="rule.label + (rule.count ? ' (' + rule.count + ')' : '')"
                />
            </el-select>
            <span class="muted name-filter-count"
                >{{ listRows.length }} / {{ rows.length }}</span
            >
            <el-button
                class="name-select-button"
                size="small"
                @click="togglePageSelection"
            >
                {{ pageFullyChecked ? "取消全选" : "全选" }}
            </el-button>
            <span class="muted name-total">
                共 {{ total }} · 改 {{ listRows.length }} · 已选
                {{ checked.size }}
            </span>
        </div>

        <div v-loading="loading" class="list tidy-main">
            <div
                v-for="r in listRows"
                :key="r.url"
                v-memo="[r, checked.has(r.url)]"
                class="row"
                :class="{ on: checked.has(r.url) }"
                @click="toggle(r)"
            >
                <el-checkbox
                    :model-value="checked.has(r.url)"
                    @click.prevent.stop="toggle(r)"
                />
                <div class="names">
                    <div class="old">{{ r.old_name || "（无名）" }}</div>
                    <div class="new">{{ r.new_name }}</div>
                </div>
                <el-tag size="small" :type="confType(r.confidence)">
                    {{ Math.round((Number(r.confidence) || 0) * 100) }}%
                </el-tag>
                <el-tooltip
                    v-if="r.collides"
                    content="改完与库里另一条源同名——可到重复筛选里对比处理"
                    placement="top"
                >
                    <el-tag size="small" type="danger">同名</el-tag>
                </el-tooltip>
                <span class="muted why">{{ r.why }}</span>
            </div>
            <el-empty
                v-if="!loading && !listRows.length"
                description="没有需要清洗的名字"
                :image-size="70"
            />
        </div>

        <div class="tidy-foot">
            <div v-if="nameResult" class="result">
                <span
                    >已改 <b>{{ nameResult.applied }}</b> 条</span
                >
                <span v-if="nameResult.missing" class="muted">
                    （{{ nameResult.missing }} 条已不在库里，跳过）
                </span>
                <el-button
                    link
                    type="primary"
                    :disabled="nameUndone"
                    @click="undoNameChanges"
                >
                    {{ nameUndone ? "已撤销" : "撤销本次改名" }}
                </el-button>
            </div>

            <div class="foot">
                <el-button
                    type="primary"
                    :loading="applying"
                    :disabled="!checked.size"
                    @click="applyNameChanges"
                >
                    应用改名（{{ checked.size }} 条）
                </el-button>
            </div>
        </div>
    </div>
</template>

<style scoped>
.name-tool {
    display: flex;
    flex-direction: column;
    height: 100%;
    min-height: 0;
}
/* 列表只是那个「框」：滚动交给 .tidy-main，免得一层套一层两个滚动条 */
.list {
    border: 1px solid #ebeef5;
    border-radius: 4px;
}
/* 一次可能铺 1600+ 行（每行十几个节点 ≈ 两万节点），滚动时每帧都有新行要
   layout + paint。`content-visibility: auto` 让浏览器跳过**视口外**那些行的
   布局与绘制，只按 contain-intrinsic-size 的占位算滚动条长度；前缀 `auto`
   会记住实测行高，滚动条不会一直跳。行高是固定的（flex 单行 + padding 6 + 边框）。 */
.row {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 6px 10px;
    border-bottom: 1px solid #f5f7fa;
    cursor: pointer;
    content-visibility: auto;
    contain-intrinsic-size: auto 33px;
}
.row:last-child {
    border-bottom: 0;
}
.row:hover {
    background: #fafcff;
}
.row.on {
    background: #f0f9ff;
}
.names {
    flex: 1 1 auto;
    min-width: 0;
    display: flex;
    align-items: baseline;
    gap: 8px;
}
.old {
    color: #909399;
    text-decoration: line-through;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}
.new {
    color: #303133;
    font-weight: 500;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}
.why {
    flex: 0 0 auto;
    font-size: 12px;
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

<!-- 工具条在三栏骨架里，但 el-form 等内部结构不受 scoped 限制问题影响；
     这一段样式只管工具条本身的伸缩与窄屏折叠，与其他抽屉无关。 -->
<style>
.name-toolbar {
    display: flex;
    min-width: 0;
    align-items: center;
    gap: 8px;
    margin-bottom: 10px;
    white-space: nowrap;
}
.name-rule-filter {
    width: min(360px, 36vw);
    min-width: 90px;
    flex: 1 1 120px;
}
.name-preview-button,
.name-select-button {
    flex: 0 0 auto;
}
.name-filter-count {
    flex: 0 0 auto;
    font-size: 12px;
}
.name-total {
    min-width: 0;
    margin-left: auto;
    overflow: hidden;
    text-overflow: ellipsis;
    font-size: 12px;
}
@media (max-width: 600px) {
    .name-toolbar {
        gap: 5px;
    }
    .name-preview-button {
        padding-right: 8px;
        padding-left: 8px;
    }
    .name-rule-filter {
        width: auto;
        min-width: 72px;
        flex: 1 1 72px;
    }
    .name-select-button {
        padding-right: 7px;
        padding-left: 7px;
    }
    .name-total {
        display: none;
    }
}
</style>
