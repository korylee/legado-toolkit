# -*- coding: utf-8 -*-
"""从**我们补抓的那份 HTML** 里猜「这一步该写什么规则」——交互候选面板的唯一一份。

四类候选（每类的用途不同，别只给一类）：``link``（详情/章节链接）、``media``
（图片/音频）、``text``（正文块）、``list``（书目/章节列表的容器+条目）。
候选面板只展示与排序，**没有「试」**：验一条候选要走真引擎（「用这条」填进
表单 + 「重新调试本步」）。

三条设计约束（随实现从前端 ruleCandidates.js 下沉而来）：
  1. 用的是**补抓的原文**，不是渲染后的 DOM——App 走自己的 WebView，JS 注入的
     内容只在它那份里；候选取自原文才与「App 会看到什么」一致，也才和本地
     回放器能跑的东西一致。
  2. **每个候选都给出「它选出了几条 + 前几个值」**。不给样本的候选等于让用户
     再猜一次；而取到 0 条的候选要能一眼看出来。
  3. 排序是**启发式**（各 family 内写明），只影响顺序、不影响内容——排错的
     代价是"多看一眼"，不是"给错规则"。

实测口径（**与前端点选的 `selector.js` 同一 spec**，两边不许各改各的）：
``hits`` = 非空值条数；``uniq`` = 去重后的条数——「命中 1228」与「命中 1198」
在界面上都只是个数字，能分开它们的是去重与占比；``ratio`` = hits / 页面链接数。
"""

import re

from typing import List

from core.html import make_soup

KINDS = ("link", "media", "text", "list")

#: 正文块的最短文字量：低于它的元素不当正文候选（导航/标题噪声）
_TEXT_MIN_CHARS = 200
#: 同一父节点下少于这个数的同形子元素不成列表
_LIST_MIN_ITEMS = 3
#: 每条候选带几个示例值
_SAMPLE_COUNT = 3

_MEDIA_ATTRS = ("src", "data-original", "data-src")

#: 链接像不像「详情页 / 章节页」：含数字（书号/章节号）、或 .html、或路径够深。
#: 这条只用来**排序**——真正的判据是「取到几条 + 样本长什么样」，由用户看
_TARGET_RE = re.compile(r"\d")
_HTML_RE = re.compile(r"\.html?($|\?)", re.I)


def _looks_like_target(href: str) -> bool:
    h = str(href or "")
    if not h or h.startswith("#") or h.startswith("javascript:"):
        return False
    if _TARGET_RE.search(h) or _HTML_RE.search(h):
        return True
    return len([p for p in h.split("/") if p]) >= 2


def _first_class(el) -> str:
    # Legado 的 class 选择器是**单类**匹配，多类拼一起会选不中——只取第一个
    if el is None:
        return ""
    classes = el.get("class")
    if not classes:
        return ""
    parts = classes.split() if isinstance(classes, str) else [c for t in classes for c in str(t).split()]
    return parts[0] if parts else ""


def _measure(soup, values) -> dict:
    vals = [str(v).strip() for v in values if str(v or "").strip()]
    uniq = len(set(vals))
    links = len(soup.select("a[href]")) or 1
    return {"hits": len(vals), "uniq": uniq, "ratio": len(vals) / links}


def _samples(values, good=None):
    """前几个示例值；link 族优先给「像目标」的那些——样本是给用户认的。"""
    vals = [str(v or "").strip() for v in values if str(v or "").strip()]
    if good is not None:
        prefer = [v for v in vals if _looks_like_target(v)][:3]
        if prefer:
            return prefer
    return vals[:_SAMPLE_COUNT]


