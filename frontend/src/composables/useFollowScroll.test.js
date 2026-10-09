// 跟随滚动的判据（composables/useFollowScroll）——守两件事：
//   ① 贴底时新行要带到底部；② 用户往上翻之后**不许**再抢他的滚动位置。
// 用假元素（只要 scrollTop/scrollHeight/clientHeight 三个数）就够，不需要 DOM。
import assert from "node:assert/strict";
import test from "node:test";

import { nextTick, ref } from "vue";

import { FOLLOW_SLACK_PX, useFollowScroll } from "./useFollowScroll.js";

function fakeList({ clientHeight = 100, scrollTop = 200, scrollHeight = 300 } = {}) {
  return { clientHeight, scrollTop, scrollHeight };
}

/** 等"渲染完 + 滚动落位"两次：watch 的回调先跑，它里头的 nextTick 排在 flush 之后。 */
async function settle() {
  await nextTick();
  await nextTick();
}

test("贴底时新行带到底部", async () => {
  const el = ref(fakeList());
  const lines = ref([]);
  const { following, onScroll } = useFollowScroll(el, lines);
  onScroll();
  assert.equal(following.value, true, "离底 0px 算在跟随");
  el.value.scrollTop = 0;
  lines.value = [1, 2, 3];
  await settle();
  assert.equal(el.value.scrollTop, el.value.scrollHeight, "新行到达时跟到底部");
});

test("往上翻就不再抢滚动位置", async () => {
  const el = ref(fakeList({ scrollTop: 0 }));
  const lines = ref([]);
  const { following, onScroll } = useFollowScroll(el, lines);
  onScroll();
  assert.equal(following.value, false, "离底 200px 不算跟随");
  lines.value = [1, 2, 3];
  await settle();
  assert.equal(el.value.scrollTop, 0, "用户翻上去看历史时不许被拽回底部");
});

test("回到最新：打开跟随并跳到底", async () => {
  const el = ref(fakeList({ scrollTop: 0 }));
  const { following, followLatest, onScroll } = useFollowScroll(el, ref([]));
  onScroll();
  assert.equal(following.value, false);
  followLatest();
  await settle();
  assert.equal(following.value, true);
  assert.equal(el.value.scrollTop, el.value.scrollHeight);
});

test("阈值只有一个：贴底判据两边同源", () => {
  const el = ref(fakeList({ scrollTop: 300 - 100 - (FOLLOW_SLACK_PX - 1) }));
  const { following, onScroll } = useFollowScroll(el, ref([]));
  onScroll();
  assert.equal(following.value, true, "阈值内算跟随");
  el.value.scrollTop = 300 - 100 - FOLLOW_SLACK_PX;
  onScroll();
  assert.equal(following.value, false, "阈值外不算跟随");
});
