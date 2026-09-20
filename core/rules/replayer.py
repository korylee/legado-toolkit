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
- 规则类型前缀：@css:（默认）、@json:、@js:、@xpath:
  （**没有 `@html:`**——Legado 的 `@html` 是取值动作，不是前缀，见 RULE_PREFIXES 的注释）
- 选择器简写：class.xxx -> .xxx 、 id.xxx -> #xxx 、 tag.a -> a
- @ 链式选择：class.item@tag.a@href
- 索引：.-1（最后一个）/ .0 / .1
- 取值动作：text / textNodes / ownText / html / all，或任意属性名
- 正则后处理：rule##正则##替换（支持 $1 反向引用；只写 ##正则 表示删除匹配）
  第四段 rule##正则##替换### = **只替换第一个匹配**，且**不命中时给空串**
  （不是原样保留——这两条都对齐 Legado 的 `replaceRegex`，别当成笔误）
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
    # **没有 `@html:`**：Legado 的前缀集只有 @CSS: / @@ / @XPath: / @Json:
    # （AnalyzeRule.kt:603-618），`@html` 只是**取值动作**（没有冒号）。
    # 这里曾经把它当合法前缀、返回整份响应体——于是 App 里跑不出东西的规则，
    # 被我们判成「有正文」，是典型的误放。实测库里 0 条规则用它，删掉零影响。
    # 别把 `@html`（动作）和 `@html:`（前缀）搞混，区别就在那个冒号。
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
    kind: str = "css"                       # css / json / js / xpath（没有 html）
    steps: List[Tuple[str, str]] = field(default_factory=list)
    regex: str = ""                         # ##之后的匹配式
    replacement: Optional[str] = None       # ##之后的替换式（None=未给出）
    replace_first: bool = False             # 第四段存在（##正则##替换###）= 只替换第一个
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


