import assert from "node:assert/strict";
import test from "node:test";
import { debugResult, resetDebugResultCache } from "./debugResultCache.js";

const DETAIL = (steps = [{ name: "search", values: ["◇书籍总数:12"] }]) => ({
  status: "done",
  result: { steps, pages: [{ id: "search", html: "<html></html>" }] },
});

test("同一任务号只取一次（并发与先后调用共用一份请求）", async () => {
  resetDebugResultCache();
  let calls = 0;
  const fetchDetail = async () => { calls += 1; return DETAIL(); };
  const [a, b] = await Promise.all([
    debugResult("job-1", fetchDetail),
    debugResult("job-1", fetchDetail),
  ]);
  const c = await debugResult("job-1", fetchDetail);
  assert.equal(calls, 1);
  assert.equal(a.steps[0].name, "search");
  assert.equal(b.pages[0].id, "search");
  assert.deepEqual(c.steps, a.steps);
});

test("失败不缓存：下一次调用会重新取（一次抖动不该永久打不开证据）", async () => {
  resetDebugResultCache();
  let calls = 0;
  const flaky = async () => {
    calls += 1;
    if (calls === 1) throw new Error("网络断了");
    return DETAIL();
  };
  await assert.rejects(() => debugResult("job-2", flaky), /网络断了/);
  const got = await debugResult("job-2", flaky);
  assert.equal(calls, 2);
  assert.equal(got.steps.length, 1);
});

test("缓存有上限（LRU）：超出后最久没用的那份要重新取", async () => {
  resetDebugResultCache();
  let calls = 0;
  const fetchDetail = async (id) => { calls += 1; return DETAIL([{ name: id }]); };
  for (let i = 0; i < 6; i += 1) await debugResult("job-" + i, fetchDetail);
  assert.equal(calls, 6, "6 个任务号各取一次");
  // 上限 5：最早的 job-0 被挤掉 → 再访问要重取
  await debugResult("job-0", fetchDetail);
  assert.equal(calls, 7);
  // 命中会把那份顶到队尾（真 LRU），所以 job-2 还在
  await debugResult("job-2", fetchDetail);
  assert.equal(calls, 7);
  // 此时被挤掉的已经是 job-1
  await debugResult("job-1", fetchDetail);
  assert.equal(calls, 8);
});

test("没有任务号不发请求；不同任务号各取各的", async () => {
  resetDebugResultCache();
  let calls = 0;
  const fetchDetail = async (id) => { calls += 1; return DETAIL([{ name: id }]); };
  const empty = await debugResult("", fetchDetail);
  assert.deepEqual(empty, { steps: [], pages: [], source: "", error: "" });
  assert.equal(calls, 0);
  const one = await debugResult("job-a", fetchDetail);
  const two = await debugResult("job-b", fetchDetail);
  assert.equal(one.steps[0].name, "job-a");
  assert.equal(two.steps[0].name, "job-b");
  assert.equal(calls, 2);
});