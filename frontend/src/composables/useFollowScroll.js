// 「跟随最新」：事件列表贴底时自动跟着新行走，用户往上翻就停住。
//
// 批量校验的时间线与调试的时间线**是同一种控件**（同一个观测帧的两种渲染），原来各写
// 一份，连"离底多近算还在跟随"都取了不同的数（24px / 40px）——同一件事在两处各判一次，
// 就会漂成"这个面板跟着走、那个不跟"，而用户只会觉得它坏了。这里只留一份、一个阈值。
//
// 只做逻辑、不带样式：两个面板的外壳与配色各自 scoped（AGENTS #15），共用的是行为。
import { nextTick, ref, watch } from "vue";

//: 距底部多近算"还在跟随"。一个数：两个面板看到的是同一种控件，行为不该有差别。
export const FOLLOW_SLACK_PX = 32;

/**
 * @param {import("vue").Ref} listEl 滚动容器（模板 ref）
 * @param {import("vue").Ref} [onFollow] 有变化就该跟着走的东西（事件数组）
 */
export function useFollowScroll(listEl, onFollow = null) {
  const following = ref(true);

  function scrollToLatest() {
    if (!following.value) return;
    nextTick(() => {
      const el = listEl.value;
      if (el) el.scrollTop = el.scrollHeight;
    });
  }

  function onScroll() {
    const el = listEl.value;
    if (!el) return;
    following.value =
      el.scrollHeight - el.scrollTop - el.clientHeight < FOLLOW_SLACK_PX;
  }

  function followLatest() {
    following.value = true;
    scrollToLatest();
  }

  if (onFollow) watch(onFollow, scrollToLatest);
  return { following, onScroll, followLatest, scrollToLatest };
}
