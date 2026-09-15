// 系统/用户标签的前端解析工具，规则与 core/tags.py 保持一致。
export const SYSTEM_TAGS = new Set([
  // 类型标签与 Legado 的 bookSourceType 0/1/2/3 一一对应，不设任何未定义类型
  "📖小说", "🎧听书", "🎨漫画", "📥下载",
  "可用", "待验证", "已失效", "需代理复检",
  "规则完整",
]);

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

export function isSystemTag(tag) {
  return SYSTEM_TAGS.has(String(tag || '').trim());
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

