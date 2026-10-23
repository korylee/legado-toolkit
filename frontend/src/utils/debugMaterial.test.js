import assert from "node:assert/strict";
import test from "node:test";
import {
  layerRow, materialOrigin, materialTabs, networkText, preferredMaterial,
  renderLine, runtimeText, verifyAction,
} from "./debugMaterial.js";

test("render facts are a separate thing from material origin", () => {
  // 后端判到页上的事实：这一页就是那次渲染的结果
  const rendered = renderLine({ url: "https://a.com/ch/1.html", rendered: true });
  assert.equal(rendered.kind, "rendered");
  assert.match(rendered.text, /渲染后的/);

  // 要渲染但没成：原因翻成人话，不是把码丢给用户
  const failed = renderLine({ rendered: false, render_reason: "no_url" });
  assert.equal(failed.kind, "failed");
  assert.match(failed.text, /既没有 url 也没有 html/);
  // 认不出的码原样显示（对端多了一个码，藏起来就没人补）
  assert.match(renderLine({ rendered: false, render_reason: "x-new" }).text, /x-new/);

  // 没判到（静态页 / 老侧车 / 设备通道）→ 不说：读不到 ≠ 没渲染（AGENTS #12）
  assert.equal(renderLine({ url: "https://a.com" }), null);
  assert.equal(renderLine(null), null);
});

test("material origin says who fetched it: the engine's copy, or ours", () => {
  const engine = materialOrigin({ origin: "engine", id: "content" });
  assert.equal(engine.kind, "engine");
  assert.match(engine.label, /0 请求/);

  const ours = materialOrigin({ origin: "fetch", fetched_at: "2026-10-21 09:12", cached: false });
  assert.equal(ours.kind, "fetch");
  assert.match(ours.label, /另抓的一份（2026-10-21 09:12）/);
  assert.match(ours.hint, /可能与引擎看到的不同/);

  const cached = materialOrigin({ origin: "fetch", cached: true });
  assert.match(cached.hint, /来自缓存/);
  // 没有页面 / 来源未知 → 不说成「未知来源」
  assert.equal(materialOrigin(null), null);
  assert.equal(materialOrigin({ origin: "" }), null);
});

test("the action follows material + layer, not the rule", () => {
  // 引擎那份：直接验（rule_try 永远不发请求）
  assert.equal(verifyAction({ layer: "L1", origin: "engine" }).kind, "verify");
  // 另抓的静态页 + L2：这一页要渲染后才有内容 → 先取渲染材料
  const render = verifyAction({ layer: "l2", origin: "fetch" });
  assert.equal(render.kind, "render");
  assert.equal(render.label, "取渲染材料");
  assert.match(render.hint, /不发请求/);
  // L3/L4/判不了：只给「验证」——那两层的材料还没做，给「取材料」是假线索
  assert.equal(verifyAction({ layer: "L3", origin: "fetch" }).kind, "verify");
  assert.equal(verifyAction({ layer: "", origin: "fetch" }).kind, "verify");
  assert.equal(verifyAction({ layer: "L2", origin: "engine" }).kind, "verify");
});

test("only materials that exist get a tab; the most specific one opens first", () => {
  // L3：DOM 里没有可取的东西，这一步的材料是运行时形状
  const l3 = materialTabs({
    name: "content",
    runtime: { params_type: "[object Object]", keys: ["chapter_images"] },
  });
  assert.deepEqual(l3.map((tab) => tab.value), ["outline", "matched", "page", "runtime"]);
  assert.equal(preferredMaterial(l3), "runtime");
  assert.match(
    runtimeText({
      params_type: "[object Object]", keys: ["chapter_images"],
      chapter_images: { type: "Array", count: 3, http: 3, blob: 0, sample_host: "img.x.com" },
    }),
    /chapter_images：类型 Array · 共 3 条 · http 3 · blob 0 · 域名 img\.x\.com/,
  );

  // L1 正文档：没有形状也没有接口，材料栏停在「取到了什么」
  const plain = materialTabs({ name: "content", content: { hasText: true } });
  assert.deepEqual(plain.map((tab) => tab.value), ["outline", "matched", "page", "content"]);
  assert.equal(preferredMaterial(plain), "content");
  // 连 App 通道的三份恒在（各自的空态负责说清为什么没有）
  assert.equal(preferredMaterial(materialTabs({ name: "toc" })), "outline");
});

test("interface entries belong to the run's last render, so only the content step shows them", () => {
  // 条目的字段就是后端 `core.app_debug.network_entries` 收敛出来的那份，原样显示
  const network = [{ method: "GET", url: "https://s/api/1", status: 200, mime: "application/json", body: "{}" }];
  assert.ok(materialTabs({ name: "content", network }).some((tab) => tab.value === "network"));
  assert.ok(!materialTabs({ name: "toc", network }).some((tab) => tab.value === "network"));
  assert.match(networkText(network), /#1 GET 200 https:\/\/s\/api\/1/);
  assert.match(networkText(network), /类型：application\/json/);
});

test("the layer row states facts only: evidence with its source, or why no layer could be judged", () => {
  assert.equal(layerRow(null), null);
  assert.equal(layerRow({}), null);

  const row = layerRow({
    page_layer: {
      layer: "l3",
      evidence: [
        { why: "CryptoJS", note: "页面自己解密", source: "bundle.js", line: 12 },
        { why: "长 base64 负载", note: "藏在一个字符串里" },
      ],
    },
  });
  assert.equal(row.code, "L3");
  assert.equal(row.label, "要解密或读运行时对象");
  assert.equal(row.more, 1);
  assert.match(row.lines[0], /CryptoJS · 页面自己解密 · bundle\.js 第 12 行/);

  // 判不出层也是事实：照原样说原因，不补一个 L0 也不当成 L1
  const unsure = layerRow({ page_layer: { layer: "", evidence: [], unsure: "这一页拿到的是拦截页" } });
  assert.equal(unsure.code, "");
  assert.equal(unsure.lines.length, 0);
  assert.match(unsure.unsure, /拦截页/);
});
