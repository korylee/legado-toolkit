# -*- coding: utf-8 -*-
"""L4：**这一页实际发过的请求**里，哪一条是它的数据接口，以及拿它怎么配规则。

**为什么是「观察」而不是「读 JS 猜」**（2026-09-21/22 两轮实测的结论）：

- 需要这条路的站，页面 HTML 我们常常**根本拿不到**（403 / Cloudflare / 2 字节的 `CN`），
  于是「扫页面 JS 找接口」连材料都没有；
- 接口地址往往在外部 bundle 里拼、带签名或 POST body，静态读出来也拼不对；
- 而**浏览器真发出去的那条请求**，地址 / 方法 / body / 响应体都是现成的——这正是源作者
  手写那些 `@js:` / `,{"method":"POST"...}` 时干的事（他们也是抓包写出来的）。

**产物有两种，取决于响应是什么**（实测两种都有）：

- 响应是 **HTML** 片段（歡享小說的接口就是）→ 交给现有那套 `analyze_search_page`，
  产物是「URL + method + body」+ CSS 规则；
- 响应是 **JSON** → 提 JSONPath（找同构对象数组 + 猜字段名），产物是 JSONPath 规则。

两个判据各留一行证据（`why`），因为「哪一条是接口」和「字段叫什么」都是推断，
用户要能反驳（AGENTS #4）。
"""
from __future__ import annotations

import json
import re
import urllib.parse
from typing import Any, Dict, List, Optional

#: 只考虑「像数据」的响应类型（其余是图片 / 字体 / 埋点，当不了规则材料）。
DATA_MIME_HINTS = ("json", "html", "text/plain", "xml")

#: 埋点 / 上报类：**直接不要**（它们也走 XHR、也有 JSON 响应体，而且往往带着搜索词）。
#: 判据是路径里的整词，**不用裸的 `log` / `stat`**——那会命中 `/api/login`、`/static/`
#: 这类正常地址（与 `models.py` 里「不要放裸 cloudflare」是同一类错）。
NOISE_HINTS = ("/report", "beacon", "statistic", "/analytics", "/collect", "/ping",
               "/track", "/monitor", "sentry", "/logging", "/logger")

#: JSON 里「这一格是不是书名 / 链接 / 封面 / 作者」的字段名线索（**只用来猜，猜错要写进附注**）。
FIELD_HINTS = {
    "name": ("name", "title", "bookname", "comicname", "articlename", "novelname", "caption"),
    "url": ("url", "href", "link", "detail", "path", "id"),
    "coverUrl": ("cover", "img", "image", "thumb", "pic", "poster"),
    "author": ("author", "writer", "authorname", "penname"),
}


def _looks_json(text: str) -> bool:
    head = (text or "").lstrip()[:1]
    return head in ("{", "[")


def _kw_variants(keyword: str) -> List[str]:
    """关键词在 URL / body 里的几种写法（编码前后都算）。"""
    kw = keyword or ""
    if not kw:
        return []
    return list(dict.fromkeys([kw, urllib.parse.quote(kw), urllib.parse.quote_plus(kw)]))


def _has_any(text: str, hints) -> str:
    low = str(text or "").lower()
    for h in hints:
        if re.search(h, low):
            return h
    return ""


def score_request(entry: Dict[str, Any], want: str = "", keyword: str = "") -> Dict[str, Any]:
    """给一条抓包打分并给出**理由**（`why` 是给用户看的，不是给我们看的）。

    评分只认能自证的东西：响应类型、响应体形状、关键词出现在请求还是响应里、
    目标是不是在响应里（链接 / 图片地址）。**埋点类明确扣分**——它们也走 XHR。
    """
    body = str(entry.get("body") or "")
    url = str(entry.get("url") or "")
    post = str(entry.get("post_data") or "")
    mime = str(entry.get("mime") or "").lower()
    why: List[str] = []
    score = 0

    if any(h in mime for h in DATA_MIME_HINTS):
        score += 1
    if _looks_json(body):
        score += 3
        why.append("响应是 JSON")
    if keyword and any(v in url or v in post for v in _kw_variants(keyword)):
        score += 2
        why.append("请求里带着搜索词")
    if keyword and keyword in body:
        score += 2
        why.append("响应里出现了搜索词")
    imgs = len(re.findall(r"https?://[^\"'\s]+\.(?:jpg|jpeg|png|webp|avif|gif)", body, re.I))
    if imgs >= 2:
        score += 2
        why.append("响应里有 %d 个图片地址" % imgs)
    links = len(re.findall(r"<a[\s>][^>]*href=", body, re.I))
    if want == "list" and links >= 2:
        score += 2
        why.append("响应里有 %d 个链接" % links)
    noise = _has_any(url, NOISE_HINTS) or _has_any(post, NOISE_HINTS)
    if noise:
        # **一票否决**：埋点里也有 JSON、也带搜索词，靠扣分压不住——而「把埋点当接口」
        # 的代价是下游拿它的响应去配规则（选得中、选中的不是数据）
        return {"entry": entry, "score": -1,
                "why": "像是埋点上报（命中 %s），不当接口用" % noise}
    return {"entry": entry, "score": score, "why": "；".join(why)}


def pick_data_requests(entries: List[Dict[str, Any]], want: str = "",
                       keyword: str = "", limit: int = 5) -> List[Dict[str, Any]]:
    """挑出「像数据的那几条」，按分排序。**只排序，不编造**：分 ≤ 0 的一条都不留。"""
    scored = [score_request(e, want, keyword) for e in entries or []]
    scored = [s for s in scored if s["score"] > 0]
    scored.sort(key=lambda s: -s["score"])
    return scored[:limit]


