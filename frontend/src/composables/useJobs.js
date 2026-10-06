// 进行中任务的共享状态（单例，同 useDebugSession 的模式）。
//
// 「有几条任务在跑」有两个读者：列表页统计条上的任务徽标、任务抽屉里的
// 进行中计数——而任务都是页面自己提交、自己订阅 SSE 的。原来这个数只活在
// JobsDrawer 里、靠 emit("running-change") 抛回父组件：状态的真实来源是
// 提交动作与 SSE 事件，却要绕一圈子组件回来，刷新编排散在两边。收进这里：
// 谁拿到事件（提交成功 / SSE 整帧 / 列表拉回）谁 upsert，读者只认这一份。
import { ref, computed } from "vue";

//: 非终态白名单。终态与未知状态（SSE 兜底的 "unknown"：重连到上限没拿到
//: 终态，任务实际可能早跑完了）都**不算在跑**——与抽屉列表的状态标签同口径。
const ACTIVE = new Set(["pending", "running", "cancel_requested"]);

//: job_id -> 最近一帧任务状态（含 progress / phase / total / kind）。
//:
//: **存整帧，不是只存 status**：徽标数的是「有几条在跑」，而进度条、阶段、在跑条
//: 要的是同一批事件里的另外几个数（提交成功 / SSE 整帧 / 列表拉回）。只存 status
//: 的话，那几个数就得各自再维护一份「当前任务」的 ref——历史上正是这么散成三份的
//: （列表页四个 ref + 抽屉的 liveState + 这里）。
const active = ref(new Map());

const runningCount = computed(() => active.value.size);

//: 在跑任务（新的在前）。列表页的在跑条与任务中心都读这一份。
const activeJobs = computed(() => Array.from(active.value.values()).sort(
  (a, b) => String(b.created_at || "").localeCompare(String(a.created_at || ""))));

/** 幂等：同一任务重复 upsert 只覆盖它带来的字段。 */
function upsertJob(job) {
  if (!job || job.error || !job.id) return;   // 后端对不存在的任务推 {"error":...}
  if (ACTIVE.has(job.status)) {
    const prev = active.value.get(job.id) || {};
    active.value.set(job.id, { ...prev, ...job });
  } else {
    active.value.delete(job.id);
  }
}

/** 按 id 取在跑任务的那一帧；不在跑（或没见过）给 null。 */
function jobById(id) {
  return active.value.get(id) || null;
}

function removeJob(id) {
  active.value.delete(id);
}

export function useJobs() {
  return { active, activeJobs, runningCount, upsertJob, jobById, removeJob };
}