def _link_candidates(soup, limit):
    # 按规则分组收 href：自己的类 + 父容器类（Legado 里最常见的详情/章节写法）
    groups = {}

    def add(rule, value):
        if rule and value:
            groups.setdefault(rule, []).append(str(value))

    anchors = soup.select("a[href]")
    for a in anchors:
        own = _first_class(a)
        if own:
            add(".%s@href" % own, a.get("href"))
        pcls = _first_class(a.parent)
        if pcls:
            add(".%s@tag.a@href" % pcls, a.get("href"))
    # 兜底：全部链接——条数会很夸张，但能让用户看出"页面里到底有多少链接"。
    # （前端那份只收了第一个锚点，count 恒为 1，与本意相反；下沉时按本意修正）
    for a in anchors:
        add("tag.a@href", a.get("href"))

    out = []
    for rule, hrefs in groups.items():
        good = sum(1 for h in hrefs if _looks_like_target(h))
        m = _measure(soup, hrefs)
        out.append({"rule": rule, "kind": "link", "count": len(hrefs),
                    "samples": _samples(hrefs, good=good), **m,
                    "_good": good})
    # 像目标的越多越靠前；同分时条数少的更聚焦
    out.sort(key=lambda c: (-c.pop("_good"), c["count"]))
    return out[:limit]


def _media_candidates(soup, limit):
    groups = {}

    def add(rule, value):
        value = str(value or "").strip()
        if rule and value:      # `<img src="">` 是 JS 注入留下的空占位
            groups.setdefault(rule, []).append(value)

    for img in soup.select("img"):
        for attr in _MEDIA_ATTRS:
            own = _first_class(img)
            if own:
                add(".%s@%s" % (own, attr), img.get(attr))
            pcls = _first_class(img.parent)
            if pcls:
                add(".%s@tag.img@%s" % (pcls, attr), img.get(attr))
            add("tag.img@%s" % attr, img.get(attr))

    out = []
    for rule, values in groups.items():
        m = _measure(soup, values)
        out.append({"rule": rule, "kind": "media", "count": len(values),
                    "samples": _samples(values), **m})
    # 图址给得最多的规则最可能是真列表
    out.sort(key=lambda c: -c["count"])
    return out[:limit]


def _text_candidates(soup, limit):
    # 正文块 = 纯文字最多、且标签里有 class 的那些——"正文提取"最经典的做法，
    # 也让用户一眼认出哪块是正文。同一 class 会出现多次（嵌套/多块），按规则
    # 归组后取文字最多的几个
    groups = {}   # rule → [每块的压缩前文本（strip 后）]
    for el in soup.select("div, article, section, td, p"):
        cls = _first_class(el)
        if not cls:
            continue
        text = re.sub(r"\s+", "", el.get_text() or "")
        if len(text) < _TEXT_MIN_CHARS:
            continue
        collapsed = re.sub(r"\s+", " ", el.get_text() or "").strip()
        groups.setdefault(".%s@text" % cls, []).append(collapsed)

    out = []
    for rule, blocks in groups.items():
        total = sum(len(re.sub(r"\s+", "", b)) for b in blocks)
        m = _measure(soup, blocks)
        out.append({"rule": rule, "kind": "text", "count": total,
                    "samples": [b[:60] for b in blocks[:_SAMPLE_COUNT]], **m})
    out.sort(key=lambda c: -c["count"])
    return out[:limit]


def _list_candidates(soup, limit):
    # 书目列表 = 「同一个父节点下、同标签同类名的一批兄弟」。规则写成 `.父类 子标签`
    groups = {}   # rule → 最多的一次出现数
    for parent in soup.select("ul, ol, div, section"):
        pcls = _first_class(parent)
        if not pcls:
            continue
        by_child = {}
        for child in parent.find_all(recursive=False):
            key = (str(child.name or "").lower(), _first_class(child))
            by_child[key] = by_child.get(key, 0) + 1
        for (tag, cls), n in by_child.items():
            if n < _LIST_MIN_ITEMS:
                continue
            rule = ".%s .%s" % (pcls, cls) if cls else ".%s %s" % (pcls, tag)
            groups[rule] = max(groups.get(rule, 0), n)

    out = []
    for rule, n in groups.items():
        m = _measure(soup, [])
        out.append({"rule": rule, "kind": "list", "count": n,
                    "samples": [], **m})
    out.sort(key=lambda c: -c["count"])
    return out[:limit]


