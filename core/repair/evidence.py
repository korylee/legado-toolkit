# -*- coding: utf-8 -*-
"""调试抽屉的 DOM 大纲：把页面压成缩进结构，供候选筛选与提示词使用。"""


from __future__ import annotations

import re
from typing import Any, List

MAX_OUTLINE_LINES = 90
#: 端给人看的行数（界面里的「大纲」视图）：比提示词的 90 行宽——模型只需要
#: 一眼看准取景区，人写规则时会往下追几层。超了由调用方**自报不全**。
DISPLAY_OUTLINE_LINES = 400
MAX_DEPTH = 6
SKIP_TAGS = ("script", "style", "noscript", "svg", "iframe", "head", "link", "meta")


def _short(s: Any, n: int = 48) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()[:n]


def _line_of(el, depth: int) -> str:
    """一个元素 → 一行：标签 + class/id + 关键属性 + 短文本。"""
    parts = [el.name]
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
    return "  " * depth + " ".join(parts)


def _shape_key(el) -> tuple:
    """两个元素「长得一样」的判据：标签 + class + **属性名集合**（不是属性值）。

    值不进 key：18 张图各有各的 `data-original`，把值算进去就永远折不动——
    而「它们都是 `img.comic_img`、都带 data-original」正是要表达的规律。
    id 也不进 key（见 :func:`_fold_groups`：序号型 id 会在分组时单独判掉）。
    """
    attrs = tuple(sorted(a for a in ("href", "src", "data-original", "data-src")
                         if el.get(a)))
    return (el.name, tuple(el.get("class") or []), attrs)


def _is_ordinal_ids(ids: List[str]) -> bool:
    """一组 id 是不是「同前缀 + 递增数字」（`img1`/`img2`…、`item_1`/`item_2`…）。

    这种 id 是模板产出的序号，不承载结构信息；把它们算进形状判据就一条也折不动
    （实测 2026-10-23 发发漫画正文 18 张图，`#img1`…`#img18` 全不同）。
    """
    if len(ids) < 2:
        return False
    prefixes, nums = set(), []
    for s in ids:
        m = re.fullmatch(r"(.*?)[-_]?(\d+)", s or "")
        if not m:
            return False
        prefixes.add(m.group(1))
        nums.append(int(m.group(2)))
    return len(prefixes) == 1


def _only_child(el):
    """唯一子元素（没有兄弟、也没有自己的文本）时返回它，否则 None。"""
    kids = el.find_all(recursive=False)
    if len(kids) != 1:
        return None
    if "".join(el.find_all(string=True, recursive=False)).strip():
        return None
    return kids[0]


def dom_outline(html: str, max_lines: int = MAX_OUTLINE_LINES,
                max_depth: int = MAX_DEPTH, select: str = "") -> str:
    """把 HTML 压成缩进大纲：标签 + class/id + 关键属性 + 短文本。

    **预算花在结构上，不花在样板与重复上**（两个与站点无关的变换）：

    - **单链折叠**：`div>div>table>tr>td>img` 这类只有唯一子元素的包装链压成一行
      （`a > b > c`）。它不携带结构信息，却吃掉一半深度预算——实测（2026-10-23
      发发漫画）正文图在深度 7，而 `MAX_DEPTH=6`，于是图片节点**完全不进大纲**，
      模型只能去猜一个不存在的 CMS 对象。
    - **同构兄弟折叠**：相邻且「标签 + class + 关键属性」相同的兄弟只画第一个，
      标 `×N`。18 张图占 1 行而不是 18 行，且**信息没少**（选择器要的是形状）。

    两者都不判断「哪个分支是正文」（那要猜，会剪错）；只是把重复与包装压掉，所以
    对 `<table>` 布局的老站、`div>div>div` 的框架页同样有效。
    """
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

    def chain(el) -> tuple:
        """沿单链一直向下，返回 (最深元素, 链上标签列表)。"""
        tags = [el.name]
        cur = el
        while True:
            nxt = _only_child(cur)
            if nxt is None or nxt.name is None:
                break
            cur = nxt
            tags.append(cur.name)
        return cur, tags

    def walk(el, depth: int) -> None:
        if len(lines) >= max_lines or depth > max_depth:
            return
        if getattr(el, "name", None) is None:
            return
        el, tags = chain(el)
        line = _line_of(el, depth)
        if len(tags) > 1:
            # 折叠掉的包装用 `>` 标出来：读的人仍知道真实层级
            line += "   （%s）" % " > ".join(tags[:-1])
        lines.append(line)
        kids = el.find_all(recursive=False)
        if not kids:
            return
        # 同构兄弟折叠：连续、同形状、且 id 是序号（或都没有 id）的一组只画第一个
        groups: List[tuple] = []
        for kid in kids:
            key = _shape_key(kid)
            if groups and groups[-1][0] == key:
                groups[-1][1].append(kid)
            else:
                groups.append((key, [kid]))
        for key, items in groups:
            ids = [str(k.get("id") or "") for k in items]
            foldable = len(items) > 1 and (not any(ids) or _is_ordinal_ids(ids))
            if foldable:
                # 折叠的是**兄弟**，保留第一份的子树（否则只看到父节点、看不到里面的
                # `img.comic_img`，等于把要表达的结构也一起折没了）
                lines.append("  " * (depth + 1) + "×%d 同构：" % len(items)
                             + _line_of(items[0], 0).strip())
                for kid in items[0].find_all(recursive=False):
                    walk(kid, depth + 2)
            else:
                for kid in items:
                    walk(kid, depth + 1)

    walk(root, 0)
    return "\n".join(lines)
