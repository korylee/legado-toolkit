# -*- coding: utf-8 -*-
"""调试抽屉的 DOM 大纲：把页面压成缩进结构，供候选筛选与提示词使用。"""


from __future__ import annotations

import re
from typing import Any, List

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