def find_candidates(html: str, kind: str, limit: int = 6) -> list:
    """找候选。页面解析失败或没有候选时返回**空数组——空数组是个结论**
    （页面上确实没有），由调用方连同诊断一起展示，不要在这里编一个默认规则。

    ``kind`` 不在 :data:`KINDS` 里抛 ``ValueError``（调用方转 400，原因走到
    用户眼前——AGENTS #4）。
    """
    kind = str(kind or "").strip()
    if kind not in KINDS:
        raise ValueError("未知的候选类型：%s（只能是 %s）" % (kind or "未提供", " / ".join(KINDS)))
    limit = max(1, min(int(limit or 6), 20))
    soup = make_soup(html or "")
    run = {"link": _link_candidates, "media": _media_candidates,
           "text": _text_candidates, "list": _list_candidates}[kind]
    return run(soup, limit)


# ---------------------------------------------------------------- 取「候选能取到什么」

#: 带 class 的文本块：与 `_text_candidates` 的取样范围一致（正文/书名这类都要过 class）
_TEXT_TAGS = ("div", "article", "section", "td", "p", "h1", "h2", "h3", "h4", "span", "a")
#: 单个取值最多留多少字符。**在取样时就截断**，而不是收集完再筛：
#: 正文页上一段就是上万字符（实测 11200），不截断的话收集阶段会为几百个元素
#: 各materialize 一份全文再丢掉（白付内存与时间）；而这一层只用来比对
#: 「样子像不像 App 取到的值」，200 字符早已过量（真正的值都比这短）。
_VALUE_MAX_CHARS = 200


def _elem_values(el) -> List[str]:
    """一个元素身上**可能被规则取到**的值：自身文本、href、title，以及直接子元素的对应值。

    子元素那一层要收，是因为列表步的候选是**容器**（`.list .item`），而 App 在列表步
    取到的是**条目里的书名**——不收子元素，列表候选就永远拿不出可比的值。
    """
    out = [_squeeze(el.get_text())[:_VALUE_MAX_CHARS],
           str(el.get("href") or "").strip(), str(el.get("title") or "").strip()]
    for child in el.find_all(True):
        out.append(_squeeze(child.get_text())[:_VALUE_MAX_CHARS])
        out.append(str(child.get("href") or "").strip())
        out.append(str(child.get("title") or "").strip())
    return [v for v in out if v]


def _squeeze(text) -> str:
    """连续空白折成一个空格（比对用的值不需要保留排版）。"""
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _collect(soup, kind: str) -> List[str]:
    """把页面上**同族**的取值收成一个集合（候选能取到的值必然是它的子集）。

    这里不解释规则文本——候选本来就是按这些值分组造出来的（见上面各 family），
    所以「候选能取到什么」与「页面上有什么」是同一份材料，不需要第二套规则解释器。
    """
    if kind == "link":
        return [str(a.get("href") or "").strip() for a in soup.select("a[href]")]
    if kind == "media":
        out = []
        for img in soup.select("img"):
            if _first_class(img):
                out += [str(img.get(attr) or "").strip() for attr in _MEDIA_ATTRS]
        return [v for v in out if v]
    if kind == "list":
        return [v for el in soup.select("ul, ol, div, section") for v in _elem_values(el)]
    out = []
    for el in soup.select(", ".join(_TEXT_TAGS)):
        if not _first_class(el):
            continue
        out += _elem_values(el)
    return [v for v in out if v]


def sample_values(html: str, kind: str, app_values, limit: int = 300) -> List[str]:
    """页面上**像 App 实测值**的那些值——免费初筛的比对基准。

    这是「取候选的值」的**唯一一份**实现（造候选时用的就是同一套取值口径），所以
    免费初筛不必再写一个规则解释器。找不到就返回空列表：调用方要把它读成
    「这一页上比不了」，**不是**「候选取不到值」（AGENTS #4）。
    """
    kind = str(kind or "").strip()
    if kind not in KINDS:
        return []
    wanted = [_squeeze(v).replace(" ", "") for v in (app_values or [])]
    wanted = [w for w in wanted if len(w) >= 2][:limit]
    if not wanted:
        return []
    out = []
    for value in _collect(make_soup(html or ""), kind):
        norm = re.sub(r"\s+", "", value)
        if any(w == norm or w in norm or norm in w for w in wanted):
            out.append(value)
        if len(out) >= limit:
            break
    return out