def _is_attr_or_action_name(seg: str) -> bool:
    """这个 `@` 分段是不是「属性名 / 取值动作」（= Legado 取值规则**末段**的语义）。

    `title` / `style` / `label` **同时**是 HTML 标签名与常见属性名，而
    `_looks_like_attr` 里的 `_is_selector_like` 先命中 HTML_TAGS，于是
    `class.a@title` 被判成「在 a 里再选一个 title 元素」→ **恒取空**。

    为什么末段是属性名：Legado 对取值规则走 `getResultList` → 末段交给
    `getResultLast`，那里除 text/textNodes/ownText/html/all 五个动作外一律
    `element.attr(末段)`（符号见 AnalyzeByJSoup.kt 的 getResultLast）。
    实测语料里末段为 `title` 的取值规则约 170 条（`ruleToc.chapterName: "@title"`
    这种「取当前节点的 title 属性」写法就在其中），改前全被静默算成取不到。

    **列表类规则不走这条**：`getElements` 把每个 `@` 段都当选择器，
    而库里列表字段（bookList/chapterList）末段用这三个词的规则**实测 0 条**。
    """
    low = (seg or "").lower()
    return low in VALUE_ACTIONS or low in COMMON_ATTRS


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
    if len(segs) == 1 and (_looks_like_attr(segs[0])
                           or _is_attr_or_action_name(segs[0])):
        return [("attr", segs[0])]

    last = len(segs) - 1
    steps: List[Tuple[str, str]] = []
    for i, seg in enumerate(segs):
        # 纯索引段：-1 / 0 / 1
        if re.fullmatch(r"-?\d+", seg):
            steps.append(("index", seg))
            continue
        # **末段的取值动作优先于「裸标签名」**。`html` 既是取值动作又是 HTML 标签名，
        # 而 `_looks_like_attr` 里 `_is_selector_like` 先命中 HTML_TAGS → 它被判成标签，
        # 于是 `class.a@tag.p@html` 解析成「在 p 里再选一个 html 元素」→ **永远取空**。
        # 实测语料 1889 条规则用这个形态（`.rd-article-wr@html` 这类）。
        #
        # 依据是 Legado 的语义，**但要说准是哪一半**（2026-09-16 订正）：
        # Legado 对**取值类**规则走 `getStringList` → `getResultList` →
        # `getResultLast(elements, rules[last])`，而 `getResultLast` 的 `when`
        # 明确把 text/textNodes/ownText/html/all 当**动作**（其余当属性名）。
        # 所以取值规则里末段是动作。
        #
        # **列表类规则不走这条路**：`chapterList` 用的是 `getElements`
        # （`BookChapterList.kt:203`），它把**每个 @ 段都当选择器**——
        # 所以 `class.chapter-list@tag.a` 能跑出章节（实测确认过）。
        # 两份函数不同，别拿一套去套另一套。
        #
        # 本条改动只影响取值规则：实测末段用 `@html` 的规则分布是
        # `content.content` 2131 / `bookinfo.intro` 356 / …，**没有一条在
        # chapterList 或 bookList 上**，也就不会踩到 `getElements` 那条路。
        #
        # 单段规则（`html` / `text`）不受影响——那时它是选择器，不是动作。
        if i == last and last > 0 and _is_attr_or_action_name(seg):
            steps.append(("attr", seg))
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
    # 1) 剥离 ##正则##替换[##只替换第一个]
    #    切法对齐 Legado `AnalyzeRule.kt:760-770`：**不限次数**地 split("##")，
    #    取前三段，**第四段存在即 replaceFirst**（第四段本身是 `###` 的余料，丢弃）。
    #    以前这里用 `split("##", 2)`，第四段不识别，于是 `##a##$1###` 的替换式
    #    被切成 `$1###`——那时不影响结果，因为整条规则在下面被报成 unsupported；
    #    现在实现了这个形式，切法就必须先对。
    if "##" in raw:
        parts = raw.split("##")
        body = parts[0]
        pr.regex = parts[1] if len(parts) > 1 else ""
        pr.replacement = parts[2] if len(parts) > 2 else ""
        pr.replace_first = len(parts) > 3
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
    bl = body.lower()      # body 是剥掉 ##正则##替换 之后的主体
    rl = raw.lower()       # raw 是整条规则（含 ## 段）

    # 3.0) JS 检测必须排在下面那批**之前**，且必须扫**整条规则**（rl）而不是主体（bl）
    #
    #  两个原因：
    #   1. `selector@js:code` 这种形态里，JS 体内部完全可能出现 $1、&&、@get: 这些
    #      token。若先命中下面的检测，报出的原因就是错的——把用户引向错误的方向，
    #      而这个工具的全部价值就是告诉他为什么。
    #   2. JS 也可能出现在 `##` 之后（如 `##总字数：X##$1###@js:result+'字'`）。
    #      只看 body 会漏掉这一批，让它们改报「## 第四段」——同样是错的原因。
    #
    #  语料实测：顺序修正 + 扫整条规则之后，这类错报归零。**这里刻意不写具体条数**
    #  ——它随「怎么算一条规则」的统计口径而变（同一份语料换口径能差好几倍），
    #  写死会变成一个既无法复现、也无法被推翻的断言。
    #
    #  原检测只认 `<js`/`</js`/开头 `js:`，从来不认中间形态的 `@js:`，所以
    #  `@js:` 这个条件是顺带补上的（属既有的检测缺口，不是本次引入）。
    #  注意必须是精确的 `@js:`：写成裸的 `js:` 会把任何含 `js:` 的选择器
    #  也判为不支持，反而制造「明明能跑却显示无法判定」的噪音。
    if (pr.kind == "js" or "<js" in rl or "</js>" in rl
            or rl.startswith("js:") or "@js:" in rl):
        pr.unsupported = "JS 规则（@js:/<js>）需要 Legado 的 Rhino 引擎，无法离线回放"
        return pr

    # 3.05) {{ }} 模板 / 变量求值。Legado 里它按 JS 求值（绑定 java / cookie / book
    #       等对象），离线做不到。
    #
    #       **这条是真正在防误杀**：以前它不被识别，会被当成 CSS 选择器跑出空结果，
    #       于是判「源坏了」（红）；现在归 unknown（灰）。
    #
    #       它还必须排在 `## 第四段` 之前：`raw.count("##")` 是**跨整串**计数的，一条
    #       含多个 `{{...##...}}` 的正文模板会把各自的 `##` 累加成 ≥3，被误报成四段式
    #       ——而那条规则里根本没有四段式。（实测语料里这类误报占「## 第四段」翻转
    #       的相当一部分。）
    #
    #       两条使用边界，写在这里免得后来人当成漏检：
    #         - 当 `{{` 与其他不支持语法（`&&` / `||` / `@get:` / `$n`）共存时，
    #           优先报「模板」。两者结论同为 unknown，只是原因更精确的那一个不报。
    #         - 判定扫的是**整条** raw，所以 `{{` 只出现在 `##` 正则/替换段里也算命中。
    #           这类规则离线回放同样不保证语义一致，归 unknown 是保守正确的方向。
    if "{{" in raw:
        pr.unsupported = "{{}} 模板/变量求值需要 Legado 的 JS 引擎，无法离线回放"
        return pr

    # 3.1) Legado 支持但本项目回放不了的语法
    #      必须显式报 unsupported，否则会被静默当成 CSS 选择器跑出空结果，
    #      让试跑把「工具测不了」误判成「源坏了」
    if raw.startswith("@@"):
        pr.unsupported = "@@ 强制 jsoup 规则暂未支持"
        return pr
    if rl.startswith("@webjs:"):
        pr.unsupported = "@webjs: 注入 WebView 执行 JS，需要 Legado 引擎，无法离线回放"
        return pr
    if "@get:{" in body or "@put:{" in body:
        pr.unsupported = "@get: / @put: 变量读写暂未支持"
        return pr
    if "&&" in body or "%%" in body:
        pr.unsupported = "多规则合并（&& / %%）暂未支持"
        return pr
    # 下面三条只管 **CSS** 规则。`$.data[0].name` 里的方括号是 **JSONPath 下标**，
    # 而 JSONPath 那条路（`_parse_json_steps`）本来就支持它——不设这道门，json 源
    # 会被自己的下标判成「索引式未实现」（实测 49 条规则误伤，其中 7 条落在本地校验
    # 真正评估的 5 个字段里）。判据用 `pr.kind`：它在第 2 步就定好了。
    if pr.kind != "json":
        if re.search(r"\[\s*-?\d+(?:\s*:\s*-?\d+){1,2}\s*\]", body):
            pr.unsupported = "区间索引（[start:end:step]）暂未支持"
            return pr
        # 方括号索引式的其余形态（`[-1]` / `[0]` / `[1,3]` / `[!0]`）。Legado 的
        # `AnalyzeByJSoup.findIndexSet` 只认数字 / `-` / `:` / `,` / `!` 与空格，所以
        # **`a[href]` 这类 CSS 属性选择器（含字母）不会命中这里**——那些是合法 CSS，
        # 一个字都不能动。点式的 `.0` / `.-1` 我们已经实现，方括号式没实现，而它交给
        # BeautifulSoup 是**语法错误**（`div[-1]` 抛 SelectorSyntaxError）。
        if re.search(r"\[[\s\d,:!-]*\]", body):
            pr.unsupported = ("索引式（[-1] / [0] / [1,3] / [!0]）暂未支持；"
                              "点式的 .0 / .-1 支持")
            return pr
    # 位置索引的**排除**写法：`li!0`（除第 0 个）/ `li!-1` / `dd!0:1:2`（一串下标）。
    # 依据 `AnalyzeByJSoup.findIndexSet` 的无括号分支：`!` 与 `.` 都是索引分隔符，
    # 但语义相反——`.` 是「留下这些下标」，`!` 是**排除**这些下标。我们只实现了 `.`。
    # CSS 选择器里不会出现 `!数字`（`!important` 只在声明里、不进选择器），不会误伤。
    if pr.kind != "json" and re.search(r"!\s*-?\d", body):
        pr.unsupported = ("排除索引（!0 / !-1 / !0:1:2）暂未支持；"
                          "选第几个用点式 .0 / .-1")
        return pr
    # XPath：Legado 认 `//` 开头的规则，我们只能连 App 试。
    # 放在 `@xpath:` 那条之前没关系——两条给的是同一个结论与措辞。
    if body.lstrip().startswith("//"):
        pr.unsupported = "XPath 规则需要 Legado 引擎，无法离线回放"
        return pr
    # 段首的 `text.` / `children.` 简写。依据 `AnalyzeByJSoup.kt:310-322` 的分派表：
    # `children` → `children()`（点后的名字被忽略）、`text.X` →
    # `getElementsContainingOwnText(X)`。两个我们都没实现，而它们**不是 CSS**：
    # `text.查看完整目录` 会被当成「标签 text、类名 查看完整目录」，永远选不中 →
    # 静默跑空 → 判源失效。这条与上面那条是同一件事：没实现 ≠ 源坏了。
    if re.search(r"(?:^|@)\s*(?:text|children)\s*\.", body):
        pr.unsupported = "text. / children. 简写暂未实现（本地选不中节点）"
        return pr
    if re.search(r"\$\d{1,2}", body):
        pr.unsupported = "$n 取列表第 n 项暂未支持"
        return pr
    # 原本这里还有一条 `raw.count("##") >= 3` → 「## 第四段暂未实现」，
    # 2026-09-16 实现该形式后删除（切法见上面第 1 步，应用见 _apply_regex）。

    if pr.kind == "xpath" or bl.startswith("@xpath"):
        pr.unsupported = "XPath 规则需要 Legado 引擎，无法离线回放"
        return pr
    if "||" in body:
        pr.unsupported = "备选规则（||）暂未支持"
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


