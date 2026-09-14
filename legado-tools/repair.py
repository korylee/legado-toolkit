# -*- coding: utf-8 -*-
"""P1：AI 规则修复循环。"""

# 闭环：抓证据 -> 模型提议 -> 回放验证 -> 把「失败差异」喂回去重试 -> 最多 N 轮。
# 模型只负责「提议」，验收一律由 legado_rules 回放完成，不通过就不落地。

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

SYSTEM_PROMPT = """你是 Legado（阅读 App）书源的规则修复专家。只输出 JSON，不要解释。

Legado 规则语法（用 @ 分段：前面是选择器，最后一段是取值动作）：
- 选择器简写：class.xxx 等于 .xxx；id.xxx 等于 #xxx；tag.a 等于 a
- 链式：class.book-list@tag.li@tag.a@href  表示 .book-list 下 li 下 a 的 href
- 索引：.0 取第一个，.-1 取最后一个
- 取值动作：text / textNodes / ownText / html，或任意属性名（href、src、data-original）
- 正则后处理：规则##正则##替换（支持 $1 反向引用）
- JSON 接口：$.data.list[*].name
- 绝对不要使用：@js:、<js>...</js>、@xpath:（离线回放器不支持）

字段含义：
- ruleSearch.bookList：搜索结果列表容器，要选到「每一个结果条目」
- ruleSearch.bookUrl：条目内指向详情页的链接
- ruleToc.chapterList：详情页章节列表容器，要选到「每一个章节链接」
- ruleToc.chapterUrl：章节链接
- ruleContent.content：正文；小说用 @text 取文本，漫画用 @tag.img@src 或 @data-original 取图片地址

硬性要求：
1. 只输出规则字段，不要改 bookSourceUrl / bookSourceName。
2. 规则里用到的 class 名必须真实存在于我给你的 DOM 大纲里，不许臆造。
3. 漫画站用图片规则并把 bookSourceType 设为 2；小说站用文本规则设为 0。
4. 输出格式（严格 JSON）：
{"ruleSearch":{"bookList":"","bookUrl":"","name":"","coverUrl":"","author":"","intro":""},
 "ruleToc":{"chapterList":"","chapterName":"","chapterUrl":""},
 "ruleContent":{"content":""},"bookSourceType":0,"reason":"一句话说明改动"}
"""


def build_user_prompt(ev: Dict[str, Any], current: Dict[str, Any],
                      verify: Optional[Dict[str, Any]] = None,
                      history: Optional[List[Dict[str, Any]]] = None) -> str:
    pages = ev.get("pages") or {}
    cur = {
        "ruleSearch": current.get("ruleSearch") or {},
        "ruleToc": current.get("ruleToc") or {},
        "ruleContent": current.get("ruleContent") or {},
        "bookSourceType": current.get("bookSourceType", 0),
    }
    L: List[str] = []
    L.append("站点：%s  %s" % (ev.get("name", ""), ev.get("url", "")))
    L.append("搜索关键词：%s" % ev.get("keyword", ""))
    L.append("")
    L.append("【当前规则（已失效，需要你修正）】")
    L.append(json.dumps(cur, ensure_ascii=False, indent=1))
    fails = (ev.get("failures") or []) + (ev.get("notes") or [])
    if verify and verify.get("steps"):
        for s in verify["steps"]:
            fails.append("验证步骤 %s: %s -> %s" % (s.get("name"), "通过" if s.get("ok") else "失败", s.get("detail", "")))
    if fails:
        L.append("")
        L.append("【失败信息】")
        for f in fails[:12]:
            L.append("- " + str(f))
    if pages.get("search"):
        L.append("")
        L.append("【搜索页 DOM 大纲】")
        L.append(pages["search"])
    if ev.get("repeats"):
        L.append("")
        L.append("【重复结构统计（次数多的通常是列表容器）】")
        for sel, n in ev["repeats"][:6]:
            L.append("- %s  x%d" % (sel, n))
    if pages.get("detail"):
        L.append("")
        L.append("【详情页 DOM 大纲】")
        L.append(pages["detail"])
    if pages.get("chapter"):
        L.append("")
        L.append("【章节页 DOM 大纲】")
        L.append(pages["chapter"])
    if ev.get("chapter_kind"):
        L.append("")
        L.append("【正文形态】%s（sample=%s）" % (ev["chapter_kind"], str(ev.get("chapter_sample"))[:200]))
    if history:
        L.append("")
        L.append("【你之前几轮的尝试（都已验证失败，别重复）】")
        for h in history[-3:]:
            L.append("- 第%d轮: %s" % (h.get("round", 0), str(h.get("reason", ""))[:120]))
    return "\n".join(L)


