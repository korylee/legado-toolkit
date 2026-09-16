// 系统/用户标签的前端解析工具。
//
// 系统标签枚举（类型 / 健康状态 / 规则质量）由后端下发，唯一来源是
// core/tags.py 与 core/models.py。前端不再自行维护这些枚举——两边各存一份
// 必然漂移，历史上已经对不上过（书源类型一度把 3 当成视频、还编出了
// Legado 不存在的 4）。
//
// 枚举是响应式的：加载完自动触发重渲染，组件里直接当普通数组/函数用即可。
import { ref } from "vue";

import { getTagsMeta } from "../api/sources";

const sourceTypes = ref([]);    // [{ value: 0, tag: "📖小说" }, ...]
const statusTags = ref([]);
const qualityTags = ref([]);
const userTagAliases = ref({});

export { sourceTypes, statusTags, qualityTags, userTagAliases };

let metaPromise = null;

async function fetchTagMeta() {
  const meta = await getTagsMeta();
  sourceTypes.value = meta.source_types || [];
  statusTags.value = meta.status_tags || [];
  qualityTags.value = meta.quality_tags || [];
  userTagAliases.value = meta.user_tag_aliases || {};
}

/**
 * 用户标签别名归一，如「精品排版」→「精排」。别名表由后端下发。
 *
 * 保存时后端会做同样的归一（core/tags.py 的 canonical_tag）。前端不提前归的话，
 * 用户输入「精品排版」、界面显示「精品排版」，保存后变成「精排」——用户会以为
 * 标签被改了或丢了。
 */
export function canonicalTag(tag) {
  const t = String(tag || "").trim();
  if (!t) return "";
  return userTagAliases.value[t.toLowerCase()] || t;
}

/**
 * 确保系统标签枚举已就绪，并发调用共用同一个请求。
 *
 * 调用 splitSystemUser **之前必须 await 这个**：它把「系统/用户」的拆分结果
 * 存进状态（弹窗里的 systemTags / userTags），不是响应式派生。枚举没到位就拆，
 * 系统标签会被当成用户标签**存下来**，之后枚举到位也不会自我纠正。
 * 失败不缓存，下次调用会重试（后端重启中途等情况能自愈）。
 */
export function ensureTagMeta() {
  if (!metaPromise) {
    metaPromise = fetchTagMeta().catch((e) => {
      metaPromise = null;
      // 吞掉异常会让「系统标签被误判成用户标签」变成无痕迹的静默故障
      console.error("[tags] 系统标签枚举加载失败，标签归类将不准确:", e);
      throw e;
    });
  }
  return metaPromise;
}

export function tagOfType(value) {
  const hit = sourceTypes.value.find((t) => t.value === Number(value));
  return hit ? hit.tag : "";
}

/**
 * 类型的**键名**（"novel" / "audio" / "manga" / "file"），不是显示用的标签。
 *
 * 它是 `services/add_source.py` 的入参，前端提交「快速生成」任务时要给。
 * 本组件原来自己抄了一份 `TYPE_KEYS` 常量——那正是这个接口存在的理由：
 * 书源类型这个枚举，前端历史上抄错过一次（把 3 当成视频、还编出了 Legado
 * 不存在的 4）。所以一律从后端下发取。
 *
 * 枚举没就绪时返回空串。**调用方必须自己处理空值**——读空了当成 "novel"
 * 会把漫画/听书源生成成小说源，而且看不出来。
 */
export function typeKeyOf(value) {
  const hit = sourceTypes.value.find((t) => t.value === Number(value));
  return (hit && hit.key) || "";
}

const _has = (list, tag) => list.some((t) => t === String(tag || "").trim());

export function isTypeTag(tag) {
  return sourceTypes.value.some((t) => t.tag === String(tag || "").trim());
}

export function isStatusTag(tag) {
  return _has(statusTags.value, tag);
}

export function isQualityTag(tag) {
  return _has(qualityTags.value, tag);
}

export function isSystemTag(tag) {
  return isTypeTag(tag) || isStatusTag(tag) || isQualityTag(tag);
}

export function splitTags(value) {
  const raw = Array.isArray(value) ? value : [value];
  const out = [];
  const seen = new Set();
  for (const item of raw) {
    for (const part of String(item || "").split(/[,，;；|]+/)) {
      const tag = part.trim();
      if (!tag || seen.has(tag)) continue;
      seen.add(tag);
      out.push(tag);
    }
  }
  return out;
}

export function splitSystemUser(value) {
  const system = [];
  const user = [];
  for (const tag of splitTags(value)) {
    (isSystemTag(tag) ? system : user).push(tag);
  }
  return { system, user };
}

export function mergeGroup(systemTags, userTags) {
  const out = [];
  const seen = new Set();
  for (const tag of [...splitTags(systemTags), ...splitTags(userTags)]) {
    if (seen.has(tag)) continue;
    seen.add(tag);
    out.push(tag);
  }
  return out.join(",");
}
