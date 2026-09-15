# -*- coding: utf-8 -*-
"""
Legado 书源规则回放器（rule replayer）。

目的
----
让「书源规则验证」与 Legado App 的真实执行语义尽量一致，避免两类误判：

1. 规则其实能用，但验证器解析不了 -> 被误判为失效（误杀）
2. 验证器用简化语法跑通了，但 Legado 跑不通 -> 导入后不能用（误放）

支持（对标 Legado 语法）
------------------------
- 规则类型前缀：@css:（默认）、@json:、@html:
- 选择器简写：class.xxx -> .xxx 、 id.xxx -> #xxx 、 tag.a -> a
- @ 链式选择：class.item@tag.a@href
- 索引：.-1（最后一个）/ .0 / .1
- 取值动作：text / textNodes / ownText / html / all，或任意属性名
- 正则后处理：rule##正则##替换（支持 $1 反向引用；只写 ##正则 表示删除匹配）
- JSONPath 子集：$.data.list[*].name

明确不支持（返回 unsupported 原因，调用方应视为「无法验证」而非「校验失败」）
--------------------------------------------------------------------------
- @js: / <js>...</js> 规则  -- 需要 Legado 的 Rhino 引擎
- @xpath: 规则             -- 需要 Legado 引擎
- || 备选规则
- JSONPath 递归下降 ..
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, List, Optional, Sequence, Tuple

# ------------------------------------------------------------------ 常量

#: 规则类型前缀 -> 内部类型
RULE_PREFIXES = {
    "@css:": "css",
    "@json:": "json",
    "@html:": "html",
    "@js:": "js",
    "@xpath:": "xpath",
}

#: 取值动作（Legado：<元素>@text / @textNodes / @ownText / @html / @all）
VALUE_ACTIONS = {
    "text", "textnodes", "texthtml", "owntext", "html", "all", "outerhtml",
    "innerhtml",
}

#: 常见属性名（用于区分「属性取值」与「裸标签选择器」）
COMMON_ATTRS = {
    "href", "src", "title", "alt", "value", "content", "id", "class", "style",
    "rel", "target", "poster", "datetime", "lang", "type", "name", "action",
    "srcset", "sizes", "width", "height", "role", "label", "for", "data",
}

#: 常见 HTML 标签名（用于区分「裸标签选择器」与「属性名」）
HTML_TAGS = {
    "a", "abbr", "address", "area", "article", "aside", "audio", "b", "base",
    "bdi", "bdo", "blockquote", "body", "br", "button", "canvas", "caption",
    "cite", "code", "col", "colgroup", "dd", "del", "details", "dfn", "dialog",
    "div", "dl", "dt", "em", "embed", "fieldset", "figcaption", "figure",
    "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "head", "header",
    "hgroup", "hr", "html", "i", "iframe", "img", "input", "ins", "kbd",
    "label", "legend", "li", "link", "main", "map", "mark", "menu", "meta",
    "meter", "nav", "noscript", "object", "ol", "optgroup", "option", "output",
    "p", "param", "picture", "pre", "progress", "q", "rp", "rt", "ruby", "s",
    "samp", "script", "section", "select", "slot", "small", "source", "span",
    "strong", "style", "sub", "summary", "sup", "table", "tbody", "td",
    "template", "textarea", "tfoot", "th", "thead", "time", "title", "tr",
    "track", "u", "ul", "var", "video", "wbr",
}

#: 图片 URL 的特征（用于判断「这个规则产出的是图片」）
_IMG_EXT_RE = re.compile(r"\.(jpe?g|png|webp|avif|gif|bmp)(\?|#|$)", re.I)


# ------------------------------------------------------------------ 数据结构

@dataclass
class ParsedRule:
    """解析后的规则结构。"""

    raw: str = ""
    kind: str = "css"                       # css / json / html / js / xpath
    steps: List[Tuple[str, str]] = field(default_factory=list)
    regex: str = ""                         # ##之后的匹配式
    replacement: Optional[str] = None       # ##之后的替换式（None=未给出）
    unsupported: str = ""                   # 非空表示无法回放

    @property
    def supported(self) -> bool:
        return not self.unsupported and bool(self.steps)


# ------------------------------------------------------------------ 解析

def _is_selector_like(seg: str) -> bool:
    """判断一个 @ 分段更像是 CSS 选择器而不是属性取值。"""
    if not seg:
        return False
    low = seg.lower()
    if low.startswith(("class.", "id.", "tag.", "#", ".", "[", ":")):
        return True
    if any(ch in seg for ch in (" ", ">", ",", "+", "~")):
        return True
    if low in HTML_TAGS:
        return True
    return False


def _looks_like_attr(seg: str) -> bool:
    """判断一个 @ 分段是「属性/动作」而不是选择器。"""
    if _is_selector_like(seg):
        return False
    low = seg.lower()
    if low in VALUE_ACTIONS or low in COMMON_ATTRS:
        return True
    if low.startswith(("data-", "aria-")):
        return True
    # 其余带连字符的裸标识符通常是自定义属性（如 data-original / img-url）
    if "-" in seg and not seg.startswith("-") and not re.fullmatch(r"-?\d+", seg):
        return True
    return False


def _normalize_selector(sel: str) -> str:
    """把 Legado 的选择器简写转换成标准 CSS。"""
    s = (sel or "").strip()
    if not s:
        return s
    s = re.sub(r"\btag\.(?=[A-Za-z_])", "", s)
    s = re.sub(r"\bclass\.(?=[A-Za-z_\-])", ".", s)
    s = re.sub(r"\bid\.(?=[A-Za-z_\-])", "#", s)
    return s.strip()


def _split_trailing_index(seg: str) -> Tuple[str, Optional[str]]:
    """拆出选择器末尾的索引写法，如 ``tag.a.-1`` -> ("tag.a", "-1")、``.0`` -> ("", "0")。"""
    m = re.search(r"\.(-?\d+)$", seg or "")
    if not m:
        return seg, None
    return seg[: m.start()], m.group(1)


def _parse_css_steps(body: str) -> List[Tuple[str, str]]:
    """把 ``class.a@tag.b@href`` 解析成步骤列表。"""
    segs = [s.strip() for s in (body or "").split("@")]
    segs = [s for s in segs if s]
    if not segs:
        return []
    # 单段且看起来是属性/动作 -> 直接取值（如规则就是 "text"）
    if len(segs) == 1 and _looks_like_attr(segs[0]):
        return [("attr", segs[0])]

    steps: List[Tuple[str, str]] = []
    for i, seg in enumerate(segs):
        # 纯索引段：-1 / 0 / 1
        if re.fullmatch(r"-?\d+", seg):
            steps.append(("index", seg))
            continue
        if i == 0 or not _looks_like_attr(seg):
            sel, idx = _split_trailing_index(seg)
            sel = _normalize_selector(sel)
            if sel:
                steps.append(("select", sel))
            if idx is not None:
                steps.append(("index", idx))
        else:
            steps.append(("attr", seg))
    return steps


def _parse_json_steps(body: str) -> Optional[List[Tuple[str, str]]]:
    """解析 JSONPath 子集：$.a.b[*].c / $[0].name / $['k']。"""
    s = (body or "").strip()
    if not s.startswith("$"):
        return None
    if ".." in s:  # 递归下降不支持
        return None
    rest = s[1:]
    steps: List[Tuple[str, str]] = []
    i = 0
    n = len(rest)
    while i < n:
        ch = rest[i]
        if ch == ".":
            j = i + 1
            while j < n and rest[j] not in ".[":
                j += 1
            key = rest[i + 1: j].strip()
            if not key:
                return None
            steps.append(("key", key))
            i = j
        elif ch == "[":
            k = rest.find("]", i)
            if k < 0:
                return None
            inner = rest[i + 1: k].strip()
            if inner == "*":
                steps.append(("wild", ""))
            elif re.fullmatch(r"-?\d+", inner):
                steps.append(("index", inner))
            elif len(inner) >= 2 and inner[0] == inner[-1] and inner[0] in "'\"":
                steps.append(("key", inner[1:-1]))
            else:
                return None
            i = k + 1
        else:
            return None
    return steps


def parse_rule(rule: str) -> ParsedRule:
    """解析 Legado 规则文本为结构。不抛异常；不支持时填 ``unsupported``。"""
    raw = (rule or "").strip()
    pr = ParsedRule(raw=raw)
    if not raw:
        pr.unsupported = "空规则"
        return pr

    body = raw
    # 1) 剥离 ##正则##替换
    if "##" in raw:
        parts = raw.split("##", 2)
        body = parts[0]
        pr.regex = parts[1] if len(parts) > 1 else ""
        pr.replacement = parts[2] if len(parts) > 2 else ""
    body = body.strip()

    # 2) 规则类型前缀
    low = body.lower()
    for prefix, kind in RULE_PREFIXES.items():
        if low.startswith(prefix):
            pr.kind = kind
            body = body[len(prefix):].strip()
            break
    else:
        # 无前缀时按内容自动判定：$ 开头视为 JSONPath
        if body.startswith("$"):
            pr.kind = "json"

    # 3) 不支持的语法 -> 明确标注，避免被当成「解析为空 = 规则失效」
    bl = body.lower()

    # 3.0) JS 检测必须排在下面那批**之前**
    #
    #  `selector@js:code` 这种形态里，JS 体内部完全可能出现 $1、&&、@get: 这些
    #  token。若先命中下面的检测，报出的原因就是错的——实测真实语料里，错误原因
    #  语料实测：这个顺序修掉之后，那类错报归零。这里刻意不写具体条数——
    #  它随「怎么算一条规则」的口径而变，写死会变成一个无法复现、也无法被推翻的断言
    #  源规则会因此显示「$n 取列表第 n 项暂未支持」，而真实原因是 JS 无法离线回放。
    #  结论仍是 unknown（灰），但「为什么测不了」正是调试功能的核心价值，
    #  报错原因等于把用户引向错误的方向。
    #
    #  原检测只认 `<js`/`</js`/开头 `js:`，从来不认中间形态的 `@js:`，所以
    #  `@js:` 这个条件是本任务顺带补上的（属既有的检测缺口，不是本次引入）。
    #  注意必须是精确的 `@js:`：写成裸的 `js:` 会把任何含 `js:` 的选择器
    #  也判为不支持，反而制造「明明能跑却显示无法判定」的噪音。
    if (pr.kind == "js" or "<js" in bl or "</js>" in bl
            or bl.startswith("js:") or "@js:" in bl):
        pr.unsupported = "JS 规则（@js:/<js>）需要 Legado 的 Rhino 引擎，无法离线回放"
        return pr

    # 3.1) Legado 支持但本项目回放不了的语法（详见设计文档 7.2）
    #      必须显式报 unsupported，否则会被静默当成 CSS 选择器跑出空结果，
    #      让试跑把「工具测不了」误判成「源坏了」
    if raw.startswith("@@"):
        pr.unsupported = "@@ 强制 jsoup 规则暂未支持"
        return pr
    if bl.startswith("@webjs:"):
        pr.unsupported = "@webjs: 注入 WebView 执行 JS，需要 Legado 引擎，无法离线回放"
        return pr
    if "@get:{" in body or "@put:{" in body:
        pr.unsupported = "@get: / @put: 变量读写暂未支持"
        return pr
    if "&&" in body or "%%" in body:
        pr.unsupported = "多规则合并（&& / %%）暂未支持"
        return pr
    if re.search(r"\[\s*-?\d+(?:\s*:\s*-?\d+){1,2}\s*\]", body):
        pr.unsupported = "区间索引（[start:end:step]）暂未支持"
        return pr
    if re.search(r"\$\d{1,2}", body):
        pr.unsupported = "$n 取列表第 n 项暂未支持"
        return pr
    if raw.count("##") >= 3:
        pr.unsupported = "## 第四段（只替换第一个匹配）暂未实现"
        return pr

    if pr.kind == "xpath" or bl.startswith("@xpath"):
        pr.unsupported = "XPath 规则需要 Legado 引擎，无法离线回放"
        return pr
    if "||" in body:
        pr.unsupported = "备选规则（||）暂未支持"
        return pr

    # 4) @html: 直接返回原始响应体
    if pr.kind == "html":
        pr.steps = [("raw", "")]
        return pr

    # 5) 切分步骤
    if pr.kind == "json":
        steps = _parse_json_steps(body)
        if steps is None:
            pr.unsupported = "JSONPath 语法超出子集支持范围（如递归下降 ..）"
            return pr
        pr.steps = steps
    else:
        pr.steps = _parse_css_steps(body)
    if not pr.steps:
        pr.unsupported = "无法解析出可执行步骤"
    return pr


def rule_supported(rule: str) -> Tuple[bool, str]:
    """返回 (是否可回放, 不可回放原因)。"""
    pr = parse_rule(rule)
    return pr.supported, pr.unsupported


def rule_kind(rule: str) -> str:
    """返回规则类型：css / json / html / js / xpath。"""
    return parse_rule(rule).kind


# ------------------------------------------------------------------ HTML 树

_PARSER_CHOICE: dict = {"name": None}


def _best_parser() -> str:
    """优先用 lxml，缺失时回退到标准库 html.parser（只探测一次）。"""
    if _PARSER_CHOICE["name"] is None:
        from bs4 import BeautifulSoup

        for parser in ("lxml", "html.parser"):
            try:
                BeautifulSoup("<p>x</p>", parser)
                _PARSER_CHOICE["name"] = parser
                break
            except Exception:
                continue
        if _PARSER_CHOICE["name"] is None:
            _PARSER_CHOICE["name"] = "html.parser"
    return _PARSER_CHOICE["name"]


def _make_soup(html: str):
    from bs4 import BeautifulSoup

    return BeautifulSoup(html or "", _best_parser())


def _is_tag(obj: Any) -> bool:
    """判断是否为 BeautifulSoup 元素。"""
    return hasattr(obj, "select") and hasattr(obj, "get_text")


# ------------------------------------------------------------------ JSON 树

def _loads_json(text: str) -> Any:
    """宽松解析 JSON：失败的响应（带前后缀/JSONP）尝试截取首个 { 或 [。"""
    t = (text or "").strip()
    if not t:
        raise ValueError("空响应")
    try:
        return json.loads(t)
    except Exception:
        pass
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        i = t.find(open_ch)
        j = t.rfind(close_ch)
        if 0 <= i < j:
            try:
                return json.loads(t[i: j + 1])
            except Exception:
                continue
    raise ValueError("响应不是合法 JSON")


def _json_to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(value, ensure_ascii=False)


# ------------------------------------------------------------------ 执行

def _norm_text(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _extract_value(node: Any, attr: str, kind: str) -> str:
    """从节点取出属性/文本。"""
    if not _is_tag(node):
        return _json_to_text(node)
    a = (attr or "text").lower()
    if a in ("text", "all", "texthtml"):
        return _norm_text(node.get_text(" ", strip=True))
    if a == "textnodes":
        return _norm_text("".join(str(s) for s in node.strings))
    if a == "owntext":
        return _norm_text("".join(str(s) for s in node.find_all(string=True, recursive=False)))
    if a in ("html", "outerhtml"):
        return str(node)
    if a == "innerhtml":
        return "".join(str(c) for c in node.contents)
    val = node.get(attr)
    return val if isinstance(val, str) else ("" if val is None else str(val))


def _css_select(nodes: Sequence[Any], selector: str) -> List[Any]:
    """在当前节点集合内做后代选择，按文档顺序去重。"""
    out: List[Any] = []
    seen = set()
    for n in nodes:
        if not _is_tag(n):
            continue
        try:
            matches = n.select(selector)
        except Exception:
            continue
        for m in matches:
            key = id(m)
            if key not in seen:
                seen.add(key)
                out.append(m)
    return out


def _json_walk(nodes: Sequence[Any], st: str, val: str) -> List[Any]:
    out: List[Any] = []
    for n in nodes:
        if st == "key":
            if isinstance(n, dict) and val in n:
                out.append(n[val])
        elif st == "wild":
            if isinstance(n, list):
                out.extend(n)
            elif isinstance(n, dict):
                out.extend(list(n.values()))
        elif st == "index":
            if isinstance(n, list):
                i = int(val)
                if -len(n) <= i < len(n):
                    out.append(n[i])
    return out


def _walk_hits(
    start_nodes: Sequence[Any],
    steps: Sequence[Tuple[str, str]],
    kind: str,
):
    """执行步骤链，并额外返回「命中节点」。

    返回值 ``(nodes, values, error, hits)``。``hits`` 是**执行到 attr 取值动作
    之前**那一刻的节点列表——那正是「规则选中的 DOM 块」。若规则没有属性取值
    步骤，``hits`` 等于最终 ``nodes``。
    """
    nodes: List[Any] = list(start_nodes)
    hits: List[Any] = []
    for st, val in steps:
        if st == "raw":
            # @html: 规则：不做任何选择，整份响应体就是命中内容
            hits = list(nodes)
            break
        if st == "select":
            nodes = _css_select(nodes, val)
        elif st == "attr":
            # 取值动作发生前，当前 nodes 就是命中块
            return [], [_extract_value(n, val, kind) for n in nodes], "", list(nodes)
        elif st == "index":
            i = int(val)
            if -len(nodes) <= i < len(nodes):
                nodes = [nodes[i]]
            else:
                nodes = []
        elif st in ("key", "wild"):
            nodes = _json_walk(nodes, st, val)
        else:
            return [], [], "未知步骤：%s" % st, []
    if not hits:
        hits = list(nodes)
    return nodes, None, "", hits


def _walk(
    start_nodes: Sequence[Any],
    steps: Sequence[Tuple[str, str]],
    kind: str,
):
    """执行步骤链。返回 ``(nodes, values, error)``。见 ``_walk_hits``。"""
    nodes, values, err, _hits = _walk_hits(start_nodes, steps, kind)
    return nodes, values, err


def _convert_replacement(repl: str) -> str:
    """把 Legado 的 $1 反向引用转换成 Python 的 \\g<1>。"""
    return re.sub(r"\$(\d+)", lambda m: "\\g<%s>" % m.group(1), repl or "")


def _apply_regex(values: Sequence[Any], regex: str, replacement: Optional[str]) -> List[str]:
    """对取出的值做正则后处理（不匹配则原样保留，与 Legado 一致）。"""
    out = ["" if v is None else str(v) for v in values]
    if not regex:
        return out
    try:
        pat = re.compile(regex)
    except re.error:
        return out
    repl = _convert_replacement(replacement or "")
    res: List[str] = []
    for s in out:
        try:
            res.append(pat.sub(repl, s))
        except Exception:
            res.append(s)
    return res


def _root_of(content: str, kind: str) -> Any:
    if kind == "json":
        return _loads_json(content)
    if kind == "html":
        return content
    return _make_soup(content)


# ------------------------------------------------------------------ 对外 API

def extract_all(content: str, rule: str) -> List[str]:
    """按规则从整份内容中取值（用于 name / bookUrl / chapterName 等字段规则）。

    返回字符串列表；解析不了时返回空列表，具体原因用 ``describe(content, rule)`` 查。
    """
    values, _err = extract_all_ex(content, rule)
    return values


def extract_all_ex(content: str, rule: str) -> Tuple[List[str], str]:
    """同 ``extract_all``，但额外返回失败原因（空串表示成功）。"""
    pr = parse_rule(rule)
    if pr.unsupported:
        return [], pr.unsupported
    if not pr.steps:
        return [], "空规则"
    try:
        root = _root_of(content, pr.kind)
    except Exception as e:
        return [], "内容解析失败：%s" % type(e).__name__
    nodes, values, err = _walk([root], pr.steps, pr.kind)
    if err:
        return [], err
    if values is None:
        if pr.kind == "json":
            values = [_json_to_text(n) for n in nodes]
        else:
            values = [_extract_value(n, "text", pr.kind) for n in nodes]
    return _apply_regex(values, pr.regex, pr.replacement), ""


def extract_all_nodes(
    content: str,
    rule: str,
    limit: int,
    max_chars: int,
) -> Tuple[List[str], List[str], str]:
    """按规则取值，并返回**命中节点的 outerHTML** 与失败原因。

    ``hits`` 是命中块的 HTML 序列（最多 ``limit`` 个，每个截断到 ``max_chars``）。
    调用方用它展示「规则现在选到了哪块 DOM」。

    **``limit`` / ``max_chars`` 故意没有默认值**：这两个数字是「证据预算」政策，
    归 ``core.quality`` 所有（``MATCHED_NODES_LIMIT`` / ``MAX_MATCHED_HTML_CHARS``）。
    在回放引擎里再硬编码一份同值默认，就等于同一份口径写两处——改动 quality 的
    常量不会有任何行为变化，将来必然分叉。调用方显式传，口径只有一处。

    两条调用方需要注意的语义：
      - **JSON 规则下 ``hits`` 可能与 ``values`` 完全相同**（字符串叶子经
        ``_json_to_text`` 原样返回），UI 上会出现两份重复内容，需要自行去重
      - **``max_chars`` 截断可能落在标签中间**，返回的片段不保证是合法 HTML

    返回 ``(values, hits, error)``；``error`` 非空表示规则不可回放。
    """
    pr = parse_rule(rule)
    if pr.unsupported:
        return [], [], pr.unsupported
    if not pr.steps:
        return [], [], "空规则"
    try:
        root = _root_of(content, pr.kind)
    except Exception as e:
        return [], [], "内容解析失败：%s" % type(e).__name__
    nodes, values, err, hit_nodes = _walk_hits([root], pr.steps, pr.kind)
    if err:
        return [], [], err
    if values is None:
        if pr.kind == "json":
            values = [_json_to_text(n) for n in nodes]
        else:
            values = [_extract_value(n, "text", pr.kind) for n in nodes]
    values = _apply_regex(values, pr.regex, pr.replacement)

    # 命中节点 -> HTML 片段：DOM 节点取 outerHTML，JSON 节点退化为文本
    hits: List[str] = []
    for node in hit_nodes[:max(0, limit)]:
        html = str(node) if _is_tag(node) else _json_to_text(node)
        hits.append(html[:max_chars] if max_chars > 0 else html)
    return values, hits, ""


def extract_first(content: str, rule: str) -> str:
    """取第一条结果（取不到返回空串）。"""
    vals = extract_all(content, rule)
    return vals[0] if vals else ""


def parse_list(content: str, rule: str) -> Tuple[List[Any], str]:
    """按列表规则（bookList / chapterList）解析出节点列表。

    返回 ``(nodes, error)``。error 非空表示无法解析（语法不支持 / 内容不是该类型）。
    """
    pr = parse_rule(rule)
    if pr.unsupported:
        return [], pr.unsupported
    if not pr.steps:
        return [], "空规则"
    try:
        root = _root_of(content, pr.kind)
    except Exception as e:
        return [], "内容解析失败：%s" % type(e).__name__
    nodes, values, err = _walk([root], pr.steps, pr.kind)
    if err:
        return [], err
    if values is not None:
        return [], "列表规则不应带属性取值"
    flat: List[Any] = []
    for n in nodes:
        # $.data.list 这种没写 [*] 的情况，自动展开一层
        if isinstance(n, list):
            flat.extend(n)
        else:
            flat.append(n)
    return flat, ""


def parse_field(node: Any, rule: str) -> List[str]:
    """在单个节点（列表项）上执行字段规则，如 ``tag.h3@tag.a@text`` / ``$.name``。"""
    pr = parse_rule(rule)
    if pr.unsupported or not pr.steps:
        return []
    kind = "css" if _is_tag(node) else "json"
    if pr.kind == "html":
        return [str(node)]
    try:
        nodes, values, err = _walk([node], pr.steps, kind)
    except Exception:
        return []
    if err:
        return []
    if values is None:
        if kind == "json":
            values = [_json_to_text(n) for n in nodes]
        else:
            values = [_extract_value(n, "text", kind) for n in nodes]
    return _apply_regex(values, pr.regex, pr.replacement)


def parse_field_first(node: Any, rule: str) -> str:
    """在单个节点上取第一条结果。"""
    vals = parse_field(node, rule)
    return vals[0] if vals else ""


# ------------------------------------------------------------------ 兼容旧接口

def apply_css_rule(content: str, rule: str) -> List[str]:
    """``add_source.apply_css_rule`` 的兼容实现（语义更接近 Legado）。"""
    return extract_all(content, rule)


def looks_like_image_rule(rule: str) -> bool:
    """启发式判断「这条正文规则产出的是图片 URL」。"""
    r = (rule or "").lower()
    if not r:
        return False
    if "img" in r:
        return True
    if any(k in r for k in ("@src", "@data-original", "@data-src", "@data-lazy-src", "srcset")):
        return True
    return False


def image_ratio(values: Sequence[str]) -> float:
    """返回值列表中「像图片 URL」的比例，用于判定图片型正文。"""
    vals = [str(v or "").strip() for v in values if str(v or "").strip()]
    if not vals:
        return 0.0
    hits = 0
    for v in vals:
        if _IMG_EXT_RE.search(v):
            hits += 1
        elif v.lower().startswith(("http", "//", "/")) and "<img" in v.lower():
            hits += 1
        elif "<img" in v.lower():
            hits += 1
    return hits / len(vals)


__all__ = [
    "ParsedRule", "parse_rule", "rule_supported", "rule_kind",
    "extract_all", "extract_all_ex", "extract_all_nodes", "extract_first",
    "parse_list", "parse_field", "parse_field_first",
    "apply_css_rule", "looks_like_image_rule", "image_ratio",
]