RULE_KEYS = ("ruleSearch", "ruleToc", "ruleContent", "ruleExplore", "ruleBookInfo")


def merge_proposal(current: Dict[str, Any], proposal: Dict[str, Any]) -> Dict[str, Any]:
    """把模型提议合并进原书源：只覆盖规则字段，保留 name/url 等元数据。"""
    out = dict(current)
    for key in RULE_KEYS:
        if isinstance(proposal.get(key), dict) and proposal[key]:
            base = dict(current.get(key) or {})
            for k, v in proposal[key].items():
                if isinstance(v, str) and v.strip():
                    base[k] = v.strip()
            out[key] = base
    t = proposal.get("bookSourceType")
    if isinstance(t, int) and t in (0, 1, 2, 3):
        out["bookSourceType"] = t
    return out



def verify_source(source: Dict[str, Any], keyword: str, detail_url: str = "") -> Dict[str, Any]:
    """回放验证（复用 add_source.verify_chain，规则解析走 legado_rules）。"""
    from add_source import verify_chain

    try:
        return verify_chain(dict(source), keyword, detail_url or "")
    except Exception as e:
        return {"steps": [{"name": "verify", "ok": False,
                          "detail": "验证异常 %s: %s" % (type(e).__name__, e)}],
                "all_ok": False}


async def repair_one(session, client, source: Dict[str, Any], keyword: str,
                     max_rounds: int = 3, timeout: float = 15.0,
                     evidence: Optional[Dict[str, Any]] = None,
                     verifier=None) -> Dict[str, Any]:
    """对单个源跑「提议 -> 验证 -> 重试」循环。

    evidence / verifier 可注入，便于离线测试。
    """
    from evidence import build_evidence
    from llm import extract_json

    verify_fn = verifier or verify_source
    ev = evidence if evidence is not None else await build_evidence(source, keyword, timeout)
    detail_url = (ev.get("pages") or {}).get("detail_url", "")
    before = verify_fn(source, keyword, detail_url)

    out: Dict[str, Any] = {
        "name": ev.get("name") or source.get("bookSourceName", ""),
        "url": ev.get("url") or source.get("bookSourceUrl", ""),
        "status": "", "rounds": 0, "error": "",
        "before": before, "after": None, "source": None,
        "history": [], "evidence": ev,
    }
    if before.get("all_ok"):
        out["status"] = "already_ok"
        return out
    if not ev.get("ok"):
        out["status"] = "no_evidence"
        return out
    if not getattr(client, "enabled", False):
        out["status"] = "dry_run"
        return out

    history: List[Dict[str, Any]] = []
    current = source
    cur_verify = before
    status = "failed"
    for r in range(1, max_rounds + 1):
        prompt = build_user_prompt(ev, current, cur_verify, history)
        try:
            text = await client.chat(SYSTEM_PROMPT, prompt, session)
        except Exception as e:
            status = "llm_error"
            out["error"] = "%s: %s" % (type(e).__name__, e)
            break
        proposal = extract_json(text)
        if not isinstance(proposal, dict):
            history.append({"round": r, "reason": "模型输出不是合法 JSON",
                            "raw": str(text)[:200]})
            continue
        merged = merge_proposal(current, proposal)
        v = verify_fn(merged, keyword, detail_url)
        history.append({"round": r, "reason": str(proposal.get("reason", ""))[:200],
                        "verify": v, "rules": {k: merged.get(k) for k in RULE_KEYS}})
        if v.get("all_ok"):
            out["status"] = "fixed"
            out["rounds"] = r
            out["source"] = merged
            out["after"] = v
            out["history"] = history
            return out
        current = merged
        cur_verify = v

    out["status"] = status
    out["rounds"] = len(history)
    out["after"] = cur_verify
    out["history"] = history
    return out


# ------------------------------------------------------------------ 批量与报告

async def repair_many(sources: List[Dict[str, Any]], keyword: str = "我的",
                      max_rounds: int = 3, concurrency: int = 4,
                      timeout: float = 15.0, client=None, verifier=None,
                      on_done=None) -> List[Dict[str, Any]]:
    import aiohttp

    from llm import LLMClient

    client = client or LLMClient()
    sem = asyncio.Semaphore(concurrency)
    connector = aiohttp.TCPConnector(limit=concurrency, limit_per_host=5)

    async def one(session, s):
        async with sem:
            try:
                res = await repair_one(session, client, s, keyword, max_rounds,
                                       timeout, verifier=verifier)
            except Exception as e:
                res = {"name": s.get("bookSourceName", ""), "url": s.get("bookSourceUrl", ""),
                       "status": "error", "error": "%s: %s" % (type(e).__name__, e),
                       "rounds": 0, "before": None, "after": None, "source": None,
                       "history": [], "evidence": {}}
            if on_done:
                on_done(res)
            return res

    async with aiohttp.ClientSession(connector=connector) as session:
        return await asyncio.gather(*[one(session, s) for s in sources])