# ------------------------------------------------------------------ 响应 → 规则

def jsonpath_candidates(body: str, limit: int = 3) -> List[Dict[str, Any]]:
    """从 JSON 响应里提 JSONPath 候选：**找「同构对象数组」**，并猜字段名。

    算法就三步，别加戏：解析 → 深度优先找「元素是字典的数组」→ 每个数组：
    `$.<路径>[*]` 当 `bookList`，再按 `FIELD_HINTS` 在元素里找书名 / 链接 / 封面 / 作者。
    猜出来的字段名**一律写进附注**——它们是猜的，用户要对一遍（AGENTS #4）。
    """
    try:
        data = json.loads(body)
    except Exception:
        return []
    found: List[Dict[str, Any]] = []

    def walk(node, path: str):
        if len(found) >= limit:
            return
        if isinstance(node, list) and node and isinstance(node[0], dict):
            fields = {}
            for key, hints in FIELD_HINTS.items():
                # 取的是**键名**，而且按「包含」判（`bookName` 含 `name`、`bookUrl` 含 `url`）——
                # 两处都有坑：① 拿命中的提示词当字段名，会写出 `$.name` 这种看起来正常、
                # 其实取空的规则；② 用整词匹配（`^url$`）则 `bookUrl` 一个都认不出来
                fields[key] = next((k for k in node[0] if _has_any(str(k), hints)), "")
            got = sum(1 for v in fields.values() if v)
            if got >= 2:                     # 至少认得出两个字段才值得提
                found.append({"path": "$%s[*]" % path, "fields": fields,
                              "count": len(node), "sample_keys": list(node[0])[:8]})
                return
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, "%s.%s" % (path, k))
        elif isinstance(node, list):
            for v in node[:3]:
                walk(v, path)

    walk(data, "")
    return found


def rules_from_response(entry: Dict[str, Any], keyword: str = "") -> Dict[str, Any]:
    """把一条**接口响应**变成规则草稿。返回 `{kind, rules, search_url, note}`（可能空）。

    - 响应是 HTML → 走 `core.analyzer.analyze_search_page`（与搜索页同一条判据，不另写一份）
    - 响应是 JSON → `jsonpath_candidates`
    - 请求形态（method / body）**照抄观察到的**，只把关键词换成 Legado 的 `{{key}}`
    """
    body = str(entry.get("body") or "")
    out: Dict[str, Any] = {"kind": "", "rules": {}, "search_url": "", "note": ""}
    method = str(entry.get("method") or "GET").upper()
    post = str(entry.get("post_data") or "")
    # 关键词 → `{{key}}`（编码前后两种写法都换；换不到就原样，并在附注里说明）
    templated_post = post
    for v in _kw_variants(keyword):
        templated_post = templated_post.replace(v, "{{key}}")
    url = str(entry.get("url") or "")
    options = ""
    if method == "POST":
        options = ',%s' % json.dumps({"method": "POST", "body": templated_post},
                                     ensure_ascii=False, separators=(",", ":"))
    out["search_url"] = url + options

    if _looks_json(body):
        cands = jsonpath_candidates(body)
        if not cands:
            out["note"] = "接口响应是 JSON，但没找到「同构对象数组」（字段对不上），没敢提字段规则"
            return out
        best = cands[0]
        rules = {"bookList": best["path"]}
        for key, field in best["fields"].items():
            if field and key in ("name", "url", "coverUrl", "author"):
                rules[key if key != "url" else "bookUrl"] = "$.%s" % field
        out.update({"kind": "json", "rules": rules})
        out["note"] = ("接口响应是 JSON：bookList 提的是 %s（%d 条），字段名是**猜的**"
                       "（%s）——请对一遍" % (best["path"], best["count"],
                                           "、".join("%s→%s" % (k, v) for k, v in best["fields"].items() if v)))
        return out

    if "<" in body[:200]:
        from core.analyzer import analyze_search_page
        a = analyze_search_page(body, keyword)
        if a.get("bookList"):
            out.update({"kind": "html", "rules": {
                "bookList": a["bookList"], "name": a["name"], "bookUrl": a["bookUrl"],
                "author": a["author"], "coverUrl": a["coverUrl"]}})
            out["note"] = "接口响应是 HTML：规则是按那份响应配的（与搜索页共用同一套推断）"
            return out
        out["note"] = "接口响应是 HTML，但没推断出列表规则"
    return out


# ------------------------------------------------------------------ 对外的一步

def search_api_via_engine(url: str, keyword: str, timeout: int = 120) -> Optional[Dict[str, Any]]:
    """让引擎渲染这一页、**看它实际发了哪些请求**，挑出搜索接口并提规则（L4 的收口）。

    拿不到就返回 None（调用方按「这条路没走通」处理，别把原因吞掉——上面每处都有 note）。
    """
    from core.jvm_debug import page_from_engine
    from core.app_debug import network_entries
    try:
        _html, entries = page_from_engine(url, timeout=timeout, with_requests=True)
    except Exception:
        return None
    entries = network_entries(entries) if not isinstance(entries, list) else entries
    picks = pick_data_requests(entries, want="list", keyword=keyword)
    for pick in picks:
        got = rules_from_response(pick["entry"], keyword)
        if got.get("rules"):
            got["why"] = pick["why"]
            got["request"] = {k: v for k, v in pick["entry"].items() if k != "body"}
            return got
    return None