class UnsupportedSelector(Exception):
    """这个选择器交给 BeautifulSoup **解析不了**（如 Legado 的 `div[-1]` 索引式）。

    **它不是「规则取不到值」**——我们根本没能力评估它。异常在这里被吞掉的话，
    「我们解析不了」会一路表现成「这条规则跑出空」，最后判成**源失效**
    （与 AGENTS #4 同一件事：不支持的语法必须显式返回原因，不能静默返回空）。
    """


def _assert_selector_supported(selector: str) -> None:
    """先单独校验选择器**本身**，不依赖当前有几个节点。

    只在 `select()` 抛异常时才报错是不够的：节点列表为空时循环体根本不执行，
    同一个非法选择器会**因为上游有没有命中**而得出两种结论（unknown / fail）。
    判定不该依赖这种偶然。

    没装 soupsieve 时静默跳过（退回运行期兜底）——它不是我们的直接依赖，
    但要防它缺失时这里变成新的抛异常点。
    """
    try:
        import soupsieve
    except Exception:
        return
    try:
        soupsieve.compile(selector)
    except Exception as e:
        raise UnsupportedSelector("选择器无法解析：%s（%s）" % (selector, type(e).__name__)) from e


def _css_select(nodes: Sequence[Any], selector: str) -> List[Any]:
    """在当前节点集合内做后代选择，按文档顺序去重。"""
    _assert_selector_supported(selector)
    out: List[Any] = []
    seen = set()
    for n in nodes:
        if not _is_tag(n):
            continue
        try:
            matches = n.select(selector)
        except Exception as e:
            # 不吞：交给 `_walk_hits` 变成 rule_error（unknown），别静默当空
            raise UnsupportedSelector("选择器无法解析：%s（%s: %s）"
                                      % (selector, type(e).__name__, e)) from e
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
        # 这里原本有一条 `st == "raw"` 的分支（`@html:` 规则：整份响应体就是命中
        # 内容），**已随 `@html:` 前缀一起删除**：那个前缀不是 Legado 的语法
        # （见 RULE_PREFIXES 的注释），删掉之后 `("raw", …)` 这个步骤类型
        # **没有任何生产者**（`_parse_css_steps` 只产 select/attr/index，
        # `_parse_json_steps` 只产 key/wild/index），整段是死代码。
        if st == "select":
            try:
                nodes = _css_select(nodes, val)
            except UnsupportedSelector as e:
                # 交给调用方当 rule_error / unsupported（unknown），**不是**取不到值
                return [], [], str(e), []
        elif st == "attr":
            # 取值动作发生前，当前 nodes 就是命中块
            return [], [_extract_value(n, val, kind) for n in nodes], "", list(nodes)
        elif st == "index" and kind == "json":
            # JSON 的数组下标要交给 `_json_walk`（它按「当前值是不是 list」取下标）。
            # 通用那条分支是对**中间结果列表**取下标，用在 JSON 上等于「取第几个中间
            # 结果」——`$.data.list[0].name` 会先 key data → key list → 拿到整个 list，
            # 再 index 0（取的是那个 list 自己）→ key name 落空，**永远取不到值**。
            # 这条路由一直没接上，于是 JSON 下标规则全被当成「源取不到值」。
            nodes = _json_walk(nodes, st, val)
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