def build_report(results: List[Dict[str, Any]]) -> str:
    from collections import Counter

    labels = {"fixed": "已修复", "already_ok": "本来就可用", "failed": "修复失败",
              "dry_run": "缺 API Key（仅抓了证据）", "no_evidence": "抓不到证据",
              "llm_error": "LLM 调用失败", "error": "异常"}
    cnt = Counter(r.get("status", "?") for r in results)
    L = ["# AI 规则修复报告", "", "共 %d 个源。" % len(results), "",
         "| 结果 | 数量 |", "|---|---:|"]
    for k, n in cnt.most_common():
        L.append("| %s | %d |" % (labels.get(k, k), n))
    L += ["", "## 明细", ""]
    for r in results:
        if r.get("status") not in ("fixed", "failed"):
            continue
        L.append("### %s  `%s`" % (r.get("name") or "(无名)", r.get("url") or ""))
        L.append("")
        L.append("- 结果：**%s**（%d 轮）" % (labels.get(r.get("status"), r.get("status")), r.get("rounds", 0)))
        if r.get("status") == "failed":
            L.append("- 最后失败原因：%s" % _fail_brief(r.get("after")))
        for h in r.get("history") or []:
            L.append("- 第%d轮：%s" % (h.get("round", 0), h.get("reason", "")))
        if r.get("status") == "fixed" and r.get("source"):
            L.append("")
            L.append("```json")
            L.append(json.dumps({k: (r["source"].get(k) or {}) for k in
                                 ("ruleSearch", "ruleToc", "ruleContent")},
                                ensure_ascii=False, indent=1))
            L.append("```")
        L.append("")
    return "\n".join(L)


def _fail_brief(verify: Optional[Dict[str, Any]]) -> str:
    if not verify:
        return "(无)"
    bad = [s for s in (verify.get("steps") or []) if not s.get("ok")]
    if not bad:
        return "(无失败步骤)"
    return "; ".join("%s: %s" % (s.get("name"), s.get("detail", "")) for s in bad[:3])



# ------------------------------------------------------------------ CLI

def load_sources(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_sources(path: str, data: Any) -> None:
    d = os.path.dirname(os.path.abspath(path))
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def cmd_repair(args) -> int:
    from llm import LLMClient

    data = load_sources(args.input)
    if not isinstance(data, list):
        print("输入必须是书源数组（JSON array）")
        return 2
    todo = data[:args.limit] if getattr(args, "limit", 0) else data
    if not todo:
        print("没有待修复的源")
        return 0
    client = LLMClient()
    if not client.enabled:
        print("提示：未设置 LEGADO_LLM_API_KEY，本次只抓证据、不做提议（dry-run）")
    print("待修复 %d 个源 | 最多 %d 轮 | 并发 %d | 模型 %s"
          % (len(todo), args.rounds, args.concurrency, client.config.model))
    done = [0]

    def on_done(res):
        done[0] += 1
        print("  [%d/%d] %-24s -> %s" % (done[0], len(todo),
                                          str(res.get("name", ""))[:24], res.get("status")))

    results = asyncio.run(repair_many(todo, keyword=args.keyword, max_rounds=args.rounds,
                                      concurrency=args.concurrency, timeout=args.timeout,
                                      client=client, on_done=on_done))
    fixed = [r for r in results if r.get("status") == "fixed"]
    if args.write and fixed:
        patch = {r.get("url"): r["source"] for r in fixed}
        ordered = [patch.get(s.get("bookSourceUrl"), s) for s in data]
        out = args.output or args.input
        save_sources(out, ordered)
        print("已回写 %d 条到 %s" % (len(fixed), out))
    if args.report:
        d = os.path.dirname(os.path.abspath(args.report))
        if d:
            os.makedirs(d, exist_ok=True)
        with open(args.report, "w", encoding="utf-8") as f:
            f.write(build_report(results))
        print("报告已写入 %s" % args.report)
    from collections import Counter
    print("结果汇总:", dict(Counter(r.get("status") for r in results)))
    return 0

