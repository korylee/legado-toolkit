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


def _tag_of(el) -> str:
    """折叠链上的一层：标签名 + id/class（不带属性值与文本——那一层只要「是谁」）。"""
    parts = [str(el.name)]
    if el.get("id"):
        parts.append("#" + _short(el.get("id"), 24))
    cls = el.get("class") or []
    if cls:
        parts.append("." + ".".join(_short(c, 24) for c in cls[:3]))
    return "".join(parts)


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
    """唯一子元素（没有兄弟、也没有自己的文本）时返回它，否则 None。

    **有名字的子元素不算「包装层」**：给它写了 id / class 的人认为它是结构节点
    （`ul#listbody.col_3_1` 就是列表容器），折进单链里等于把「这是个列表」这件事
    从大纲上抹掉。实测（2026-10-27 发发漫画，搜索只命中 1 本书）：真实列表
    `div.imgBox > ul#listbody > li > a.txtA` 被压成一行 `a.txtA`，模型看不到列表，
    转而去选了页面上另一个**真的有多条重复**的 `.nav`——那是导航栏。
    """
    kids = el.find_all(recursive=False)
    if len(kids) != 1:
        return None
    nxt = kids[0]
    if nxt.get("id") or nxt.get("class"):
        return None
    if "".join(el.find_all(string=True, recursive=False)).strip():
        return None
    return nxt


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
        """沿单链一直向下，返回 (最深元素, 链上**元素**列表)。"""
        els = [el]
        cur = el
        while True:
            nxt = _only_child(cur)
            if nxt is None or nxt.name is None:
                break
            cur = nxt
            els.append(cur)
        return cur, els

    def walk(el, depth: int) -> None:
        if len(lines) >= max_lines or depth > max_depth:
            return
        if getattr(el, "name", None) is None:
            return
        el, chain_els = chain(el)
        line = _line_of(el, depth)
        if len(chain_els) > 1:
            # 折叠掉的包装用 `>` 标出来：读的人仍知道真实层级。**class 要一起给**——
            # 只写标签名（`ul`）时列表容器那个 `col_3_1` 就看不见了，模型只能从
            # 「哪层看起来最像列表」猜，于是选到导航栏的 `ul.nav`（实测 2026-10-27）。
            line += "   （%s）" % " > ".join(_tag_of(e) for e in chain_els[:-1])
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

def _json_shape(value, depth: int = 0, max_depth: int = 5) -> str:
    """JSON 值 → 一行形状：类型 + 键 + 条数（**值本身只给个短样例**）。

    与 `dom_outline` 同一目的：模型要的是「哪条路径能取到书名」，不是完整数据。
    """
    import json as _json

    if depth > max_depth:
        return "…"
    if isinstance(value, dict):
        keys = list(value.keys())
        shown = keys[:8]
        parts = []
        for k in shown:
            v = value[k]
            if isinstance(v, (dict, list)):
                parts.append("%s: %s" % (k, _json_shape(v, depth + 1, max_depth)))
            else:
                sample = _short(str(v), 24)
                parts.append("%s=%r" % (k, sample) if sample else "%s=?" % k)
        more = "" if len(keys) <= len(shown) else " …还有 %d 键" % (len(keys) - len(shown))
        return "{" + "  ".join(parts) + more + "}"
    if isinstance(value, list):
        if not value:
            return "[]"
        first = value[0]
        head = _json_shape(first, depth + 1, max_depth)
        # 同构元素折叠：`[×30]` 和 dom_outline 的 `×N 同构` 同一件事
        return "[×%d] %s" % (len(value), head) if len(value) > 1 else "[1] %s" % head
    return "%s(%r)" % (type(value).__name__, _short(str(value), 24))


def json_outline(text: str, max_lines: int = MAX_OUTLINE_LINES) -> str:
    """把整页 JSON 压成缩进大纲：路径 + 类型 + 样例（`$.data.list[×30]` 这种形状）。

    为什么要它：接口站的响应体没有 DOM，`material_kind` 判成 `json_body` 之后
    `dom_outline` 只能看到 `<pre>` 里的一个文本节点——模型看不到任何字段名，
    只能猜一个不存在的变量。**给的正是 JSONPath 要照着写的那份形状。**
    """
    import json as _json

    try:
        data = _json.loads(str(text or ""))
    except (ValueError, TypeError):
        return ""
    lines: List[str] = []

    def walk(value, path: str, depth: int) -> None:
        if len(lines) >= max_lines or depth > 5:
            return
        indent = "  " * depth
        if isinstance(value, dict):
            lines.append(indent + path + "  " + _json_shape(value, 0, 0))
            for k, v in list(value.items())[:8]:
                if isinstance(v, (dict, list)):
                    walk(v, "%s.%s" % (path, k), depth + 1)
        elif isinstance(value, list) and value:
            first = value[0]
            lines.append(indent + "%s[×%d]" % (path, len(value)))
            if isinstance(first, (dict, list)):
                walk(first, path + "[*]", depth + 1)

    walk(data, "$", 0)
    return "\n".join(lines)
