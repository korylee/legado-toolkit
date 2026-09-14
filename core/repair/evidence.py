# -*- coding: utf-8 -*-
"""为 AI 修复构建「证据包」。"""


from __future__ import annotations

import asyncio
import re
from typing import Any, Dict, List, Optional, Tuple

MAX_OUTLINE_LINES = 90
MAX_DEPTH = 6
SKIP_TAGS = ("script", "style", "noscript", "svg", "iframe", "head", "link", "meta")


def _short(s: Any, n: int = 48) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()[:n]


def dom_outline(html: str, max_lines: int = MAX_OUTLINE_LINES,
                max_depth: int = MAX_DEPTH, select: str = "") -> str:
    """把 HTML 压成缩进大纲：标签 + class/id + 关键属性 + 短文本。"""
    from bs4 import BeautifulSoup

    try:
        soup = BeautifulSoup(html or "", "html.parser")
    except Exception:
        return ""
    for bad in soup(list(SKIP_TAGS)):
        bad.decompose()
    root = soup
    if select:
        try:
            found = soup.select(select)
            if found:
                root = found[0]
        except Exception:
            pass
    lines: List[str] = []

    def walk(el, depth: int) -> None:
        if len(lines) >= max_lines or depth > max_depth:
            return
        name = getattr(el, "name", None)
        if not name:
            return
        parts = [name]
        if el.get("id"):
            parts.append("#" + _short(el.get("id"), 24))
        cls = el.get("class") or []
        if cls:
            parts.append("." + ".".join(_short(c, 24) for c in cls[:3]))
        for attr in ("href", "src", "data-original", "data-src", "title", "alt"):
            v = el.get(attr)
            if v:
                parts.append("%s=%s" % (attr, _short(v, 36)))
        own = _short("".join(el.find_all(string=True, recursive=False)), 36)
        if own:
            parts.append(chr(34) + own + chr(34))
        lines.append("  " * depth + " ".join(parts))
        for child in el.find_all(recursive=False):
            walk(child, depth + 1)

    walk(root, 0)
    return "\n".join(lines)


def count_repeats(html: str, limit: int = 8) -> List[Tuple[str, int]]:
    """统计重复出现的「父>子」结构，用于定位列表容器。"""
    from bs4 import BeautifulSoup

    try:
        soup = BeautifulSoup(html or "", "html.parser")
    except Exception:
        return []
    counter: Dict[str, int] = {}
    for el in soup.find_all(True):
        parent = el.parent
        pname = getattr(parent, "name", None)
        if not pname or pname == "[document]":
            continue
        pcls = (parent.get("class") or [""])
        sel = "%s%s > %s" % (pname, ("." + pcls[0]) if pcls and pcls[0] else "", el.name)
        counter[sel] = counter.get(sel, 0) + 1
    return sorted(counter.items(), key=lambda kv: -kv[1])[:limit]



def _first_nodes(html: str, rule: str) -> List[Any]:
    from core.rules.replayer import parse_list

    if not rule:
        return []
    nodes, _err = parse_list(html, rule)
    return nodes or []


def _abs(base: str, href: str) -> str:
    from core.urls import abs_url as _abs_url

    return _abs_url(base, href or "")


async def build_evidence(source: Dict[str, Any], keyword: str,
                         timeout: float = 15.0, max_outline: int = MAX_OUTLINE_LINES) -> Dict[str, Any]:
    """抓取搜索页 / 详情页 / 章节页，压缩成证据包。"""
    import urllib.parse

    from services import add_source as A
    from core.rules.replayer import image_ratio, parse_field_first

    ev: Dict[str, Any] = {
        "ok": False,
        "name": str(source.get("bookSourceName", "") or ""),
        "url": str(source.get("bookSourceUrl", "") or ""),
        "keyword": keyword,
        "pages": {},
        "repeats": [],
        "failures": [],
        "notes": [],
        "heuristic": {},
    }
    base = ev["url"]

    async def fetch(u: str) -> str:
        try:
            return await asyncio.to_thread(A.fetch, u, timeout)
        except Exception as e:
            ev["failures"].append("抓取失败 %s: %s" % (u, type(e).__name__))
            return ""

    # ---- 1) 搜索页 ----
    search_tpl = str(source.get("searchUrl", "") or "")
    s_html = ""
    s_url = ""
    if search_tpl:
        if "{{key}}" in search_tpl:
            s_url = search_tpl.replace("{{key}}", urllib.parse.quote(keyword))
        else:
            s_url = search_tpl
        if s_url.startswith("/"):
            s_url = _abs(base, s_url)
        s_html = await fetch(s_url)
        if s_html:
            ev["pages"]["search"] = dom_outline(s_html, max_outline, select="")
            ev["repeats"] = count_repeats(s_html)
            try:
                ev["heuristic"]["search"] = A.analyze_search_page(s_html, keyword)
            except Exception as e:
                ev["notes"].append("启发式搜索分析失败: %s" % type(e).__name__)
        else:
            ev["failures"].append("搜索页无响应: %s" % s_url)
    else:
        ev["notes"].append("该源没有 searchUrl（仅发现模式）")

    # ---- 2) 用「当前规则」尝试取详情页链接；失败则退回启发式 ----
    rule_search = source.get("ruleSearch") or {}
    book_url = ""
    if s_html:
        nodes = _first_nodes(s_html, str(rule_search.get("bookList", "") or ""))
        if nodes:
            href = parse_field_first(nodes[0], str(rule_search.get("bookUrl", "") or ""))
            book_url = _abs(s_url or base, href)
            ev["notes"].append("bookList 解析出 %d 条，取第一条进详情页" % len(nodes))
        else:
            ev["failures"].append("bookList 解析为空 -> 搜索规则已失效")
            hs = ev["heuristic"].get("search") or {}
            for key in ("book_url", "first_book_url", "sample_book_url"):
                if hs.get(key):
                    book_url = _abs(s_url or base, hs[key])
                    break

    # ---- 3) 详情页 ----
    d_html = ""
    if book_url:
        d_html = await fetch(book_url)
        if d_html:
            ev["pages"]["detail_url"] = book_url
            ev["pages"]["detail"] = dom_outline(d_html, max_outline)
        else:
            ev["failures"].append("详情页无响应: %s" % book_url)
    else:
        ev["failures"].append("拿不到详情页 URL（bookUrl 规则失效且启发式无结果）")

    # ---- 4) 章节页（判断正文是文本还是图片）----
    toc = source.get("ruleToc") or {}
    rule_content = source.get("ruleContent") or {}
    if d_html:
        chapters = _first_nodes(d_html, str(toc.get("chapterList", "") or ""))
        if chapters:
            ch_url = _abs(book_url, parse_field_first(chapters[0], str(toc.get("chapterUrl", "") or "")))
            c_html = await fetch(ch_url) if ch_url else ""
            if c_html:
                ev["pages"]["chapter_url"] = ch_url
                ev["pages"]["chapter"] = dom_outline(c_html, max_outline)
                c_rule = str(rule_content.get("content", "") or "")
                vals, _err = _extract(c_html, c_rule)
                ratio = image_ratio(vals)
                ev["chapter_kind"] = "image" if ratio >= 0.5 else ("text" if vals else "unknown")
                ev["chapter_sample"] = vals[:3]
        else:
            ev["failures"].append("chapterList 解析为空 -> 目录规则已失效")

    ev["ok"] = bool(ev["pages"])
    return ev


def _extract(html: str, rule: str):
    from core.rules.replayer import extract_all_ex

    if not rule:
        return [], "无规则"
    return extract_all_ex(html, rule)

