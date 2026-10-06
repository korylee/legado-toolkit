<!-- 首屏的**五格决策卡**：现状 / 解决 / 取证 / AI 补足 + 折叠的证据。
     判据全在 `utils/debugDecision.js`（唯一一份），这里只渲染，**不许在本组件里判层或判缺口**。
     四个动作都由宿主映射到现有事件（goto / rerunFrom / applyRule），组件不认识业务。 -->
<script setup>
const props = defineProps({
  decision: { type: Object, required: true },
  step: { type: Object, default: () => ({}) },
  stepLabel: { type: String, default: "" },
  showRerun: { type: Boolean, default: false },
  canRerun: { type: Boolean, default: false },
  rerunning: { type: Boolean, default: false },
  rerunTooltip: { type: String, default: "" },
  aiLoading: { type: Boolean, default: false },
});

const emit = defineEmits(["fix", "probe", "ai", "rerun", "gotoBasic"]);

const VERDICT_TYPE = { pass: "success", fail: "danger", unknown: "info" };
</script>

<template>
  <section class="decision" :class="'is-' + decision.state.statusKey">
    <div class="dec-top">
      <b class="dec-title">{{ stepLabel }}</b>
      <el-tag size="small" :type="VERDICT_TYPE[decision.state.verdict] || 'info'">
        {{ decision.state.verdictLabel }}
      </el-tag>
      <el-tag v-if="decision.state.stale" size="small" type="warning">已过期</el-tag>
      <el-tag v-if="decision.layer.key" size="small"
              :type="decision.layer.key === 'L1' ? 'success' : 'warning'">
        {{ decision.layer.name }}
      </el-tag>
      <el-tag v-else-if="decision.layer.unsure" size="small" type="info">未定层</el-tag>
      <span class="mono muted dec-url" :title="step.url">{{ step.url }}</span>
    </div>

    <!-- 现状：一句话说清这一步怎么了，不解释机制 -->
    <p class="dec-state">{{ decision.gap ? decision.gap.reason : decision.state.reason }}</p>
    <p v-if="decision.gap && decision.gap.todo" class="muted dec-todo">
      {{ decision.gap.todo }}
    </p>

    <!-- 解决（改源）与取证（换通道）分栏：没有可改的东西时，取证才顶到主动作位 -->
    <div class="dec-actions">
      <el-button v-if="decision.fix" size="small" type="primary"
                 @click="emit('fix', decision.fix)">{{ decision.fix.label }}</el-button>
      <el-button v-if="decision.probe" size="small" plain
                 :type="decision.fix ? 'default' : 'primary'"
                 @click="emit('probe', decision.probe)">{{ decision.probe.label }}</el-button>
      <el-tooltip v-if="showRerun" placement="top" :content="rerunTooltip">
        <span>
          <el-button size="small" plain :loading="rerunning"
                     :disabled="rerunning || !canRerun"
                     @click="emit('rerun')">重新调试本步</el-button>
        </span>
      </el-tooltip>
    </div>

    <!-- AI 补足：只有材料合格（或没配模型这种设置问题）才出现；
         材料不对时整块不渲染——理由是现状行已经说过的那件事 -->
    <div v-if="decision.ai.eligible" class="dec-ai">
      <el-button size="small" type="primary" plain :loading="aiLoading"
                 @click="emit('ai')">{{ decision.ai.label }}</el-button>
      <span class="muted">拿这一步的材料提候选；验证仍走真引擎</span>
    </div>
    <p v-else-if="decision.ai.hint" class="muted dec-ai-hint">{{ decision.ai.hint }}</p>

    <div class="dec-core-value">
      <b>核心值</b>
      <template v-if="decision.core_values.length">
        <pre v-for="(value, i) in decision.core_values" :key="i" class="dec-core-value-text">{{ value }}</pre>
      </template>
      <span v-else class="muted">{{ decision.core_value_hint }}</span>
    </div>

    <!-- 证据来源在默认层（TODO ux-debug-reading 约束）：结论建立在哪些材料上，
         不展开就能看到；明细（可信边界/其余缺口/层证据）才进下面的折叠 -->
    <div v-if="decision.have.length" class="dec-evidence">
      <b>证据来源</b>
      <el-tag v-for="s in decision.have" :key="s.key" size="small" :type="s.type">
        {{ s.label }}<template v-if="s.trust === 'authoritative'">（可验收）</template><template
          v-else-if="s.trust === 'projection'">（辅助）</template>
      </el-tag>
    </div>

    <details class="dec-more">
      <summary>
        可信边界与其余缺口<template v-if="decision.deferred_gaps.length">（还有
        {{ decision.deferred_gaps.length }} 条）</template>
      </summary>
      <p v-if="decision.boundaries.length" class="muted dec-boundary">
        可信边界：{{ decision.boundaries.join("；") }}
      </p>
      <ul v-if="decision.deferred_gaps.length" class="dec-list">
        <li v-for="(g, i) in decision.deferred_gaps" :key="i">
          <el-tag size="small" :type="g.level === 'warn' ? 'warning' : 'info'">
            {{ g.level === "warn" ? "问题" : "提示" }}
          </el-tag>
          <span>{{ g.reason }}</span>
          <span class="muted"> · {{ g.todo }}</span>
        </li>
      </ul>
      <ul v-if="decision.layer.evidence.length" class="dec-list">
        <li v-for="(e, i) in decision.layer.evidence" :key="i">
          <span class="dec-why">{{ e.why }}</span>
          <span v-if="e.line" class="muted">（原文第 {{ e.line }} 行）</span>
          <span class="muted"> · {{ e.note }}</span>
          <code class="dec-snippet">{{ e.snippet }}</code>
        </li>
      </ul>
      <p v-if="decision.rule_error" class="dec-rule-error">
        规则无法离线回放：{{ decision.rule_error }}
      </p>
      <ul v-if="decision.notes.length" class="dec-list dec-notes">
        <li v-for="(n, i) in decision.notes" :key="i">
          {{ n }}
          <el-button v-if="n.indexOf('bookSourceType') >= 0" size="small" link
                     type="primary" @click="emit('gotoBasic')">去改类型</el-button>
        </li>
      </ul>
    </details>
  </section>