def _apply_regex(values: Sequence[Any], regex: str, replacement: Optional[str],
                 replace_first: bool = False) -> List[str]:
    """对取出的值做正则后处理。

    **两支的行为不同，别当成一支**（Legado `AnalyzeRule.kt:487-497` 的 `replaceRegex`）：

    - 三段式 ``##正则##替换``：全部替换；**不匹配则原样保留**
    - 四段式 ``##正则##替换###``（``replace_first``）：只替换**第一个**匹配；
      **不匹配则返回空串**——不是原样保留

    「不匹配就返回空」看着刺眼，但它是 App 的行为，复刻它才谈得上「判定对齐 Legado」；
    代价是一条正则不命中的规则这一格会变空，在报告里可能表现为判失败。
    """
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
            if replace_first:
                # count=1：只替第一处；没命中给空串（对齐 Legado 的 `else ""`）
                res.append(pat.sub(repl, s, count=1) if pat.search(s) else "")
            else:
                res.append(pat.sub(repl, s))
        except Exception:
            res.append(s)
    return res


def _root_of(content: str, kind: str) -> Any:
    if kind == "json":
        return _loads_json(content)
    # `kind == "html"` 的分支随 `@html:` 前缀一起删了（那个前缀不是 Legado 的，
    # 库里 0 条规则用它）。留着的话就是一段「看起来像合法输入」的死代码——
    # 正是 lessons §十九 说的那种陷阱
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
    return _apply_regex(values, pr.regex, pr.replacement, pr.replace_first), ""


