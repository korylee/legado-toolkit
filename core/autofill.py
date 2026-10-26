# -*- coding: utf-8 -*-
"""详情页「一键填充」：**按字段以引擎实测值反推规则**，全程免费（不发模型请求）。

它用的是**调试返回体里现成的东西**（页面整页 + 引擎实测值），所以编排成
``attach_autofill``：``run_jvm_debug`` / ``run_app_debug`` 组装完 ``steps`` + ``pages``
顺手调它，建议直接挂在对应步上——不存在单独的请求与入参。复用的两块现成件：
  - ``core.candidates.find_candidates``：从已抓的详情页 HTML 猜候选（四类，带样本）；
  - ``core.repair.suggest.preselect``：拿**引擎实测值**当基准给候选打分（strong 精确
    相等 / weak 包含）——锚点就是 ``step.structured.bookUrl`` 里的字段值。

四条纪律：
  - **判定不在这里**：填出的规则只是建议，验收走「重调本步验证」的真引擎（AGENTS #3）；
  - **比不了就如实说**：某字段在页面上找不到像实测值的材料，该行给 ``reason`` 留白，
    不编一个默认规则（AGENTS #4）；
  - **锚点用哪份要写明**：锚点是这一轮 structured 里的引擎实测值。规则还是空的时候
    引擎取到的就是空——那几个字段没有锚点，**不硬比**；
  - **只出敢拍板的那条**：这一层产出会**直接落进字段输入框**（不再有预览表 + 勾选），
    所以「页面上像该字段的头名」「不敢拍板时的头名候选」一律**不出规则文本**——
    用户实测反馈「填的几乎都不可用，不如不填」，这里按「宁可空着」处理：没有
    ``preselect`` 的结论就只有原因，没有 rule。
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple
import re

from core import candidates as candidates_mod
from core.candidates import _first_class
from core.html import make_soup
from core.repair.suggest import preselect

#: 字段中文名（参考行的那句 reason 用）。权威词表在前端 RULE_FIELD_DEFS /
#: Kotlin BOOKINFO_FIELD_LABELS；这里只要意思对，不参与任何判定。
FIELD_LABELS = {
    "name": "书名", "author": "作者", "kind": "分类",
    "word_count": "字数", "last_chapter": "最新章节", "intro": "简介",
    "cover_url": "封面", "toc_url": "目录链接",
    "book_list": "书籍列表容器", "book_url": "书籍链接",
    "chapter_list": "章节列表容器", "chapter_name": "章节名", "chapter_url": "章节链接",
}

# ---------------------------------------------------------------- FILL_SPECS
#
#: **填充规格表**（泛化的落点）：步骤 → 该步各字段的填充方式。
#: 加一步（search / toc / content）= 加一个键，编排函数（autofill_detail /
#: attach_autofill / agent_loop）按表展开，**不再加 bookUrl 特判**。
#: 键语义（dict 顺序即预览表行序）：
#:   kind        候选取样类型（core.candidates.KINDS）；也是 AI 提议的取样类型。
#:               ``script`` 是**没有 DOM 候选可采**的那一类（页面脚本，不是取值规则）
#:   anchor      structured 里锚点值的取法——None = 直取字段名；
#:               列表形状给 ``(键, 子键)``：从 items[0] 取子键
FILL_SPECS = {
    "bookUrl": {
        "name": {"kind": "text"},
        "author": {"kind": "text"},
        "kind": {"kind": "text"},
        "word_count": {"kind": "text"},
        "last_chapter": {"kind": "text"},
        "intro": {"kind": "text"},
        "cover_url": {"kind": "media"},
        "toc_url": {"kind": "link"},
    },
    "search": {
        # 列表容器：引擎 `BookList.analyzeBookList` 是**两段式**——先用它取条目元素，
        # 再在**每个条目里**取 name/bookUrl。没有它就是「列表为空」（实测 2026-10-26：
        # 只有合并链 `.imgBox@tag.li@tag.a@href` 时引擎 total=0，补上容器后 total=1）。
        # `list` 这个 kind 让候选取样走「一组同形元素」，与容器规则的形态对得上
        # （`rule_shape` 判 `container`，`can_fast_verify` 放行）。
        "book_list": {"kind": "list"},
        # 下面是**条目内规则**（相对条目元素），不是从整页起的完整链
        "book_url": {"kind": "link", "anchor": ("items", "book_url")},
        "name": {"kind": "text", "anchor": ("items", "name")},
    },
    #: 目录段：与搜索段同构——`chapterList` 是**条目容器**，另两个是条目内规则
    #: （上游 `BookChapterList` 先按 `chapterList` 切条目、再逐条目取名称与链接）。
    "toc": {
        "chapter_list": {"kind": "list"},
        "chapter_name": {"kind": "text"},
        "chapter_url": {"kind": "link"},
    },
    #: 正文步：`webJs` 是页面脚本（跑在页面环境里，它的结果成为这一步的响应体），
    #: `content` 是取值规则——材料是脚本输出时，它读的 `result` 就是 webJs 的产出。
    "content": {
        "webJs": {"kind": "script"},
        # 正文取的是**一段内容**，不是一组链接：`content` 走单值口径，可快验
        "content": {"kind": "text"},
    },
}


def field_kind(step: str, field: str) -> str:
    """某步某字段的候选取样类型——**只有 FILL_SPECS 一份**。

    ``kind`` 决定候选族与 AI 提议的取样范围；原先另有一张 ``FIELD_KINDS`` 表，
    两张表各改一边的后果不是报错，是「这个字段的候选永远猜不出来」。
    """
    return str(((FILL_SPECS.get(step) or {}).get(field) or {}).get("kind") or "text")


def segment_fields(step: str) -> List[str]:
    """这一步的**全部可补字段**（顺序即 `FILL_SPECS` 的定义序，也就是预览表行序）。

    段级补全 = 一次 `run_loop(fields=segment_fields(step))`：同一段共享一份 DOM 大纲
    与页面变量，一次调用问全段比逐字段问省 N 次往返（判据在 `suggest_many` 的
    「按 kind 分组」——这里只回答「这一段有哪些字段」）。
    """
    return list((FILL_SPECS.get(step) or {}).keys())


def container_field(step: str) -> str:
    """这一段的列表容器字段名（`kind == "list"` 的那个）；没有就是空串。

    有容器时其余字段是**条目内规则**（相对条目元素）——`BookList` / `Toc` 都是两段式。
    它决定快验怎么跑：单跑一条条目内规则验的是「整页第一个元素」，答案与引擎相反。
    """
    for name in segment_fields(step):
        if field_kind(step, name) == "list":
            return str(name)
    return ""


#: 容器字段 → (源里的组, 规则名)。**只有这两个字段带容器**（搜索/目录各一个），
#: 所以不必再维护「段 → 组」与「字段 → 驼峰名」两张表——那两张表只是为了拼出这一行。
_CONTAINER_PATHS = {"book_list": ("ruleSearch", "bookList"),
                    "chapter_list": ("ruleToc", "chapterList")}


def container_rule(source: Dict[str, Any], step: str) -> str:
    """本段列表容器在源里的当前值（搜索段 `bookList`、目录段 `chapterList`）。

    容器**从源里读，不要调用方传**：它一直在源配置里，而 `fast_verify` 本来就手持整份源。
    让前端补参数等于要求用户「先填容器才能验下一行」——顺序一乱就验不了。
    """
    group, rule_name = _CONTAINER_PATHS.get(container_field(step), ("", ""))
    if not group:
        return ""
    return str((source.get(group) or {}).get(rule_name) or "").strip()


def propose_fields(step: str, material_kind: str = "",
                   page_stats: Optional[Dict[str, Any]] = None) -> List[str]:
    """这一段**该提议哪些字段**——按材料事实裁，不靠模型自觉。

    正文段有两个字段，但**只有一种情况要两个**：内容在页面变量里时，`webJs` 才是
    必需的（把变量取出来），`content` 只负责包装。DOM 里已经有目标时，`content`
    一条就够——这时把 `webJs` 也列给模型，它会**多写一条用不上的脚本**
    （实测 2026-10-23 发发漫画：content 一条就取到 18 张图，模型还额外给了
    `G_zc.contents()` 的 webJs）。

    判据复用「图在不在 DOM 里」那一份（`suggest.outline_plan` 的 stats 档同一个比例）：
    带 `src` 的图占比够高 = DOM 里有目标 → 不给 `webJs`。
    """
    fields = segment_fields(step)
    if "webJs" not in fields:
        return fields
    from core.repair.suggest import outline_plan

    plan = outline_plan({"material_kind": material_kind, "page_stats": page_stats})
    if plan.get("mode") in ("stats", "skip"):
        # 图不在 DOM 里（或材料不是 DOM）：内容只能在页面变量里 → 两个字段都要
        return fields
    return [f for f in fields if f != "webJs"]


#: **源级字段**（不属于任何段，整个源共用）。与 `FILL_SPECS` 分开是因为落点不同：
#: `FILL_SPECS` 的键是 `ruleBookInfo.name` 这类段内路径，而 `header` 挂在源根上
#: （`BookSource.header`，上游 `BaseSource.getHeaderMap` 读它）。
SOURCE_LEVEL_SPECS = {
    #: 请求头规则：JSON 文本，或 `@js:` / `<js>` 求值出 JSON 字符串。
    #: **什么时候该提它**：这一步的材料是拦截页（`core.quality.interstitial_marker`），
    #: 或**别的段**被拦（`step_health`）——那是「请求被拒」的唯一可靠事实。
    #: **不是**「页面声明了 no-referrer」也不是「图片打不开」：前者是页面给浏览器的期望，
    #: 而 App 用 OkHttp/Cronet 发请求、根本不看 HTML 里的 referrerpolicy；
    #: 后者是探活反推（TODO `image-probe-is-not-a-verdict`）。
    "header": {"kind": "text"},
}


def _no_rule_reason(preselect_reason: str, has_basis: bool) -> str:
    """比不出来时的**一句话**原因（用户场景是「从零建源」，话要短）。

    两类：没有锚点（规则全空时引擎取到的就是空，正常现象）；有锚点但比不了
    （沿用 preselect 的结论）。不再出现「连 App」——无锚点不是通道问题。
    """
    if not has_basis:
        return "没有实测值可比：先随便填一条基础规则，重调后有了实测值再一键填充"
    return preselect_reason or "这一页上比不出来"


def _engine_values(structured: Dict[str, Any], field: str,
                   step: str = "bookUrl") -> List[str]:
    """某字段在这轮调试里的**引擎实测值**（锚点）。空值不给锚——空是「引擎取到空」，
    不是「页面没有」；拿空当基准会把所有候选都判成 strong（页面文本 ≠ 空串）。

    锚点的取法跟着 FILL_SPECS 的 ``anchor`` 走（搜索步是列表形状：items[0].子键），
    与 :func:`autofill_detail` 用的是同一份判据。
    """
    anchor = ((FILL_SPECS.get(step) or {}).get(field) or {}).get("anchor")
    if anchor:
        return _anchor_from_list(structured, anchor)
    value = str((structured or {}).get(field) or "").strip()
    return [value] if value else []


#: 无锚点字段的候选**取样层**：详情字段是页面上的**结构行**（书名/作者/简介），
#: 不是 200 字的正文块——正文候选族对它们恒空（取样范围见 `core.candidates`）。
#: 用 head 内短文本块（标题行 h1～h6、段落 p、强调 strong/b/span），就是
#: 「作者: 夏子久」那一层。
_HEAD_TEXT_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6", "p", "strong", "b", "span")


def _head_text_candidates(html: str, limit: int = 3) -> List[Dict[str, Any]]:
    """页面上带 class 的**短文本块**候选（无锚点字段没别的东西可比）。

    与 `core.candidates._text_candidates` 分工：那边是正文块（≥200 字），这边是
    结构行（不限长度、只要求有 class——规则要写得出来）。只编 Legado 常见写法：
    ``.类@text`` / ``tag.X@text``，不猜属性链。
    """
    soup = make_soup(html or "")
    out: List[Dict[str, Any]] = []
    seen = set()
    for el in soup.select(", ".join(_HEAD_TEXT_TAGS)):
        text = re.sub(r"\s+", " ", el.get_text() or "").strip()
        if not text or len(text) > 200:
            continue
        cls = _first_class(el)
        rule = (".%s@text" % cls) if cls else ("tag.%s@text" % (el.name or "").lower())
        if rule in seen:
            continue
        seen.add(rule)
        out.append({"rule": rule, "kind": "text", "count": 1,
                    "samples": [text[:60]], "hits": 1, "uniq": 1, "ratio": 0.0})
        if len(out) >= limit:
            break
    return out


def attach_autofill(steps: List[Dict[str, Any]],
                    pages: List[Dict[str, Any]]) -> None:
    """把详情组建议**内嵌**进调试结果（原地写 ``steps[].autofill``）。

    材料（页面整页）与锚点（引擎实测值）都在手上，调用点在两通道组装完
    ``steps`` + ``pages`` 之后。按 FILL_SPECS 表展开——表里有的步都自动带上
    建议（现在 bookUrl + search；表扩即扩）。**建议失败绝不带走判定**（同
    matched_html / network 的纪律）：整块 try，炸了只留一行附注。
    """
    try:
        by_page = {str(p.get("id") or ""): p for p in (pages or [])}
        for step in (steps or []):
            name = str(step.get("name") or "")
            if name not in FILL_SPECS:
                continue
            page = by_page.get(str(step.get("page_id") or ""))
            html = str((page or {}).get("html") or "")
            structured = (step or {}).get("structured")
            if not (html.strip() and isinstance(structured, dict)):
                continue
            step["autofill"] = autofill_detail(
                html, structured, step=name,
                material_kind=str((page or {}).get("material_kind") or ""))
    except Exception as e:  # 建议是附赠品：炸了说一声，判定不动
        for step in (steps or []):
            if step.get("name") in FILL_SPECS:
                step["notes"] = list(step.get("notes") or []) + [
                    "一键填充建议生成失败：%s" % e]
                step["has_notes"] = True
        return


def autofill_detail(html: str, structured: Dict[str, Any],
                    limit: int = 3, step: str = "bookUrl",
                    material_kind: str = "") -> Dict[str, Any]:
    """逐字段出建议（按 FILL_SPECS 表展开）。返回 ``{fields: [...], free_ok: True}``：
    每行 ``{field, reason, ...}``，**只有 ``rule`` 是敢拍板的那一条**——没有 ``rule``
    就是不产出规则（``need_ai`` = 比不出结论 / ``no_anchor`` = 没有实测值可比），
    原因照旧在 ``reason`` 里，交给模型提议或人填。

    ``structured`` 的形状按表里的 ``anchor`` 取：详情段是平的（直取字段名），
    搜索段是列表（items[0].子键）。

    ``material_kind`` 是这一步材料的**引擎事实**（`page["material_kind"]`，判据一份在
    `core.jvm_debug.material_kind`）：材料是页面脚本的输出时，DOM 候选没有意义——
    实测在那种材料上找候选会拍出「把地址列表逐行当正文」的假通过（`body@html` 也 pass）。
    这类字段一律标 ``need_ai``，让 AI 按材料范式写。
    """
    from core.material import can_fill_dom

    html = html or ""
    structured = structured if isinstance(structured, dict) else {}
    spec = FILL_SPECS.get(step) or {}
    fields: List[Dict[str, Any]] = []
    for field, cfg in spec.items():
        kind = cfg["kind"]
        row: Dict[str, Any] = {"field": field}
        if not html.strip():
            row["reason"] = "这一步没有页面材料"
            fields.append(row)
            continue
        if kind == "script":
            # 页面脚本没有「DOM 候选」可采（它不是取值规则，跑在页面环境里）。交给 AI——
            # 真去调 find_candidates 只会抛「未知的候选类型」再被吞成一句工具故障（AGENTS #4）
            row["reason"] = "页面脚本要在页面环境里写（result = params.<键>…），交给 AI"
            row["need_ai"] = True
            fields.append(row)
            continue
        if not can_fill_dom(material_kind):
            # 材料**不是 DOM**（页面脚本的输出）：在它上面找候选会拍出「把地址列表当正文」
            # 那种假通过（同一页实测：不写 webJs 时整页 HTML 逐行包成 <img> 也 pass）。
            # 取值规则得读 result，按材料交 AI 写——判据是引擎事实，不读源配置。
            row["reason"] = ("这一步的材料是页面脚本的输出，取值规则要读 result"
                             "——交给 AI 按材料写")
            row["need_ai"] = True
            fields.append(row)
            continue
        try:
            if kind == "text":
                candidates = (candidates_mod.find_candidates(html, kind, limit=limit)
                              or _head_text_candidates(html, limit))
            else:
                candidates = candidates_mod.find_candidates(html, kind, limit=limit)
        except Exception as e:  # 单字段失败不带整批（同 matched/structured 的纪律）
            row["reason"] = "候选生成失败：%s: %s" % (type(e).__name__, str(e)[:80])
            fields.append(row)
            continue
        anchor = cfg.get("anchor")
        if anchor:
            basis = _anchor_from_list(structured, anchor)
        else:
            basis = _engine_values(structured, field)
        if not basis:
            # 无锚点（从零建源：规则全空，引擎取到的就是空）。**不出规则**：
            # 没有基准的比较是编出来的分，而这一轮的值会直接落进输入框——
            # 填一条编出来的还不如空着（用户实测：填的几乎都不可用）。
            row["no_anchor"] = True
            row["reason"] = _no_rule_reason("", False)
            fields.append(row)
            continue
        pick = preselect(candidates, basis, html, step=step, kind=kind)
        ranked = pick.get("ranked") or []
        # preselect 的 picked 是**完整的 rank 条目**（rule/score/samples 一起带），
        # 不是字符串——直接摊开，别再从 ranked 里反查一遍
        picked = pick.get("picked")
        if isinstance(picked, dict) and picked.get("rule"):
            row["rule"] = picked["rule"]
            row["score"] = picked.get("score", 0)
            row["samples"] = list(picked.get("samples") or [])
            row["reason"] = pick.get("reason") or ""
        elif ranked:
            # preselect 拒绝拍板（并列 / 低分 / 只有 weak）：**只有原因，没有规则**。
            # 这就是「仅参考」那一行的由来，也是要删掉的那一行：头名候选对不上
            # 实测值的不比没有，直接落进输入框等于让用户先信一次假的。
            row["reason"] = pick.get("reason") or ""
            row["need_ai"] = True
        else:
            row["reason"] = _no_rule_reason(pick.get("reason") or "", bool(basis))
        fields.append(row)
    return {"fields": fields, "free_ok": True}


def _anchor_from_list(structured: Dict[str, Any],
                      anchor: Tuple[str, str]) -> List[str]:
    """列表形状 structured 的锚点取法：``(items, book_url)`` = items[0].book_url。
    空列表/缺键都返回空（没有锚——与 _engine_values 同一纪律）。"""
    container, key = anchor
    items = structured.get(container)
    if not isinstance(items, list) or not items:
        return []
    value = str((items[0] or {}).get(key) or "").strip()
    return [value] if value else []


def _candidates_for_ai(html: str, kind: str) -> List[Dict[str, Any]]:
    """AI 提议用的候选集（同进程取，不经过 HTTP）。取不到就空着：模型还有
    DOM 大纲可用（suggest 的 build_prompt 自己兜）。"""
    try:
        return candidates_mod.find_candidates(html, kind, limit=6)
    except Exception:
        return []