</template>

<style scoped>
/* 这一张卡是首屏的视觉中心：白底 + 状态色条，比周边面板高一档。
   色条按状态走（**过期优先于结论**：规则改过之后旧结论已不作数） */
.decision {
  margin: 0 0 var(--app-space-3);
  padding: var(--app-space-3);
  background: var(--app-surface);
  border: 1px solid var(--app-border-light);
  border-left: 3px solid var(--app-status-unknown);
  border-radius: var(--app-radius-md);
  box-shadow: 0 1px 2px rgb(0 0 0 / 4%);
}
.decision.is-pass { border-left-color: var(--app-status-pass); }
.decision.is-fail { border-left-color: var(--app-status-fail); }
.decision.is-stale { border-left-color: var(--app-status-warning); }
.dec-top { display: flex; align-items: center; flex-wrap: wrap; gap: var(--app-space-2); }
.dec-title { font-size: 15px; }
.dec-url { flex: 1 1 200px; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.dec-state { margin: var(--app-space-2) 0 0; line-height: 1.6; }
.dec-todo { margin: 2px 0 0; line-height: 1.6; }
.dec-actions { display: flex; flex-wrap: wrap; gap: var(--app-space-2); margin-top: var(--app-space-3); }
.dec-ai { display: flex; align-items: center; flex-wrap: wrap; gap: var(--app-space-2); margin-top: var(--app-space-2); }
.dec-ai-hint { margin: var(--app-space-2) 0 0; }
.dec-more { margin-top: var(--app-space-2); }
.dec-more summary { cursor: pointer; color: var(--app-primary); font-size: 12px; }
.dec-core-value { display: flex; align-items: flex-start; flex-wrap: wrap; gap: 6px; margin-top: 8px; font-size: 12px; }
.dec-core-value-text { max-width: 100%; margin: 0; white-space: pre-wrap; overflow-wrap: anywhere; font: inherit; color: var(--app-text); }
.dec-evidence { display: flex; align-items: center; flex-wrap: wrap; gap: 6px; margin-top: 6px; font-size: 12px; }
.dec-boundary { margin: 6px 0 0; line-height: 1.5; }
.dec-list { margin: 6px 0 0; padding-left: 18px; line-height: 1.7; font-size: 12px; }
.dec-snippet { display: block; margin-top: 2px; color: var(--app-text-muted); word-break: break-all; }
.dec-rule-error { margin: 6px 0 0; color: var(--app-status-fail); }
.dec-notes { color: var(--app-status-warning); }

@media (max-width: 720px) {
  .decision { padding: var(--app-space-2); }
  .dec-url { flex: 1 1 100%; }
  .dec-state { overflow-wrap: anywhere; }
  .dec-actions .el-button { flex: 1 1 auto; }
}
</style>
