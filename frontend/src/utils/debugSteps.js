// 「这一步上次跑出什么地址」的**唯一来源**：会话里刚跑完的那份优先，退回工作区记着的
// 最近一次材料。
//
// 为什么必须合并（2026-10-29 实测的误导）：会话结果（`useDebugSession.result`）只在
// 这次打开页面期间活着，刷新就没了；而工作区材料是壳从 `GET /rules/last-debug` 查回来的
// （每步带 name / page_id / url / verdict）。重跑一步要有它的 URL，只读会话结果就会在
// **明明有材料**的情况下报「上一轮结果里没有这一步的链接」——用户被引去重跑一遍整链，
// 而材料一直在（AGENTS #4：原因说错了方向，比没有原因更坏）。
//
// 本件是纯函数：不 import vue、不调 API，node 测试直接驱动。

function stepsOf(source) {
  const list = source && Array.isArray(source.steps) ? source.steps : [];
  return list.filter((step) => step && String(step.name || "").trim());
}

/**
 * 合并两份步骤列表：``result``（会话，字段最全）在前，``material``（工作区材料）补缺。
 *
 * 同名步骤**只保留会话那份**：它带着这次跑出来的 values / verdict，用旧材料里的
 * 同名项去覆盖会让「刚跑完的结论」被上一次的地址顶掉。
 *
 * @param {{result?: object|null, material?: object|null}} sources
 * @returns {Array<object>}
 */
export function mergedSteps({ result = null, material = null } = {}) {
  const fresh = stepsOf(result);
  const seen = new Set(fresh.map((step) => String(step.name)));
  const out = [...fresh];
  for (const step of stepsOf(material)) {
    const name = String(step.name);
    if (seen.has(name)) continue;
    seen.add(name);
    out.push(step);
  }
  return out;
}

/**
 * 这一步上次跑到的页面地址（空串 = 没有可用材料，调用方要说清缺什么）。
 *
 * @param {string} name 步名（`search` / `bookUrl` / `toc` / `content`）
 */
export function stepUrlOf(name, { result = null, material = null } = {}) {
  const want = String(name || "").trim();
  if (!want) return "";
  const step = mergedSteps({ result, material })
    .find((item) => String(item.name) === want);
  return step ? String(step.url || "").trim() : "";
}