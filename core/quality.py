# -*- coding: utf-8 -*-
"""书源提取结果的质量判定。

设计要点
--------
1. **判定底线是「提取值非空 / 不报错」，不自创数值阈值。**
   Legado 的调试只判 ``contentStr.isBlank()``（BookContent.kt:203-205），
   全链路没有长度阈值；自创阈值会误杀短章节，与本仓库 checker 反复强调的
   「不误杀」原则相悖。
2. **启发式只产出 notes 附注，绝不改变 verdict。**
   这样「绿色即是真通过」，同时疑点不会被吞掉。
3. ``unknown`` 是我们的增量。Legado 把「解析为空」与「请求失败」都归为 Error
   （Debug.kt:290/294），也把「静态无法回放的 JS 规则」当普通失败；
   用工具的能力边界去判源的好坏是不对的。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from core.models import BOOK_SOURCE_TYPE_NAMES

# ------------------------------------------------------------------ 三态

VERDICT_PASS = "pass"
VERDICT_FAIL = "fail"
VERDICT_UNKNOWN = "unknown"

#: 实测形态
SHAPE_TEXT = "text"
SHAPE_IMAGE = "image"
SHAPE_AUDIO = "audio"
SHAPE_MIXED = "mixed"
SHAPE_EMPTY = "empty"

# ------------------------------------------------------------------ 截断上限

#: 单页整页源码上限，超出置 truncated=true 并在 UI 标注
MAX_PAGE_HTML_CHARS = 1_000_000
#: 单个命中节点 HTML 上限
MAX_MATCHED_HTML_CHARS = 200_000
#: 命中节点最多回传几个（目录列表类规则会命中上百个；3 个够看出结构）
MATCHED_NODES_LIMIT = 3
#: 单条提取值上限。0 = 不截断——正文全文是本次的核心产出
MAX_VALUE_CHARS = 0
#: 提取值最多回传几条（列表步可能上百条）
VALUES_PREVIEW_LIMIT = 100
#: 证据总量硬上限。这是唯一一道与调用方无关的保险
MAX_EVIDENCE_TOTAL_CHARS = 2_000_000

#: 「正文较短」阈值。取自 Legado 自身 BookContent.kt:194 的
#: ``if (contentStr.length < 500)``，不是我们自创的。**只用于产出附注。**
SHORT_CONTENT_CHARS = 500

#: 实测形态 -> 期望的 bookSourceType。注意没有 3：
#: Legado 的 3 是「只提供下载服务的网站」（BookSourceType.kt:8-11），不是视频
EXPECTED_SHAPE: Dict[int, str] = {0: SHAPE_TEXT, 1: SHAPE_AUDIO, 2: SHAPE_IMAGE}

#: judge_list_step 的步骤名。导出成常量是为了让调用方无法拼错——
#: 只有精确等于 "toc" 才启用 toc 语义（下载源豁免 / 章节数偏少附注），
#: 而 "TOC" 这种大小写笔误会把本该 unknown 的结果变成 fail。
STEP_SEARCH = "search"
STEP_BOOK_URL = "bookUrl"
STEP_TOC = "toc"

#: 结构性 HTML 标签。命中说明多半捞到了容器而不是正文（高精度信号）
STRUCT_TAG_RE = re.compile(
    r"<\s*(div|script|style|nav|header|footer|aside|table|ul|section|form)\b", re.I)

#: 块级分隔符（用于 evidence.block_seps，仅展示）
_BLOCK_SEP_RE = re.compile(r"</?\s*(?:p|div|br|li|tr)\b", re.I)

#: HTML 标签
_TAG_RE = re.compile(r"<[^>]{0,200}>")

#: 中文字符（只用于统计展示，不参与判定）
_CJK_RE = re.compile(r"[一-鿿]")

#: 媒体形态的识别通道
_MEDIA_EXT: Dict[str, re.Pattern] = {
    SHAPE_IMAGE: re.compile(r"\.(?:jpe?g|png|webp|avif|gif|bmp)(?:\?|#|$)", re.I),
    SHAPE_AUDIO: re.compile(r"\.(?:mp3|m4a|aac|ogg|flac|wav|m4b)(?:\?|#|$)", re.I),
}
_MEDIA_TAG: Dict[str, str] = {SHAPE_IMAGE: "<img", SHAPE_AUDIO: "<audio"}

#: 错误页特征词。**仅当提取值总字符 < SHORT_CONTENT_CHARS 时启用**——
#: 整页含「登录」太常见（导航栏就有），长正文里出现「404」也可能是巧合
CONTENT_NOISE_MARKERS: List[str] = [
    "验证码", "人机验证", "安全验证", "访问验证", "安全检测", "继续访问",
    "页面不存在", "内容不存在", "章节不存在", "已下架", "正在审核",
    "维护中", "站点维护", "请开启JavaScript", "请开启javascript",
    "访问受限", "403 Forbidden", "Access Denied", "cloudflare", "captcha",
]


# ------------------------------------------------------------------ 数据结构

@dataclass
class Judgement:
    """一次判定的结论。``verdict`` 只有 pass / fail / unknown 三种。"""

    verdict: str
    reason: str = ""                      # fail / unknown 的原因；pass 时为空
    shape: str = SHAPE_EMPTY              # 实测形态
    notes: List[str] = field(default_factory=list)   # 启发式附注，不改 verdict
    evidence: Dict[str, Any] = field(default_factory=dict)

    @property
    def has_notes(self) -> bool:
        return bool(self.notes)

    @property
    def ok(self) -> bool:
        """兼容映射：仅 fail → False。unknown 仍算 ok（不是坏，是测不了）。"""
        return self.verdict != VERDICT_FAIL

    @property
    def checker_state(self) -> Optional[bool]:
        """给 checker 的三态映射：pass→True / fail→False / unknown→None。

        沿用 checker「无法验证不给分、不误杀」的既有策略。
        """
        if self.verdict == VERDICT_PASS:
            return True
        if self.verdict == VERDICT_FAIL:
            return False
        return None

    def as_step_dict(
        self,
        name: str,
        url: str = "",
        page_id: str = "",
        values: Sequence[str] = (),
        matched_html: str = "",
        rule_error: str = "",
        detail: str = "",
    ) -> Dict[str, Any]:
        """摊平成 ``steps[]`` 的一项。

        **两个调用方必须共用这一个序列化口径。** 若「试跑」和「批量校验」
        各写一份摊平逻辑，一处忘记同步 verdict / notes 就会重新分叉——
        那正是本次改造要消灭的「同源不同判」。所以它放在这里，不放在
        verify.py 里。
        """
        return {
            # ---- 兼容字段：旧前端 / 快速生成 / AI 修复循环都读这两个 ----
            "name": name,
            "ok": self.ok,
            "detail": detail or self.reason or self.shape,
            # ---- 新字段 ----
            "verdict": self.verdict,
            "has_notes": self.has_notes,
            "notes": list(self.notes),
            "reason": self.reason,
            "shape": self.shape,
            "evidence": dict(self.evidence),
            "rule_error": rule_error,
            "url": url,
            "page_id": page_id,
            "values": list(values),
            "matched_html": matched_html,
        }


# ------------------------------------------------------------------ 形态嗅探

def _classify_value(text: str) -> str:
    """单条提取值属于哪种形态。扩展名与标签双通道，任一通道命中即归类。"""
    low = text.lower()
    for shape, pattern in _MEDIA_EXT.items():
        if pattern.search(low):
            return shape
    for shape, tag in _MEDIA_TAG.items():
        if tag in low:
            return shape
    return SHAPE_TEXT


def sniff_shape(values: Sequence[str]) -> Tuple[str, Dict[str, int]]:
    """实测形态嗅探。

    逐条分类后取占比最高者；最高占比未过半（≤ 0.5）→ mixed；全空 → empty。
    返回 (形态, 各形态计数)。
    """
    counts = {SHAPE_TEXT: 0, SHAPE_IMAGE: 0, SHAPE_AUDIO: 0}
    total = 0
    for raw in values or []:
        s = str(raw or "").strip()
        if not s:
            continue
        total += 1
        counts[_classify_value(s)] += 1
    if total == 0:
        return SHAPE_EMPTY, counts
    shape, hit = max(counts.items(), key=lambda kv: kv[1])
    if hit == 0:
        return SHAPE_EMPTY, counts
    # 最高占比未过半即视为形态不统一：1:1 这类平局（如一条图片 + 一条文本）
    # 要判 mixed，所以用 <= 而非 <（plain < 会让平局落在 max() 的字典序赢家上）
    if hit / total <= 0.5:
        return SHAPE_MIXED, counts
    return shape, counts


# ------------------------------------------------------------------ 证据采集

def _tag_ratio(text: str) -> float:
    """提取值中 HTML 标签字符的占比（仅展示，不参与判定）。"""
    if not text:
        return 0.0
    tagged = sum(len(m.group(0)) for m in _TAG_RE.finditer(text))
    return round(tagged / len(text), 3)


def _noise_hit(text: str) -> str:
    """返回命中的第一个错误页特征词，未命中返回空串。"""
    for marker in CONTENT_NOISE_MARKERS:
        if marker in text:
            return marker
    return ""


def build_evidence(values: Sequence[str], matched_html: str = "") -> Dict[str, Any]:
    """构造结构化证据。``chars`` / ``cjk_chars`` 只作展示，**不参与判定**。"""
    clean = [str(v or "") for v in (values or [])]
    joined = "".join(clean)
    return {
        "values_total": len([v for v in clean if v.strip()]),
        "chars": len(joined),
        # 用 finditer 计数而不是 findall：findall 会为每个中文字符实体化一个
        # 字符串对象。150 万字符实测：峰值内存 findall 约 100MB、finditer 约 0MB。
        # 本改动针对的是**内存**，不是速度——耗时差异随机器与测法浮动
        # （同一段代码在不同机器上实测到过 1.0x 与 1.5x 两种结果），不要把它当性能优化引用。
        # 正文全文不截断（MAX_VALUE_CHARS = 0），这个量级会真实出现，
        # 而这个字段只用于统计展示——不值得为它瞬时吃上百 MB
        "cjk_chars": sum(1 for _ in _CJK_RE.finditer(joined)),
        # 段落信息只能从命中节点的 HTML 拿：replayer 的 text 动作会 re.sub(r"\s+", " ")
        # 把换行全抹掉，提取值里已经没有段落信息了
        "block_seps": len(_BLOCK_SEP_RE.findall(matched_html or "")),
        "tag_ratio": _tag_ratio(joined),
        # 噪声词只在短内容上采信，避免长正文里的偶然命中污染证据
        "noise_hit": _noise_hit(joined) if len(joined) < SHORT_CONTENT_CHARS else "",
    }


# ------------------------------------------------------------------ 证据页面登记

def new_page(pages: Dict[str, Dict[str, Any]], page_id: str, url: str, html: str,
             status: int = 200, charset: str = "") -> str:
    """把抓到的页面登记进 ``pages``（按 id 去重），返回实际可用的 page_id。

    原先这是 ``core/verify.py`` 的私有函数 ``_new_page``。提到这里是因为
    **「试跑」与「连 App 调试」两个调用方都要产出同形状的 ``pages[]``**，
    而 ``html`` 的截断口径（``MAX_PAGE_HTML_CHARS``）本来就定义在本模块——
    口径定义在这儿、实现却抄到第二个调用方去，正是本次改造反复在消灭的
    「同一件事写两处」。

    行为与原实现逐字一致，未做任何改动：
      - ``html`` 为空 → 返回 ``""``（不登记空页；调用方据此知道这步没有页面）
      - ``page_id`` 已存在 → 返回该 id，**保留先登记的那份**（搜索页与详情页
        可能是同一个 URL 但语义不同，先到先得）
      - ``truncated`` 按原始长度判定，``html`` 只存前 ``MAX_PAGE_HTML_CHARS`` 个字符
    """
    if not html:
        return ""
    if page_id in pages:
        return page_id
    truncated = len(html) > MAX_PAGE_HTML_CHARS
    pages[page_id] = {
        "id": page_id,
        "url": url,
        "status": status,
        "charset": charset,
        "html": html[:MAX_PAGE_HTML_CHARS],
        "len": len(html),
        "truncated": truncated,
    }
    return page_id


def _shape_label(shape: str) -> str:
    return {
        SHAPE_TEXT: "文本", SHAPE_IMAGE: "图片", SHAPE_AUDIO: "音频",
        SHAPE_MIXED: "多种形态混合", SHAPE_EMPTY: "空",
    }.get(shape, shape)


def safe_int(value: Any, default: int = 0) -> int:
    """宽松取整。脏值（"" / [] / "abc" / None）一律降级为默认值。

    书源的 bookSourceType 是从外部 JSON 来的，历史上就出现过 ''/[]/字符串数字
    这类脏值。core/sanitize.py 的 int 字段清单只覆盖 concurrentRate /
    customOrder / respondTime / weight / lastUpdateTime，**不含 bookSourceType**，
    所以本函数是该字段的唯一防线。本模块是全部源的共用闸门：抛异常会中断整批
    校验，而降级为 0 最坏只是判定口径偏保守——按「不误杀」的立场，后者才对。

    OverflowError 也要接住：json.loads('{"bookSourceType": 1e400}') 会解析出
    inf，int(inf) 抛的正是 OverflowError，不接就会穿透这道闸门。
    """
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


# ------------------------------------------------------------------ 正文判定

def _content_notes(source_type: int, shape: str, values: Sequence[str],
                   evidence: Dict[str, Any]) -> List[str]:
    """启发式附注。**只写 notes，绝不改 verdict。**"""
    notes: List[str] = []
    joined = "".join(str(v or "") for v in values)

    if STRUCT_TAG_RE.search(joined):
        notes.append("提取值含结构性 HTML 标签（<div> 等），疑似选到容器而非正文；"
                     "建议改用 @text，或加 ##<[^>]+>## 清洗")

    if evidence.get("chars", 0) < SHORT_CONTENT_CHARS:
        notes.append("正文较短（%d 字符），建议看一眼全文确认不是错误页"
                     % evidence.get("chars", 0))
        hit = evidence.get("noise_hit") or ""
        if hit:
            notes.append("疑似错误页：命中「%s」" % hit)

    expected = EXPECTED_SHAPE.get(int(source_type or 0))
    if expected and shape not in (expected, SHAPE_EMPTY, SHAPE_MIXED):
        notes.append("声明为%s，但提取结果像%s，建议核对 bookSourceType"
                     % (BOOK_SOURCE_TYPE_NAMES.get(int(source_type or 0), source_type),
                        _shape_label(shape)))
    return notes


def judge_content(
    source_type: int,
    values: Sequence[str],
    rule: str,
    matched_html: str = "",
    rule_error: str = "",
) -> Judgement:
    """正文判定。前置分流顺序**不可调换**。"""
    st = safe_int(source_type)
    clean = [str(v or "") for v in (values or [])]
    shape, _counts = sniff_shape(clean)
    evidence = build_evidence(clean, matched_html)

    # 1) 下载源不解析正文。Legado 用 book.isWebFile 判定并跳过
    #    （Debug.kt:329-332、BookSourceCheckRepository.kt:221）；本项目无该字段，
    #    退化为按 bookSourceType == 3 判断
    if st == 3:
        return Judgement(VERDICT_UNKNOWN, "文件类书源，不解析正文", shape, [], evidence)

    # 2) 规则为空 —— Legado 不报错，而是把章节链接本身当正文返回
    #    （WebBook.kt:400-403），后果按类型不同
    if not str(rule or "").strip():
        if st == 0:
            return Judgement(
                VERDICT_FAIL,
                "正文规则为空；Legado 会把章节链接当作正文，无法阅读",
                shape, [], evidence)
        if st in (1, 2):
            return Judgement(
                VERDICT_PASS,
                "正文规则为空；Legado 回退为使用章节链接本身，这对音频/图片源是正常配置",
                shape, [], evidence)
        return Judgement(
            VERDICT_UNKNOWN,
            "正文规则为空，且类型未知，无法判断是否符合预期",
            shape, [], evidence)

    # 3) 规则回放不了 —— 是工具的能力边界，不是源坏了
    if str(rule_error or "").strip():
        return Judgement(VERDICT_UNKNOWN, str(rule_error), shape, [], evidence)

    # 4) 提取为空 —— 对应 Legado 的 ContentEmptyException（BookContent.kt:203-205）
    if evidence["values_total"] == 0:
        return Judgement(VERDICT_FAIL, "正文提取为空", shape, [], evidence)

    # 5) 非空即通过。附注不妨碍这一点
    return Judgement(VERDICT_PASS, "", shape,
                     _content_notes(st, shape, clean, evidence), evidence)


# ------------------------------------------------------------------ 列表步骤判定

def judge_list_step(
    step: str,
    values: Sequence[str],
    matched_html: str = "",
    rule_error: str = "",
    source_type: int = 0,
    rule: str = "",
) -> Judgement:
    """目录 / 搜索结果列表判定（search / bookUrl / toc 三步共用）。

    ``rule`` 用来区分「规则为空」与「规则回放不了」——这两件事性质完全不同：
      - **规则为空**是**源的配置错误**（Legado 也解析不出东西）→ `fail`
      - **规则回放不了**是**我们的能力边界**（JS / 模板 / XPath）→ `unknown`

    压进同一个通道过（`_extract` 曾对空规则返回 ``"空规则"`` 哨兵），后果是
    `bookList` 为空的源被判成 unknown → `all_ok=True` → `core/repair/loop.py`
    把它当成「已经修好了」，AI 修复循环永远不会碰它。
    """
    # 入口归一化：只有精确等于 STEP_TOC 才启用 toc 语义，
    # 而调用方一个大写笔误（"TOC"）就会把本该 unknown 的结果变成 fail
    step_key = str(step or "").strip().lower()
    clean = [str(v or "") for v in (values or [])]
    shape, _counts = sniff_shape(clean)
    evidence = build_evidence(clean, matched_html)

    # 下载源不解析目录（Debug.kt:329-332）——豁免排在空规则判定之前
    if step_key == STEP_TOC and safe_int(source_type) == 3:
        return Judgement(VERDICT_UNKNOWN, "文件类书源不解析目录", shape, [], evidence)

    # 规则为空：与 judge_content 的空规则分支对称，同样是**源的配置错误**
    if not str(rule or "").strip():
        return Judgement(VERDICT_FAIL, "列表规则为空，Legado 无法解析", shape, [], evidence)

    # 规则回放不了：是工具的能力边界，不是源坏了
    if str(rule_error or "").strip():
        return Judgement(VERDICT_UNKNOWN, str(rule_error), shape, [], evidence)

    # 解析为空 —— 对应 TocEmptyException（BookSourceCheckRepository.kt:236）
    if evidence["values_total"] == 0:
        return Judgement(VERDICT_FAIL, "解析结果为空", shape, [], evidence)

    notes: List[str] = []
    if step_key == STEP_TOC and evidence["values_total"] < 3:
        notes.append("章节数偏少（%d），目录可能分页加载" % evidence["values_total"])
    return Judgement(VERDICT_PASS, "", shape, notes, evidence)


# ------------------------------------------------------------------ 静态错配检查

#: 可能是 URL 规则的字段（Legado 的 webView 是 URL 规则的选项，不是 ContentRule 字段）
_URL_RULE_KEYS = ("searchUrl", "exploreUrl")

_WEBVIEW_ON_RE = re.compile(r'"?webView"?\s*:\s*(?:true|"true"|1)', re.I)


def _any_url_rule_uses_webview(source: Dict[str, Any]) -> bool:
    """宽松检测 URL 规则里是否开了 webView（形如 ``url,{"webView":true}``）。"""
    for key in _URL_RULE_KEYS:
        if _WEBVIEW_ON_RE.search(str((source or {}).get(key, "") or "")):
            return True
    toc_url = str(((source or {}).get("ruleBookInfo") or {}).get("tocUrl", "") or "")
    return bool(_WEBVIEW_ON_RE.search(toc_url))


def static_misconfig_notes(source: Dict[str, Any]) -> List[str]:
    """书源级静态错配检查（与抓取结果无关）。

    这些错配**靠「看 HTML 源码」是发现不了的**，只有读 Legado 源码才知道：
      - ruleContent.webJs 非空，但 URL 规则未开 webView → webJs 不生效
        （AnalyzeUrl.kt:441 的 ``if (this.useWebView && useWebView)``，
         以及 :457,467 的 ``javaScript = webJs ?: jsStr``）
      - bookSourceType == 4 → Legado 无此取值
        （BookSourceType.kt:8-11 的 @IntDef 只有 0~3）
    """
    src = source or {}
    notes: List[str] = []

    content = src.get("ruleContent") or {}
    if str(content.get("webJs", "") or "").strip() and not _any_url_rule_uses_webview(src):
        notes.append("ruleContent.webJs 已配置，但 URL 规则未开启 webView，"
                     "该段 JS 在 Legado 中不会生效（AnalyzeUrl.kt:441）")

    if safe_int(src.get("bookSourceType", 0)) == 4:
        notes.append("bookSourceType=4 是 Legado 不存在的取值，导出后行为未定义，"
                     "建议改为 0~3")

    return notes


__all__ = [
    "Judgement", "VERDICT_PASS", "VERDICT_FAIL", "VERDICT_UNKNOWN",
    "SHAPE_TEXT", "SHAPE_IMAGE", "SHAPE_AUDIO", "SHAPE_MIXED", "SHAPE_EMPTY",
    "STEP_SEARCH", "STEP_BOOK_URL", "STEP_TOC",
    "sniff_shape", "build_evidence", "judge_content", "judge_list_step",
    "static_misconfig_notes", "new_page",
    "EXPECTED_SHAPE", "STRUCT_TAG_RE", "CONTENT_NOISE_MARKERS",
    "MAX_PAGE_HTML_CHARS", "MAX_MATCHED_HTML_CHARS", "MATCHED_NODES_LIMIT",
    "MAX_VALUE_CHARS", "VALUES_PREVIEW_LIMIT", "MAX_EVIDENCE_TOTAL_CHARS",
    "SHORT_CONTENT_CHARS", "safe_int",
]