def extract_field_in_nodes(
    content: str,
    container_rule: str,
    field_rule: str,
    limit: int,
    max_chars: int,
) -> Tuple[List[str], List[str], str]:
    """在 ``container_rule`` 选中的**每个节点内**求 ``field_rule``，每个节点取首个非空值。

    这是 Legado 的**字段作用域**语义：列表字段（`bookUrl` / `chapterUrl` /
    `chapterName` …）是在列表节点内求值的，不是对整页求值。

    **不这么做的代价是整条链路都错**——实测：`bookUrl` 写成 `tag.a.0@href` 时对
    整页求值会先命中**导航栏的第一条链接**，于是详情页变成站点首页，目录、正文
    跟着全错。三条真实源（SF轻小说 / 溜达小说 / 次元姬子）的详情页都被解成
    首页；对比之下 App 引擎拿到的是真正的书页（`/so/45121/` 这类）。
    「`bookUrl` 少了 bookList 作用域」这条的机制与实测见 lessons §二十三「第二次实证」
    与 §四十四。

    返回 ``(values, hits, error)``，与 ``extract_all_nodes`` 同形：
    ``values`` 与容器节点**一一对应**（该节点的字段求值为空时落空串，保持对齐——
    调用方要按"第几条结果"配对时，错位比缺项更难查）。

    ``limit`` / ``max_chars`` 同样必须显式传（证据预算只在 core.quality 定义一处）。
    """
    cpr = parse_rule(container_rule)
    if cpr.unsupported:
        return [], [], cpr.unsupported
    fpr = parse_rule(field_rule)
    if fpr.unsupported:
        return [], [], fpr.unsupported
    if not cpr.steps:
        return [], [], "容器规则为空"
    if not fpr.steps:
        return [], [], "字段规则为空"
    try:
        root = _root_of(content, cpr.kind)
    except Exception as e:
        return [], [], "内容解析失败：%s" % type(e).__name__
    nodes, _values, err, _hits = _walk_hits([root], cpr.steps, cpr.kind)
    if err:
        return [], [], err
    values: List[str] = []
    hits: List[str] = []
    for node in nodes[:limit]:
        v_nodes, v_values, v_err, v_hits = _walk_hits([node], fpr.steps, fpr.kind)
        if v_err:
            values.append("")
            continue
        if v_values is None:
            if fpr.kind == "json":
                v_values = [_json_to_text(n) for n in v_nodes]
            else:
                v_values = [_extract_value(n, "text", fpr.kind) for n in v_nodes]
        v_values = _apply_regex(v_values, fpr.regex, fpr.replacement, fpr.replace_first)
        first = next((str(v).strip() for v in v_values if str(v or "").strip()), "")
        values.append(first)
        hits.extend(v_hits or [])
    return values, hits, ""


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
    values = _apply_regex(values, pr.regex, pr.replacement, pr.replace_first)

    # 命中节点 -> HTML 片段：DOM 节点取 outerHTML，JSON 节点退化为文本
    hits: List[str] = []
    for node in hit_nodes[:max(0, limit)]:
        html = str(node) if _is_tag(node) else _json_to_text(node)
        hits.append(html[:max_chars] if max_chars > 0 else html)
    return values, hits, ""


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
    return _apply_regex(values, pr.regex, pr.replacement, pr.replace_first)


