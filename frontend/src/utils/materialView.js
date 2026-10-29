// 材料文本的**显示层**：分块窗口、命中索引、按命中切片。**唯一一份**——调试工作台与
// 证据面板共用；各写一份的话，两块材料的长短与高亮位置会不一样，读的人以为其中一块被截了。
//
// 三条口径都是踩出来的：
//   ① 分块要**先截断再格式化**：整份格式化再截一块，等于每次重算都替看不见的部分付钱；
//   ② 命中索引在**原文**上数：搜索要覆盖整份材料，不只是已渲染的那一段；
//   ③ 高亮位置在**显示文本**上找：格式化会改字符偏移，原文偏移直接拿去切片必然错位。
//      顺序在格式化下不变，所以第 i 处仍对应原文第 i 处；两处数不等时如实说，不假装对齐。
//
// 本件是纯函数：不 import vue，node 测试直接驱动。

//: 一次搜索最多索引多少处。整页 HTML 里搜 div / class 必然远超此数，
//: 超限时调用方要显式告知，否则计数器会把「前 N 处」说成全部。
export const HIT_LIMIT = 200;

/** 渲染窗口：按**原文字符数**截一段。`hasMore` 为真时调用方给「加载更多」。 */
export function viewWindow(raw, limit) {
  const text = String(raw == null ? "" : raw);
  const size = Math.max(0, Number(limit) || 0);
  return { head: text.slice(0, size), hasMore: size < text.length };
}

/**
 * 在整份文本里找命中位置。
 *
 * `truncated` 只在**真的还有下一处**时为真：恰好占满上限时不算截断
 * （否则计数器会说「命中过多」，而其实是正好 200 处）。
 */
export function findHits(text, key, limit = HIT_LIMIT) {
  const needle = String(key || "").trim();
  const hay = String(text == null ? "" : text);
  if (!needle || !hay) return { offsets: [], truncated: false };
  const offsets = [];
  let from = 0;
  while (offsets.length < limit) {
    const i = hay.indexOf(needle, from);
    if (i < 0) break;
    offsets.push(i);
    from = i + Math.max(1, needle.length);
  }
  const last = offsets.length ? offsets[offsets.length - 1] : 0;
  const truncated = offsets.length >= limit
    && hay.indexOf(needle, last + Math.max(1, needle.length)) >= 0;
  return { offsets, truncated };
}

/**
 * 把显示文本按命中位置切成 `[普通, 高亮, 普通, …]`，供模板逐段渲染。
 *
 * 落在窗口外的位置、以及与前一处重叠的位置都跳过；`index` 仍是它在**命中序列**里的
 * 序号（跳转按钮按它定位，与窗口宽窄无关）。
 */
export function segmentsOf(text, offsets, key) {
  const head = String(text == null ? "" : text);
  const needle = String(key || "").trim();
  if (!needle || !Array.isArray(offsets) || !offsets.length) {
    return [{ text: head, hit: false, index: -1 }];
  }
  const segs = [];
  let cursor = 0;
  offsets.forEach((off, hitIndex) => {
    if (off < cursor || off + needle.length > head.length) return;
    if (off > cursor) segs.push({ text: head.slice(cursor, off), hit: false, index: -1 });
    segs.push({ text: head.slice(off, off + needle.length), hit: true, index: hitIndex });
    cursor = off + needle.length;
  });
  if (cursor < head.length) segs.push({ text: head.slice(cursor), hit: false, index: -1 });
  return segs;
}