def parse_field_first(node: Any, rule: str) -> str:
    """在单个节点上取第一条结果。"""
    vals = parse_field(node, rule)
    return vals[0] if vals else ""


# 这里原本有个「兼容旧接口」段，只剩 `apply_css_rule` / `extract_first` 两个转发
# 函数。两者都已删除：
#   - `apply_css_rule`：三个调用点（checker / verify / add_source）用的都是
#     `from core.rules.replayer import extract_all as apply_css_rule` 这个**别名**，
#     从来没人调 replayer 自己的那个——它注释里写的「add_source 仍在用」是错的
#   - `extract_first`：全仓库零调用
# 转发函数的价值是「让旧调用点不用改」，既然没有旧调用点，它只是多一层间接。

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
        # 原本是三支（图片扩展名 / URL 前缀且含 <img> / 含 <img>）：三支体完全相同，
        # 中间那支的条件又是第三支的子集，合成一支。
        # **不是「后一支不可达」**——`<img src=…>` 这种不以 URL 前缀开头的值
        # 只有第三支接得住（实测 image_ratio(['<img src=x>']) == 1.0）
        if _IMG_EXT_RE.search(v) or "<img" in v.lower():
            hits += 1
    return hits / len(vals)


__all__ = [
    "ParsedRule", "parse_rule", "rule_supported", "rule_kind",
    "extract_all", "extract_all_ex", "extract_all_nodes",
    "parse_list", "parse_field", "parse_field_first",
    "looks_like_image_rule", "image_ratio",
]