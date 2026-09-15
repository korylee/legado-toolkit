# 试跑调试与编辑弹窗加固 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让「全链路试跑」摊开每一步的原始证据（正文全文、命中片段、整页源码）并把判定口径对齐「阅读」App 的调试语义；同时修掉编辑弹窗里会静默丢数据的六处交互缺陷。

**Architecture:** 新增 `core/quality.py` 作为唯一的判定模块，「试跑」(`core/verify.py`) 与「批量校验」(`core/checker.py`) 共用它，消除「同源不同判」。判定底线是「提取值非空 / 不报错」，启发式只产出 `notes` 附注、绝不改变 `verdict`。前端新增 `RuleDebugDrawer.vue` 承载证据，`SourceEditDialog.vue` 改为三态渲染并接入抽屉。

**Tech Stack:** Python 3 / FastAPI / aiohttp / BeautifulSoup（后端），Vue 3 + Element Plus（前端）。测试用标准库 `unittest`（**仓库没有 pytest**）。

---

## 开始前必读

**设计依据**：`docs/superpowers/specs/2026-09-15-rule-debug-and-dialog-hardening-design.md`。本计划是它的实现，**spec 里的 `文件:行号` 引用是行为依据，不要改它们**（它们指向的是外部参照项目 Legado 的源码）。

**跑测试的命令**（仓库根目录，Git Bash）：

```bash
.venv/Scripts/python.exe -m unittest tests.test_legado_rules -v
```

**关键约定**：

1. 仓库**没有 pytest**，所有测试用 `unittest.TestCase`，文件放 `tests/test_*.py`。
2. 代码注释用**中文**，关键逻辑节点必须加注释。
3. **不要引入新依赖**。
4. 提交粒度：每个 Task 一次提交。
5. 当前分支是 `master`，历史上直接提交在 `master`，跟随即可。

---

## 文件结构

### 新增

| 文件 | 职责 |
|---|---|
| `core/quality.py` | 唯一的判定模块：形态嗅探、三态判定、启发式附注、静态错配检查、截断常量 |
| `tests/test_quality.py` | 判定矩阵单测 |
| `tests/test_verify_chain.py` | 证据结构与兼容性单测 |
| `frontend/src/components/RuleDebugDrawer.vue` | 调试抽屉：提取结果 / 命中源码 / 整页源码 |

### 修改

| 文件 | 改什么 |
|---|---|
| `core/rules/replayer.py` | 新增 `extract_all_nodes()`；`parse_rule` 补齐不支持语法检测 |
| `core/fetch.py` | `fetch()` 支持 headers / charset / proxy；新增 `parse_source_header()` |
| `core/verify.py` | 证据采集；判定改调 `quality`；`tocUrl` 分支；`file` 型分支；`proxy` 透传 |
| `core/checker.py` | `_probe_content` / `_probe_toc` 改调 `quality`；删 `ruleContent.image` 死分支；`CACHE_VERSION` 5 → 6 |
| `backend/api/ops.py` | 入 job 结果前剥离 `pages` / `matched_html` / `values` |
| `backend/api/sources.py` | 新增 `GET /api/sources/exists` |
| `frontend/src/api/sources.js` | 新增 `sourceExists()` |
| `frontend/src/components/SourceEditDialog.vue` | 三态渲染 + 结果过期 + 抽屉接入 + P0/P1 六项 |
| `frontend/src/styles.css` | 抽屉与圆点样式 |

### 不动

`core/reclassify.py`（类型判定改造整体推迟）、`backend/schemas.py`、`core/repair/loop.py`、`services/add_source.py`。

---

## 与 spec 的两处有意偏离

写代码时按**本计划**来，这两处是我在细化时发现 spec 的写法不够好：

1. **`judge_content` 去掉 `source` 参数**。spec §6.1 的签名是 `judge_content(source_type, source, values, rule, ...)`——那是为原设计的「空规则时看 `ruleContent.webView`」分支留的。该分支在减法中已改为**按类型分派**（spec §6.3 前置分流第 2 条），`source` 参数不再被使用。**别传一个用不上的参数。**
2. **`judge_list_step` 增加 `source_type` 参数**。spec §6.4 要求 `toc` 对 `bookSourceType == 3` 返回 `unknown`，这个判断需要类型信息，而 spec §6.1 的签名里没有。签名改为 `judge_list_step(step, values, matched_html="", rule_error="", source_type=0)`。

---

# 阶段 A：后端判定与证据

## Task 1: `core/quality.py` —— 判定模块（含全部常量）

这是整个改造的地基：**判定只剩下「非空 / 不报错」这一条底线**，所有启发式只写 `notes`。

**Files:**
- Create: `core/quality.py`
- Test: `tests/test_quality.py`

- [ ] **Step 1: 先写测试**

创建 `tests/test_quality.py`：

```python
# -*- coding: utf-8 -*-
"""判定模块单元测试。

重点守住一条底线：**没有任何一条基于长度的 fail**。
Legado 的调试只判 contentStr.isBlank()（BookContent.kt:203-205），
自创长度阈值会误杀短章节。
"""

import unittest

from core import quality as Q


def content_judge(source_type, values, rule="id.content@text",
                  matched_html="", rule_error=""):
    return Q.judge_content(source_type, values, rule, matched_html, rule_error)


class SniffShapeTests(unittest.TestCase):
    def test_text(self):
        shape, _ = Q.sniff_shape(["第一章 正文内容"])
        self.assertEqual(shape, Q.SHAPE_TEXT)

    def test_image_by_ext_and_tag(self):
        self.assertEqual(Q.sniff_shape(["https://a.com/1.jpg"])[0], Q.SHAPE_IMAGE)
        self.assertEqual(Q.sniff_shape(['<img src="/1.png">'])[0], Q.SHAPE_IMAGE)

    def test_audio(self):
        self.assertEqual(Q.sniff_shape(["https://a.com/1.mp3"])[0], Q.SHAPE_AUDIO)

    def test_empty_and_mixed(self):
        self.assertEqual(Q.sniff_shape([])[0], Q.SHAPE_EMPTY)
        self.assertEqual(Q.sniff_shape(["  ", ""])[0], Q.SHAPE_EMPTY)
        self.assertEqual(Q.sniff_shape(["https://a.com/1.jpg", "正文"])[0], Q.SHAPE_MIXED)


class ContentVerdictTests(unittest.TestCase):
    def test_nonempty_is_pass_even_if_very_short(self):
        """回归防线：20 字的合法短正文必须是 pass，不是 fail。"""
        j = content_judge(0, ["短正文。"])
        self.assertEqual(j.verdict, Q.VERDICT_PASS)

    def test_empty_is_fail(self):
        j = content_judge(0, [])
        self.assertEqual(j.verdict, Q.VERDICT_FAIL)
        self.assertIn("空", j.reason)

    def test_blank_only_is_fail(self):
        j = content_judge(0, ["  ", "\n"])
        self.assertEqual(j.verdict, Q.VERDICT_FAIL)


class ContentSourceTypeTests(unittest.TestCase):
    def test_text_source_empty_rule_is_fail(self):
        j = content_judge(0, [], rule="")
        self.assertEqual(j.verdict, Q.VERDICT_FAIL)
        self.assertIn("章节链接", j.reason)

    def test_audio_source_empty_rule_is_pass(self):
        j = content_judge(1, [], rule="")
        self.assertEqual(j.verdict, Q.VERDICT_PASS)
        self.assertIn("章节链接", j.reason)

    def test_image_source_empty_rule_is_pass(self):
        j = content_judge(2, [], rule="")
        self.assertEqual(j.verdict, Q.VERDICT_PASS)

    def test_unknown_type_empty_rule_is_unknown(self):
        j = content_judge(4, [], rule="")
        self.assertEqual(j.verdict, Q.VERDICT_UNKNOWN)

    def test_download_source_is_unknown(self):
        j = content_judge(3, ["x"])
        self.assertEqual(j.verdict, Q.VERDICT_UNKNOWN)
        self.assertIn("文件类", j.reason)

    def test_rule_error_is_unknown(self):
        j = content_judge(0, ["x"], rule_error="JS 规则（@js:/<js>）需要 Legado 的 Rhino 引擎，无法离线回放")
        self.assertEqual(j.verdict, Q.VERDICT_UNKNOWN)
        self.assertIn("Rhino", j.reason)


class NoteTests(unittest.TestCase):
    """启发式只能产出 notes，不能改变 verdict。"""

    def test_struct_tag_is_note_not_fail(self):
        j = content_judge(0, ['<div class="content">正文</div>'])
        self.assertEqual(j.verdict, Q.VERDICT_PASS)
        self.assertTrue(j.has_notes)
        self.assertTrue(any("结构性" in n for n in j.notes))

    def test_noise_word_is_note_not_fail(self):
        j = content_judge(0, ["页面不存在"])
        self.assertEqual(j.verdict, Q.VERDICT_PASS)
        self.assertTrue(any("疑似错误页" in n for n in j.notes))

    def test_noise_word_ignored_when_content_long(self):
        long_text = "正文" * 400 + "页面不存在"
        j = content_judge(0, [long_text])
        self.assertFalse(any("疑似错误页" in n for n in j.notes))

    def test_type_mismatch_note(self):
        j = content_judge(0, ["https://a.com/1.jpg", "https://a.com/2.jpg"])
        self.assertEqual(j.verdict, Q.VERDICT_PASS)
        self.assertTrue(any("bookSourceType" in n for n in j.notes))


class ListStepTests(unittest.TestCase):
    def test_toc_empty_is_fail(self):
        j = Q.judge_list_step("toc", [])
        self.assertEqual(j.verdict, Q.VERDICT_FAIL)

    def test_toc_few_chapters_is_pass_with_note(self):
        j = Q.judge_list_step("toc", ["/c/1", "/c/2"])
        self.assertEqual(j.verdict, Q.VERDICT_PASS)
        self.assertTrue(any("章节数偏少" in n for n in j.notes))

    def test_toc_download_source_is_unknown(self):
        j = Q.judge_list_step("toc", [], source_type=3)
        self.assertEqual(j.verdict, Q.VERDICT_UNKNOWN)

    def test_search_empty_is_fail(self):
        j = Q.judge_list_step("search", [])
        self.assertEqual(j.verdict, Q.VERDICT_FAIL)


class StaticMisconfigTests(unittest.TestCase):
    def test_webjs_without_webview(self):
        notes = Q.static_misconfig_notes({
            "searchUrl": "https://a.com/s?q={{key}}",
            "ruleContent": {"webJs": "return 1"},
        })
        self.assertTrue(any("webJs" in n for n in notes))

    def test_webjs_with_webview_is_clean(self):
        notes = Q.static_misconfig_notes({
            "searchUrl": 'https://a.com/s?q={{key}},{"webView":true}',
            "ruleContent": {"webJs": "return 1"},
        })
        self.assertEqual([n for n in notes if "webJs" in n], [])

    def test_type_four_warns(self):
        notes = Q.static_misconfig_notes({"bookSourceType": 4})
        self.assertTrue(any("bookSourceType" in n for n in notes))


class CheckerStateMapTests(unittest.TestCase):
    def test_mapping(self):
        self.assertIs(Q.Judgement(Q.VERDICT_PASS).checker_state, True)
        self.assertIs(Q.Judgement(Q.VERDICT_FAIL).checker_state, False)
        self.assertIsNone(Q.Judgement(Q.VERDICT_UNKNOWN).checker_state)

    def test_ok_only_fail_is_false(self):
        self.assertTrue(Q.Judgement(Q.VERDICT_PASS).ok)
        self.assertTrue(Q.Judgement(Q.VERDICT_UNKNOWN).ok)
        self.assertFalse(Q.Judgement(Q.VERDICT_FAIL).ok)


if __name__ == "__main__":
    unittest.main()
```

> 注意 `content_judge` 这个 helper 把 `Q.judge_content` 的 `source_type, values, rule` 三个参数固定了顺序，让用例读起来短一些。下面每个类的用例都是完整的，直接照抄即可。

- [ ] **Step 2: 跑测试，确认失败**

```bash
.venv/Scripts/python.exe -m unittest tests.test_quality -v
```

Expected: `ModuleNotFoundError: No module named 'core.quality'`

- [ ] **Step 3: 写 `core/quality.py`**

创建 `core/quality.py`：

```python
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

    逐条分类后取占比最高者；最高占比未过半（<= 0.5）→ mixed；全空 → empty。

    注意是 ``<=`` 而不是 ``<``：1:1 平局（如一张图 + 一段文字）占比正好 0.5，
    此时没有主导形态，应当算 mixed。语义即「占多数才算单一形态」。
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


def _shape_label(shape: str) -> str:
    return {
        SHAPE_TEXT: "文本", SHAPE_IMAGE: "图片", SHAPE_AUDIO: "音频",
        SHAPE_MIXED: "多种形态混合", SHAPE_EMPTY: "空",
    }.get(shape, shape)


def _safe_int(value: Any, default: int = 0) -> int:
    """宽松取整。脏值（"" / [] / "abc" / None）一律降级为默认值。

    书源的 bookSourceType 是从外部 JSON 来的，历史上就出现过 ''/[]/字符串数字
    这类脏值（见 core/sanitize.py 的说明）。本模块是全部源的共用闸门：
    抛异常会中断整批校验，而降级为 0 最坏只是判定口径偏保守——按「不误杀」的
    立场，后者才对。
    """
    try:
        return int(value)
    except (TypeError, ValueError):
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
    st = _safe_int(source_type)
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
) -> Judgement:
    """目录 / 搜索结果列表判定（search / bookUrl / toc 三步共用）。"""
    # 入口归一化：只有精确等于 STEP_TOC 才启用 toc 语义，
    # 而调用方一个大写笔误（"TOC"）就会把本该 unknown 的结果变成 fail
    step_key = str(step or "").strip().lower()
    clean = [str(v or "") for v in (values or [])]
    shape, _counts = sniff_shape(clean)
    evidence = build_evidence(clean, matched_html)

    # 下载源不解析目录（Debug.kt:329-332）
    if step_key == STEP_TOC and _safe_int(source_type) == 3:
        return Judgement(VERDICT_UNKNOWN, "文件类书源不解析目录", shape, [], evidence)

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

    if _safe_int(src.get("bookSourceType", 0)) == 4:
        notes.append("bookSourceType=4 是 Legado 不存在的取值，导出后行为未定义，"
                     "建议改为 0~3")

    return notes


__all__ = [
    "Judgement", "VERDICT_PASS", "VERDICT_FAIL", "VERDICT_UNKNOWN",
    "SHAPE_TEXT", "SHAPE_IMAGE", "SHAPE_AUDIO", "SHAPE_MIXED", "SHAPE_EMPTY",
    "STEP_SEARCH", "STEP_BOOK_URL", "STEP_TOC",
    "sniff_shape", "build_evidence", "judge_content", "judge_list_step",
    "static_misconfig_notes",
    "EXPECTED_SHAPE", "STRUCT_TAG_RE", "CONTENT_NOISE_MARKERS",
    "MAX_PAGE_HTML_CHARS", "MAX_MATCHED_HTML_CHARS", "MATCHED_NODES_LIMIT",
    "MAX_VALUE_CHARS", "VALUES_PREVIEW_LIMIT", "MAX_EVIDENCE_TOTAL_CHARS",
    "SHORT_CONTENT_CHARS",
]
```

- [ ] **Step 4: 跑测试，确认通过**

```bash
.venv/Scripts/python.exe -m unittest tests.test_quality -v
```

Expected: `OK`，全部用例通过。

- [ ] **Step 5: 提交**

```bash
git add core/quality.py tests/test_quality.py
git commit -m "feat(quality): 新增三态判定模块，底线对齐 Legado 调试语义"
```

---

## Task 2: `replayer.extract_all_nodes()` —— 拿到命中片段

「改规则时你看的是现在选到了哪块 DOM，而不是 50 万字符的整页」——这是本次对改源效率提升最大的一件事。

**关键**：`_walk()` 内部已经算出了命中节点，只是 `extract_all_ex()` 丢弃了。新增薄封装，**零现有调用点改动**。

**Files:**
- Modify: `core/rules/replayer.py:420-445`（`_walk` 附近）
- Test: `tests/test_legado_rules.py`（追加测试类）

- [ ] **Step 1: 写失败测试**

在 `tests/test_legado_rules.py` 末尾（`if __name__ == "__main__":` 之前）追加：

```python
class ExtractAllNodesTests(unittest.TestCase):
    """命中片段：规则选中的 DOM 块的 outerHTML。"""

    #: 测试固定的证据预算。真实口径归 core.quality 所有
    #: （MATCHED_NODES_LIMIT / MAX_MATCHED_HTML_CHARS），replayer 不设默认值
    LIMIT = 3
    MAX_CHARS = 200_000

    def _nodes(self, content, rule, **kw):
        kw.setdefault("limit", self.LIMIT)
        kw.setdefault("max_chars", self.MAX_CHARS)
        return R.extract_all_nodes(content, rule, **kw)

    def test_content_rule_returns_matched_block(self):
        vals, hits, err = self._nodes(HTML, "class.book-list@tag.li@tag.a@text")
        self.assertEqual(err, "")
        self.assertEqual(vals, ["测试书", "第二本"])
        # 必须**逐字**断言：命中的是属性取值动作**之前**的那个 <a>。
        # 用 assertIn("/book/1", hits[0]) 是不够的——父节点 <li> 的 outerHTML
        # 同样含 /book/1，实现若错选到祖先，那种弱断言照样通过，
        # 而「选到哪一块」正是本次改动的全部价值。
        self.assertEqual(hits[0], '<a href="/book/1">测试书</a>')
        self.assertEqual(hits[1], '<a href="/book/2">第二本</a>')

    def test_hits_are_capped(self):
        _vals, hits, _err = self._nodes(HTML, "class.book-list@tag.li", limit=1)
        self.assertEqual(len(hits), 1)

    def test_hits_truncated_by_max_chars(self):
        _vals, hits, _err = self._nodes(HTML, "class.book-list@tag.li", max_chars=10)
        # 先钉住条数：若实现返回空列表，下面的循环会变成空转的假覆盖
        self.assertEqual(len(hits), 2)
        for h in hits:
            self.assertEqual(len(h), 10)
            self.assertTrue(h.startswith("<li class="))

    def test_json_leaf_hits_equal_values(self):
        """JSON 字符串叶子下 hits 与 values 相同——调用方需自行去重。

        这是已知语义：字符串叶子经 _json_to_text 原样返回，UI 上会出现两份
        一样的内容。补这条用例把该行为固定下来，避免日后被当成 bug 修。
        """
        vals, hits, err = self._nodes(JSONTEXT, "$.data.list[*].name")
        self.assertEqual(err, "")
        self.assertEqual(vals, ["A", "B"])
        self.assertEqual(hits, vals)

    def test_json_object_hit_is_serialized(self):
        """选中 dict 节点时 hits 是该节点的 JSON 串，不是空。"""
        _vals, hits, err = self._nodes(JSONTEXT, "$.data")
        self.assertEqual(err, "")
        self.assertEqual(len(hits), 1)
        self.assertIn("list", hits[0])

    def test_unsupported_rule_returns_reason(self):
        _vals, hits, err = self._nodes(HTML, "@js:result")
        self.assertTrue(err)
        self.assertEqual(hits, [])

    def test_empty_rule(self):
        _vals, hits, err = self._nodes(HTML, "")
        self.assertTrue(err)
        self.assertEqual(hits, [])

    def test_html_rule_returns_raw_response(self):
        """@html: 分支不做任何选择，整份响应体就是命中内容。

        该分支此前无任何测试保护（变异测试证实：把它改成永假，原有测试全绿）。
        """
        vals, hits, err = self._nodes(HTML, "@html:")
        self.assertEqual(err, "")
        self.assertEqual(vals, [HTML])
        self.assertEqual(hits, [HTML])
```

> ⚠️ `test_content_rule_returns_matched_block` 里的两条**逐字**断言（`'<a href="/book/1">测试书</a>'`）是照 BeautifulSoup 的序列化结果写的。**执行时请先打印一次实际值确认**，若序列化细节（属性顺序、引号、空白）与预期不符，以真实输出为准调整断言——但**必须保持逐字断言这种强度**，不要退回 `assertIn`。

- [ ] **Step 2: 跑测试，确认失败**

```bash
.venv/Scripts/python.exe -m unittest tests.test_legado_rules.ExtractAllNodesTests -v
```

Expected: `AttributeError: module 'core.rules.replayer' has no attribute 'extract_all_nodes'`

- [ ] **Step 3: 实现**

在 `core/rules/replayer.py` 的 `_walk()` 之后、`_convert_replacement()` 之前插入（约第 446 行）：

```python
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
```

然后把现有的 `_walk()` 改成它的薄封装（**这样所有现有调用点一行都不用改**）：

```python
def _walk(
    start_nodes: Sequence[Any],
    steps: Sequence[Tuple[str, str]],
    kind: str,
):
    """执行步骤链。返回 ``(nodes, values, error)``。见 ``_walk_hits``。"""
    nodes, values, err, _hits = _walk_hits(start_nodes, steps, kind)
    return nodes, values, err
```

> **注意**：上面的 `_walk` 是**替换**已有的同名函数，不是新增。原函数体（`nodes: List[Any] = list(start_nodes)` 到 `return nodes, None, ""`）整体删掉。

然后在 `extract_all_ex()` 之后新增对外 API：

```python
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

    hits: List[str] = []
    for node in hit_nodes[:max(0, limit)]:
        html = str(node) if _is_tag(node) else _json_to_text(node)
        hits.append(html[:max_chars] if max_chars > 0 else html)
    return values, hits, ""
```

最后把 `extract_all_nodes` 加进文件末尾的 `__all__`：

```python
    "extract_all", "extract_all_ex", "extract_all_nodes", "extract_first",
```

- [ ] **Step 4: 跑测试（新旧一起）**

```bash
.venv/Scripts/python.exe -m unittest tests.test_legado_rules -v
```

Expected: `OK`。**关键**：原有的 `ShorthandTests` / `RegexTests` 等必须**全部照旧通过**——这次改动是薄封装，不该影响任何既有行为。

- [ ] **Step 5: 提交**

```bash
git add core/rules/replayer.py tests/test_legado_rules.py
git commit -m "feat(replayer): 新增 extract_all_nodes 返回命中节点的 HTML 片段"
```

---

## Task 3: `replayer.parse_rule` —— 补齐不支持语法检测

**这条修的是一个会让试跑误报 `fail` 的路径**：Legado 支持但本项目回放不了的语法，现在会被静默当成 CSS 选择器跑出空结果 → 试跑报「解析为空」（红），而真相是「工具测不了」。

**Files:**
- Modify: `core/rules/replayer.py:246-258`（`parse_rule` 的不支持检测段）
- Test: `tests/test_legado_rules.py`（追加）

- [ ] **Step 1: 写失败测试**

在 `tests/test_legado_rules.py` 的 `UnsupportedTests` 类里追加方法：

```python
    def test_legado_only_syntax_reported(self):
        """Legado 支持、我们回放不了的语法，必须报 unsupported 而不是静默跑空。"""
        rules = [
            "@@class.a@text",              # 强制 jsoup
            "@webjs:return document.body",  # 注入 WebView
            "class.a@text&&class.b@text",   # && 合并
            "class.a@text%%class.b@text",   # %% 按索引交替
            "tag.div[2:5]",                 # 区间索引
            "tag.div[0:10:2]",              # 区间索引（带步长）
            "$.data.list$1",                # $n 取列表第 n 项
            "id.content@text##广告##x###",   # ## 第四段（只替换第一个）
            "class.a@text@get:{name}",      # 变量读取
        ]
        for rule in rules:
            ok, why = R.rule_supported(rule)
            self.assertFalse(ok, "应判为不支持：%s" % rule)
            self.assertTrue(why, "必须给出原因：%s" % rule)

    def test_supported_syntax_not_affected(self):
        """反向断言：新检测不能误伤本来能跑通的规则。"""
        for rule in ("class.a@tag.b@text", "class.item@href", "@css:class.a@text",
                     "$.data.list[*].name", "id.content@text##广告##",
                     "id.content@text##广告##替换",
                     "class.a@text", "text", "class.list@tag.li"):
            ok, why = R.rule_supported(rule)
            self.assertTrue(ok, "被误伤：%s (%s)" % (rule, why))

    def test_js_in_middle_reports_js_reason(self):
        """`selector@js:code` 的 JS 体里出现 $1/&&/@get: 时，报的原因必须是 JS。

        实测真实语料里，错误原因从 589 条（去重）/ 5031 条（含重复）降为 0。结论（unknown）本来就对，
        但如果 JS 检测排在新检测段后面，报出的原因会变成「$n 取列表第 n 项
        暂未支持」——把用户引向错误的方向，而这个工具的全部价值就是告诉他
        为什么。断言必须检查**原因内容**，否则这类错报不会被任何用例发现。
        """
        rules = [
            r"text@js:result.replace(/^(\d+)章/,'第$1章')",
            "id.c@text@js:return result + '&&'",
            "id.c@text@js:java.get('x')@get:{y}",
        ]
        for rule in rules:
            ok, why = R.rule_supported(rule)
            self.assertFalse(ok, rule)
            self.assertIn("JS", why,
                          "原因必须指向 JS 而不是别的 token：%s -> %s" % (rule, why))
```

- [ ] **Step 2: 跑测试，确认失败**

```bash
.venv/Scripts/python.exe -m unittest tests.test_legado_rules.UnsupportedTests -v
```

Expected: `test_legado_only_syntax_reported` FAIL（第一条 `@@class.a@text` 现在会被当成 CSS 选择器）

- [ ] **Step 3: 实现**

在 `core/rules/replayer.py` 的 `parse_rule()` 里，找到现有的不支持检测段（在切分步骤之前）：

```python
    # 3) 不支持的语法 -> 明确标注，避免被当成「解析为空 = 规则失效」
    bl = body.lower()
    if pr.kind == "js" or "<js" in bl or "</js>" in bl or bl.startswith("js:"):
```

在它**之前**插入新的一段（注意 `raw` 是未剥离 `##` 的原始串，检测 `$n` 与四段 `##` 要用它）：

```python
    # 2.5) Legado 支持但本项目回放不了的语法
    #      必须显式报 unsupported，否则会被静默当成 CSS 选择器跑出空结果，
    #      让试跑把「工具测不了」误判成「源坏了」（详见设计文档 7.2）
    if raw.startswith("@@"):
        pr.unsupported = "@@ 强制 jsoup 规则暂未支持"
        return pr
    if low.startswith("@webjs:"):
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
```

> **注意 `low` 的作用域**：这一段要放在 `low = body.lower()` 之后。现有的 `low` 定义在规则类型前缀剥离那段（步骤 2），而 `bl = body.lower()` 在后面。**直接把这段插在 `bl = body.lower()` 那一行的前面**，并把 `low.startswith` 改成 `bl.startswith`，避免重复定义变量。

对应的最终代码应为：

```python
    # 3) 不支持的语法 -> 明确标注，避免被当成「解析为空 = 规则失效」
    bl = body.lower()

    # 3.0) JS 检测必须排在下面那批**之前**
    #
    #  `selector@js:code` 这种形态里，JS 体内部完全可能出现 $1、&&、@get: 这些
    #  token。若先命中下面的检测，报出的原因就是错的——实测真实语料里，错误原因
    #  从 589 条（去重）/ 5031 条（含重复）降为 0
    #  源规则会因此显示「$n 取列表第 n 项暂未支持」，而真实原因是 JS 无法离线回放。
    #  结论仍是 unknown（灰），但「为什么测不了」正是调试功能的核心价值，
    #  报错原因等于把用户引向错误的方向。
    #
    #  原检测只认 `<js`/`</js`/开头 `js:`，从来不认中间形态的 `@js:`，所以
    #  `@js:` 这个条件是本任务顺带补上的（属既有的检测缺口，不是本次引入）。
    if (pr.kind == "js" or "<js" in bl or "</js>" in bl
            or bl.startswith("js:") or "@js:" in bl):
        pr.unsupported = "JS 规则（@js:/<js>）需要 Legado 的 Rhino 引擎，无法离线回放"
        return pr

    # 3.1) Legado 支持但本项目回放不了的语法（详见设计文档 7.2）
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
```

> **注意这与原计划的顺序相反**：原计划是把新检测段插在 JS 检测**之前**，实测证明那样会让数百条真实规则（去重 589）报错原因。现在 JS 检测在前，新检测段在后。
>
> **`@js:` 用精确匹配而不是裸 `"js:" in bl`**：后者会把任何含 `js:` 的选择器也判为不支持，造成「明明能跑却显示无法判定」的反向噪音。`"@js:" in bl` 加 `bl.startswith("js:")` 已覆盖 Legado 的全部 JS 书写形态。
>
> **`$n` 检测要小心**：`body` 在剥离 `##` 之后，正则 `$\d` 只在规则主体上匹配。但 `$.data.list[*].name` 这种 JSONPath 不含 `$数字`，不会被误伤。若发现误伤，把正则收紧为 `r"\$\d{1,2}$"`。

- [ ] **Step 4: 跑测试**

```bash
.venv/Scripts/python.exe -m unittest tests.test_legado_rules -v
```

Expected: `OK`。**特别确认 `test_supported_syntax_not_affected` 通过**——它守住「不误伤」这条线。

- [ ] **Step 5: 提交**

```bash
git add core/rules/replayer.py tests/test_legado_rules.py
git commit -m "fix(replayer): 补齐不支持语法检测，避免工具限制被误判为源失效"
```

---

## Task 4: `core/fetch.py` —— headers / charset / proxy

**为什么必须做**：Legado 的调试复用 `AnalyzeUrl(source = bookSource, ...)` 同一请求路径（`WebBook.kt:62-67`），header 由 `AnalyzeUrl.kt:130-141` → `BaseSource.kt:102-124` 拼装。**不带 header 的试跑抓回来的 HTML 本身就是错的**——那「看 HTML 源码改规则」就成了空中楼阁。

`proxy` 同理：`checker` 一直支持它，而 `verify` 不支持——需要代理的源在试跑里会连接失败，用户只会判「源坏了」。

**Files:**
- Modify: `core/fetch.py:6-20`
- Test: `tests/test_fetch.py`（新建）

- [ ] **Step 1: 写失败测试**

创建 `tests/test_fetch.py`：

```python
# -*- coding: utf-8 -*-
"""fetch 的请求头 / 编码 / 代理支持。"""

import unittest

from core.fetch import parse_source_header
from core import fetch as F


class ParseSourceHeaderTests(unittest.TestCase):
    def test_json_form(self):
        h, why = parse_source_header('{"User-Agent":"X","Referer":"https://a.com"}')
        self.assertEqual(h["User-Agent"], "X")
        self.assertEqual(h["Referer"], "https://a.com")
        self.assertEqual(why, "")

    def test_line_form(self):
        h, why = parse_source_header("User-Agent: X\nReferer: https://a.com")
        self.assertEqual(h["User-Agent"], "X")
        self.assertEqual(h["Referer"], "https://a.com")
        self.assertEqual(why, "")

    def test_js_header_reported_not_applied(self):
        h, why = parse_source_header('<js>return {"a":"b"}</js>')
        self.assertEqual(h, {})
        self.assertTrue(why)

    def test_empty(self):
        h, why = parse_source_header("")
        self.assertEqual(h, {})
        self.assertEqual(why, "")

    def test_garbage_does_not_raise(self):
        h, why = parse_source_header("这不是 header")
        self.assertEqual(h, {})


class FetchSignatureTests(unittest.TestCase):
    def test_accepts_new_kwargs(self):
        """只验证签名，不发真实请求。"""
        import inspect
        sig = inspect.signature(F.fetch)
        for name in ("headers", "charset", "proxy"):
            self.assertIn(name, sig.parameters)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑测试，确认失败**

```bash
.venv/Scripts/python.exe -m unittest tests.test_fetch -v
```

Expected: `ImportError: cannot import name 'parse_source_header'`

- [ ] **Step 3: 实现**

把 `core/fetch.py` 的 `fetch()` 替换为下面两个函数（保留文件里其它函数不动）：

```python
def parse_source_header(raw: str) -> tuple:
    """解析 Legado 书源的 ``header`` 字段，返回 ``(请求头, 不可用原因)``。

    Legado 支持两种写法：
      - JSON：``{"User-Agent":"...","Referer":"..."}``
      - 换行分隔：``User-Agent: xxx\\nReferer: yyy``

    含 JS（``<js`` / ``@js:``）时返回空头 + 原因。Legado 在 App 里有 Rhino 引擎
    可以执行（BaseSource.kt:102-124），我们离线做不到，所以只能标注为附注，
    **不因此判源失败**。
    """
    text = (raw or "").strip()
    if not text:
        return {}, ""
    if "<js" in text or "@js:" in text:
        return {}, "header 含 JS 规则，需要 Legado 引擎，离线无法应用"

    # JSON 写法
    if text.startswith("{"):
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                return {str(k): str(v) for k, v in obj.items() if v is not None}, ""
            return {}, "header 的 JSON 不是对象"
        except Exception:
            return {}, "header 的 JSON 解析失败"

    # 换行分隔写法
    headers = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        key, _sep, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if key and value:
            headers[key] = value
    return headers, ""


def fetch(url: str, timeout: int = 15,
          headers: dict = None, charset: str = "", proxy: str = "") -> str:
    """抓取页面 HTML。

    与 Legado 的 ``AnalyzeUrl`` 对齐的部分：
      - ``headers``：书源自身的 header；缺 User-Agent 时补默认 UA
        （对齐 BaseSource.kt 缺 UA 补 UA 的行为）
      - ``charset``：优先用它解码，失败按常见编码回退
      - ``proxy``：形如 ``http://host:port`` / ``socks5://host:port``；
        留空走直连
    """
    h = {"User-Agent": DEFAULT_UA,
         "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
         "Accept-Language": "zh-CN,zh;q=0.9"}
    if headers:
        h.update({str(k): str(v) for k, v in headers.items() if v})

    req = urllib.request.Request(url, headers=h)

    # 代理：checker 一直支持 proxy，verify 之前不支持——这会让需要代理的源
    # 在试跑里表现为「连接失败」，被用户误判成源坏了
    opener = None
    if proxy:
        handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        opener = urllib.request.build_opener(handler)

    open_fn = opener.open if opener is not None else urllib.request.urlopen
    with open_fn(req, timeout=timeout) as resp:
        raw = resp.read()

    # charset 优先，其次按常见编码回退
    order = []
    if charset:
        order.append(charset.strip().lower())
    order += ["utf-8", "gbk", "gb2312", "big5"]
    for enc in order:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")
```

> `json` 与 `urllib` 已经由 `from core.constants import *` 带进来了（`core/constants.py` 顶部 import 了它们）。若报 `NameError`，在 `core/fetch.py` 顶部补 `import json`。

- [ ] **Step 4: 跑测试 + 既有回归**

```bash
.venv/Scripts/python.exe -m unittest tests.test_fetch tests.test_legado_rules -v
```

Expected: `OK`

- [ ] **Step 5: 提交**

```bash
git add core/fetch.py tests/test_fetch.py
git commit -m "feat(fetch): 支持书源 header / charset / proxy，对齐 Legado 请求路径"
```

---

## Task 5: `core/verify.py` —— 证据采集 + 判定接入 + tocUrl / file 分支 + proxy

**Files:**
- Modify: `core/verify.py`（整体重写函数体，签名向后兼容）
- Test: `tests/test_verify_chain.py`（新建）

- [ ] **Step 1: 写失败测试**

创建 `tests/test_verify_chain.py`：

```python
# -*- coding: utf-8 -*-
"""verify_chain 的证据结构与向后兼容性。"""

import unittest
from unittest.mock import patch

from core.verify import verify_chain

SEARCH_HTML = """
<div class="book-list">
  <li class="item"><h3><a href="/book/1">测试书</a></h3></li>
  <li class="item"><h3><a href="/book/2">第二本</a></h3></li>
</div>
"""
TOC_HTML = """
<div class="chapters">
  <a href="/read/1.html">第1章</a><a href="/read/2.html">第2章</a>
  <a href="/read/3.html">第3章</a>
</div>
"""
CONTENT_HTML = '<div id="content">' + "正文内容" * 50 + "</div>"

PAGES = {
    "https://site/search?q=%E6%88%91": SEARCH_HTML,
    "https://site/book/1": TOC_HTML,
    "https://site/read/1.html": CONTENT_HTML,
}

SOURCE = {
    "bookSourceUrl": "https://site",
    "bookSourceName": "测试源",
    "bookSourceType": 0,
    "searchUrl": "https://site/search?q={{key}}",
    "ruleSearch": {"bookList": "class.item", "name": "tag.a@text",
                   "bookUrl": "tag.a@href"},
    "ruleToc": {"chapterList": "class.chapters@tag.a", "chapterName": "tag.a@text",
                "chapterUrl": "tag.a@href"},
    "ruleContent": {"content": "id.content@text"},
}


def fake_fetch(url, timeout=15, headers=None, charset="", proxy=""):
    return PAGES.get(url, "")


def run_chain(source=None, **kw):
    with patch("core.verify.fetch", side_effect=fake_fetch):
        return verify_chain(source or dict(SOURCE), "我", **kw)


class StructureTests(unittest.TestCase):
    def test_steps_have_new_fields(self):
        r = run_chain()
        self.assertTrue(r["steps"])
        for s in r["steps"]:
            for key in ("name", "ok", "verdict", "has_notes", "notes", "values",
                        "evidence", "url", "page_id"):
                self.assertIn(key, s, "%s 缺字段 %s" % (s["name"], key))

    def test_pages_deduped(self):
        """搜索页被 search 与 bookUrl 两步共用，只应出现一次。"""
        r = run_chain()
        ids = [p["id"] for p in r["pages"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertIn("search", ids)

    def test_page_truncated_flag_present(self):
        r = run_chain()
        for p in r["pages"]:
            self.assertIn("truncated", p)
            self.assertIn("len", p)

    def test_content_values_are_full_text(self):
        """对齐 BookContent.kt:194-205：正文全文要能拿到。"""
        r = run_chain()
        content = [s for s in r["steps"] if s["name"] == "content"][0]
        self.assertTrue(content["values"])
        self.assertGreater(len(content["values"][0]), 100)


class CompatibilityTests(unittest.TestCase):
    def test_ok_only_false_on_fail(self):
        r = run_chain()
        for s in r["steps"]:
            self.assertEqual(s["ok"], s["verdict"] != "fail", s["name"])

    def test_all_ok_semantics(self):
        r = run_chain()
        self.assertEqual(r["all_ok"], all(s["ok"] for s in r["steps"]))

    def test_notes_do_not_break_all_ok(self):
        """有附注的 pass 仍算通过。"""
        r = run_chain()
        for s in r["steps"]:
            if s["verdict"] == "pass":
                self.assertTrue(s["ok"])


class TocUrlBranchTests(unittest.TestCase):
    def test_toc_url_skips_detail_page(self):
        """ruleBookInfo.tocUrl 非空时跳过详情页。对齐 Debug.kt:318-322。"""
        src = dict(SOURCE)
        src["ruleBookInfo"] = {"tocUrl": "https://site/book/1"}
        src["ruleSearch"] = dict(SOURCE["ruleSearch"])
        r = run_chain(src)
        ids = [p["id"] for p in r["pages"]]
        self.assertIn("detail", ids)      # tocUrl 指向的页面仍在 pages 里
        toc = [s for s in r["steps"] if s["name"] == "toc"][0]
        self.assertEqual(toc["url"], "https://site/book/1")


class FileTypeTests(unittest.TestCase):
    def test_file_type_toc_is_unknown(self):
        src = dict(SOURCE)
        src["bookSourceType"] = 3
        r = run_chain(src)
        toc = [s for s in r["steps"] if s["name"] == "toc"][0]
        self.assertEqual(toc["verdict"], "unknown")
        self.assertTrue(toc["ok"])      # unknown 不是 fail


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑测试，确认失败**

```bash
.venv/Scripts/python.exe -m unittest tests.test_verify_chain -v
```

Expected: `KeyError: 'verdict'`（现在只有 `name` / `ok` / `detail`）

- [ ] **Step 3: 实现**

把 `core/verify.py` 整体替换为：

```python
# -*- coding: utf-8 -*-
"""由 services/add_source.py 拆分而来。

全链路试跑：search → bookUrl → toc → content，并采集每一步的证据。

设计依据见 docs/superpowers/specs/2026-09-15-rule-debug-and-dialog-hardening-design.md：
- 判定底线对齐 Legado 的调试（只判「非空 / 不报错」），见 core/quality.py
- 证据含**提取值全文**，对齐 BookContent.kt:194-205 的「正文长度或全文」
- steps[].ok 与 all_ok 保持旧语义（仅 fail → False），三个消费方零改动
"""

from core.constants import *
from core.urls import abs_url as _abs_url
from core.fetch import fetch, parse_source_header
from core.rules.replayer import extract_all_nodes
from core import quality as Q

#: 旧的 apply_css_rule 兼容名（services/add_source.py 仍在用）
from core.rules.replayer import extract_all as apply_css_rule


def _new_page(pages: dict, page_id: str, url: str, html: str,
              status: int = 200, charset: str = "") -> str:
    """把抓到的页面登记进 pages（按 id 去重），返回 page_id。"""
    if not html:
        return ""
    if page_id in pages:
        return page_id
    truncated = len(html) > Q.MAX_PAGE_HTML_CHARS
    pages[page_id] = {
        "id": page_id,
        "url": url,
        "status": status,
        "charset": charset,
        "html": html[:Q.MAX_PAGE_HTML_CHARS],
        "len": len(html),
        "truncated": truncated,
    }
    return page_id


def _extract(html: str, rule: str):
    """按规则取值 + 命中片段。

    **证据预算从 quality 取，这里必须显式传**——`replayer.extract_all_nodes` 故意
    不设默认值，就是为了让截断口径全仓库只有一处定义。这个 helper 是唯一的
    对接点，别绕过它直接调 replayer。
    """
    if not str(rule or "").strip():
        return [], [], "空规则"
    return extract_all_nodes(
        html, rule, Q.MATCHED_NODES_LIMIT, Q.MAX_MATCHED_HTML_CHARS)


def _step(name: str, judgement, url: str = "", page_id: str = "",
          values=None, matched_html: str = "", rule_error: str = "") -> dict:
    """把 Judgement 摊平成 steps[] 的一项。

    **摊平口径只有一处**：``quality.Judgement.as_step_dict()``。这里只是个短名字，
    绝不要把字典字面量抄回来——「试跑」和「批量校验」各写一份映射，一处忘记
    同步 verdict / notes 就会重新分叉，那正是本次改造要消灭的「同源不同判」。

    ``rule_error`` 必须从这里**透传**进去，不要走「先建字典、再事后赋值」——
    那会让 `as_step_dict` 的入参形同虚设，两个口径又并存了。
    """
    return judgement.as_step_dict(
        name, url=url, page_id=page_id,
        values=values or [], matched_html=matched_html, rule_error=rule_error,
    )


def _cap_evidence(steps: list, pages: dict) -> list:
    """证据总量硬上限。这是唯一一道与调用方无关的保险——ops.py 若将来改漏，
    它兜住，避免正文全文/整页 HTML 撑爆 job 的 result_json 与 SSE 流。"""
    total = sum(len(p.get("html", "")) for p in pages.values())
    total += sum(len(s.get("matched_html", "")) for s in steps)
    total += sum(len(v) for s in steps for v in s.get("values", []))
    if total <= Q.MAX_EVIDENCE_TOTAL_CHARS:
        return steps
    for s in steps:
        s["values"] = []
        s["matched_html"] = ""
        if "证据体积超出上限，已省略" not in s["notes"]:
            s["notes"].append("证据体积超出上限，已省略")
            s["has_notes"] = True
    for p in pages.values():
        p["html"] = p["html"][:1000]
        p["truncated"] = True
    return steps


def verify_chain(source: dict, keyword: str, detail_url: str = "",
                 pick: int = 1, proxy: str = "") -> dict:
    """
    全链路验证一个书源：
      search（搜索）→ bookUrl（取详情链接）→ toc（目录）→ content（正文）
    若 searchUrl 为空（仅发现模式），跳过搜索步，直接用 detail_url 从目录验证。
    返回 {"steps": [...], "pages": [...], "all_ok": bool}。
    """
    steps: list = []
    pages: dict = {}
    src = source or {}
    source_type = int(src.get("bookSourceType", 0) or 0)

    # 书源自身的请求头：不带它抓回来的 HTML 是失真的，「看源码改规则」就失去地基
    headers, header_why = parse_source_header(str(src.get("header", "") or ""))
    charset = str(src.get("charset", "") or "").strip()

    def _fetch(url: str) -> str:
        return fetch(url, headers=headers, charset=charset, proxy=proxy)

    search_tpl = src.get("searchUrl", "") or ""
    s_html = ""
    book_url = ""

    # ---- Step 1: 搜索（仅发现模式无搜索规则则跳过） ----
    if not search_tpl:
        skip_notes = ["仅发现模式，无搜索规则"]
        if header_why:
            skip_notes.append(header_why)
        j_skip = Q.Judgement("unknown", "", "empty", skip_notes)
        steps.append(_step("search", j_skip, "", ""))
        steps[-1]["detail"] = "跳过（仅发现模式，无搜索规则）"
        book_url = detail_url
        if not book_url:
            steps.append(_step("bookUrl", Q.Judgement("fail", "仅发现模式需提供详情页 URL"), "", ""))
            return {"steps": steps, "pages": [], "all_ok": False}
    else:
        search_url = search_tpl.replace("{{key}}", urllib.parse.quote(keyword))
        try:
            s_html = _fetch(search_url)
            book_list_rule = (src.get("ruleSearch") or {}).get("bookList", "")
            vals, hits, rule_error = _extract(s_html, book_list_rule)
            j = Q.judge_list_step("search", vals, "".join(hits), rule_error, source_type)
            page_id = _new_page(pages, "search", search_url, s_html, charset=charset)
            st = _step("search", j, search_url, page_id, vals, "".join(hits), rule_error)
            st["detail"] = j.reason or ("%d 条结果" % len(vals))
            steps.append(st)
            if not j.ok:
                return {"steps": steps, "pages": list(pages.values()), "all_ok": False}
        except Exception as e:
            steps.append(_step("search", Q.Judgement("fail", "抓取失败 %s" % e), search_url, ""))
            return {"steps": steps, "pages": list(pages.values()), "all_ok": False}


        # ---- Step 2: 详情链接（取第 pick 条） ----
        book_url_rule = (src.get("ruleSearch") or {}).get("bookUrl", "")
        hrefs, _hits, _err = _extract(s_html, book_url_rule)
        hrefs = [str(h).strip() for h in hrefs if str(h or "").strip()]
        if not hrefs or pick > len(hrefs):
            j = Q.Judgement("fail", "取不到详情链接")
            steps.append(_step("bookUrl", j, search_url, "", hrefs))
            return {"steps": steps, "pages": list(pages.values()), "all_ok": False}
        chosen = hrefs[pick - 1]
        book_url = chosen if chosen.startswith("http") else _abs_url(search_url, chosen)
        bu = _step("bookUrl", Q.Judgement("pass"), search_url,
                   "search" if "search" in pages else "", hrefs)
        bu["detail"] = "%d 条候选，取第 %d 条：%s" % (len(hrefs), pick, book_url)
        steps.append(bu)

        # 调用方给了 detail_url 时优先用它（更可靠）
        if detail_url:
            book_url = detail_url

    # ---- Step 3: 目录 ----
    # ruleBookInfo.tocUrl 非空时跳过详情页解析，直接抓目录页（对齐 Debug.kt:318-322）
    toc_url_rule = str((src.get("ruleBookInfo") or {}).get("tocUrl", "") or "").strip()
    if toc_url_rule:
        book_url = toc_url_rule if toc_url_rule.startswith("http") else _abs_url(book_url, toc_url_rule)

    t_html = ""
    try:
        t_html = _fetch(book_url)
        toc = src.get("ruleToc") or {}
        chapter_list_rule = toc.get("chapterList", "")
        chapters, hits, rule_error = _extract(t_html, chapter_list_rule)
        j = Q.judge_list_step("toc", chapters, "".join(hits), rule_error, source_type)
        page_id = _new_page(pages, "detail", book_url, t_html, charset=charset)
        st = _step("toc", j, book_url, page_id, chapters, "".join(hits), rule_error)
        st["detail"] = j.reason or ("%d 章" % len(chapters))
        steps.append(st)
        if not j.ok:
            return {"steps": steps, "pages": list(pages.values()), "all_ok": False}
    except Exception as e:
        steps.append(_step("toc", Q.Judgement("fail", "抓取失败 %s" % e), book_url, ""))
        return {"steps": steps, "pages": list(pages.values()), "all_ok": False}

    # ---- Step 4: 正文（抓第一章 URL） ----
    try:
        toc = src.get("ruleToc") or {}
        ch_url_rule = toc.get("chapterUrl", "")
        ch_urls, _hits, _err = _extract(t_html, ch_url_rule)
        # 过滤伪链接（javascript:/# 等），避免抓取报错
        ch_urls = [u for u in ch_urls
                   if u.strip() and not u.strip().lower().startswith(
                       ("javascript:", "#", "mailto:", "tel:"))]
        if not ch_urls:
            steps.append(_step("content", Q.Judgement("fail", "取不到章节URL"), book_url, ""))
            return {"steps": steps, "pages": list(pages.values()), "all_ok": False}
        first_ch = ch_urls[0]
        ch_url = _abs_url(book_url, first_ch) if not first_ch.startswith("http") else first_ch
        c_html = _fetch(ch_url)
        content_rule = (src.get("ruleContent") or {}).get("content", "")
        texts, hits, rule_error = _extract(c_html, content_rule)
        j = Q.judge_content(source_type, texts, content_rule, "".join(hits), rule_error)
        page_id = _new_page(pages, "chapter", ch_url, c_html, charset=charset)
        st = _step("content", j, ch_url, page_id, texts, "".join(hits), rule_error)
        st["detail"] = j.reason or ("%d 字符" % j.evidence.get("chars", 0))
        steps.append(st)
    except Exception as e:
        steps.append(_step("content", Q.Judgement("fail", "抓取失败 %s" % e), "", ""))

    # 静态错配检查（只有读 Legado 源码才知道的坑）
    misconfigs = Q.static_misconfig_notes(src)
    if header_why:
        misconfigs = [header_why] + misconfigs
    if misconfigs:
        for s in steps:
            if s["name"] in ("search", "content"):
                s["notes"] = list(s["notes"]) + misconfigs
                s["has_notes"] = True
                break

    steps = _cap_evidence(steps, pages)
    return {
        "steps": steps,
        "pages": list(pages.values()),
        "all_ok": all(s["ok"] for s in steps),
    }
```

- [ ] **Step 4: 跑测试**

```bash
.venv/Scripts/python.exe -m unittest tests.test_verify_chain -v
```

Expected: `OK`

- [ ] **Step 5: 跑全量回归**

```bash
.venv/Scripts/python.exe -m unittest discover -s tests -v
```

Expected: `OK`（`tests/test_repair.py` 也依赖 `verify_chain`，它必须照旧通过）

- [ ] **Step 6: 提交**

```bash
git add core/verify.py tests/test_verify_chain.py
git commit -m "feat(verify): 采集每步证据并接入 quality 判定，新增 tocUrl/file 分支与 proxy 透传"
```

---

## Task 6: `core/checker.py` —— 判定收拢 + 删除死分支 + `CACHE_VERSION` 升 6

**这是本批次唯一有回归风险的一步**（影响全部源的批量校验）。它必须独立成一个提交，便于单独 revert。

**三个动作**：

1. `_probe_content` / `_probe_toc` 的判定改调 `core.quality`，与试跑共用同一套口径。
2. **删掉 `ruleContent.image` 死分支**——这个字段 Legado 的 `ContentRule` 里不存在（`ContentRule.kt:12-25`），本项目也没有任何生产者（`analyzer.py:366` 把图片规则写进的是 `content`），所以 `image_rule` 恒为空，`checker.py:793` 的图片兜底**从未执行过**。
3. **`CACHE_VERSION` 5 → 6**。判定逻辑变了，但缓存里的 `toc_complete` / `content_ok` 是**旧逻辑算出来的**，而 `restore_from_cache` 正是拿它们重算星级。不升版本，收拢会"改了不生效"——可用源 TTL 14 天。

**Files:**
- Modify: `core/checker.py:48`（版本号）、`:742-805`（`_probe_content`）、`:668-740`（`_probe_toc`）、`:186-212`（`is_cache_item_valid` 不用改，但读一下确认逻辑）
- Test: `tests/test_checker_judge.py`（新建）

- [ ] **Step 1: 写失败测试**

创建 `tests/test_checker_judge.py`：

```python
# -*- coding: utf-8 -*-
"""批量校验的判定收拢：版本号与缓存失效。"""

import unittest
from datetime import datetime, timedelta

from core import checker
from core.models import BookSourceRecord, Health


class CacheVersionTests(unittest.TestCase):
    def test_version_bumped(self):
        """判定逻辑变了，缓存必须整体作废，否则收拢等于没做。"""
        self.assertEqual(checker.CACHE_VERSION, 6)

    def test_old_cache_item_rejected(self):
        raw = {"bookSourceUrl": "https://a.com", "bookSourceName": "x",
               "bookSourceType": 0}
        rec = BookSourceRecord(index=0, url="https://a.com", name="x", raw=raw)
        from core.loader import fingerprint
        item = {
            "v": 5,                                     # 旧版本
            "fingerprint": fingerprint(raw),
            "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "health": Health.OK,
        }
        self.assertFalse(checker.is_cache_item_valid(rec, item))

    def test_current_cache_item_accepted(self):
        raw = {"bookSourceUrl": "https://a.com", "bookSourceName": "x",
               "bookSourceType": 0}
        rec = BookSourceRecord(index=0, url="https://a.com", name="x", raw=raw)
        from core.loader import fingerprint
        item = {
            "v": checker.CACHE_VERSION,
            "fingerprint": fingerprint(raw),
            "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "health": Health.OK,
        }
        self.assertTrue(checker.is_cache_item_valid(rec, item))

    def test_expired_cache_item_rejected(self):
        raw = {"bookSourceUrl": "https://a.com", "bookSourceName": "x",
               "bookSourceType": 0}
        rec = BookSourceRecord(index=0, url="https://a.com", name="x", raw=raw)
        from core.loader import fingerprint
        item = {
            "v": checker.CACHE_VERSION,
            "fingerprint": fingerprint(raw),
            "checked_at": (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S"),
            "health": Health.OK,
        }
        self.assertFalse(checker.is_cache_item_valid(rec, item))


class JudgeMappingTests(unittest.TestCase):
    """quality 的三态映射必须落到 checker 既有的 None/True/False 语义上。"""

    def test_mapping_is_stable(self):
        from core import quality as Q
        self.assertIs(Q.Judgement(Q.VERDICT_PASS).checker_state, True)
        self.assertIs(Q.Judgement(Q.VERDICT_FAIL).checker_state, False)
        self.assertIsNone(Q.Judgement(Q.VERDICT_UNKNOWN).checker_state)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑测试，确认失败**

```bash
.venv/Scripts/python.exe -m unittest tests.test_checker_judge -v
```

Expected: `AssertionError: 5 != 6`

- [ ] **Step 3: 改版本号**

`core/checker.py:48`：

```python
# 缓存版本 6：正文/目录判定收拢到 core.quality（底线改为「非空即通过」，
# 删除了从未生效的 ruleContent.image 兜底）。判定口径变了，旧缓存的
# toc_complete / content_ok 是旧逻辑的产物，必须整体作废。
CACHE_VERSION = 6
```

- [ ] **Step 4: 跑测试，确认通过**

```bash
.venv/Scripts/python.exe -m unittest tests.test_checker_judge -v
```

Expected: `OK`

- [ ] **Step 5: 收拢 `_probe_content` 的判定**

在 `core/checker.py` 顶部 import 区加上：

```python
from core.quality import (judge_content, judge_list_step,
                         MATCHED_NODES_LIMIT, MAX_MATCHED_HTML_CHARS)
from core.rules.replayer import extract_all_nodes
```

> `MATCHED_NODES_LIMIT` / `MAX_MATCHED_HTML_CHARS` 必须显式传给 `extract_all_nodes`
> ——它故意不设默认值，好让截断口径全仓库只有一处定义（在 `core/quality.py`）。
> 注意依赖方向：**checker 依赖 quality，quality 不依赖 replayer**，这是刻意的，
> 别为了图省事让 replayer 反向 import quality。

把 `_probe_content` 里这段（约 `:761` 与 `:787-802`）：

```python
        image_rule = str(content.get("image", "") or "").strip()
```

**整行删除**，并删除函数 docstring 里那句「文本不达标或 content 规则缺失时，回退 ruleContent.image（图片 URL >= 3 张）」。

再把判定段：

```python
            ok = False
            if content_rule and "<js" not in content_rule:
                c_html = _decode_body(c_body)
                parts = apply_css_rule(c_html, _strip_rule_prefix(content_rule))
                text = "".join(str(p or "") for p in parts).strip()
                ok = len(text) > 100  # 正文长度阈值：防空壳页/验证码页/错误页
            if not ok and image_rule and "<js" not in image_rule:
                c_html = _decode_body(c_body)
                imgs = apply_css_rule(c_html, _strip_rule_prefix(image_rule))
                imgs = [str(u).strip() for u in imgs if str(u or "").strip()]
                ok = len(imgs) >= 3  # 图片正文判定：漫画图源至少 3 张图
            record.content_ok = ok
            if not ok:
                record.content_fail_reason = (
                    "正文提取为空或长度不足（疑似反爬/需登录/图片源）" if content_rule else "无正文规则且图片不足"
                )
```

替换为：

```python
            # 判定收拢到 core.quality：与「全链路试跑」共用同一套口径，
            # 避免同一个源在两个入口得到相反结论（详见设计文档 1.2）
            c_html = _decode_body(c_body)
            if content_rule:
                parts, hits, rule_error = extract_all_nodes(
                    c_html, _strip_rule_prefix(content_rule),
                    Q.MATCHED_NODES_LIMIT, Q.MAX_MATCHED_HTML_CHARS)
                verdict = judge_content(int(record.source_type or 0), parts,
                                        content_rule, "".join(hits), rule_error)
            else:
                # 空规则：quality 会按类型分派（文本源 fail / 音图源 pass）
                verdict = judge_content(int(record.source_type or 0), [], "")
            record.content_ok = verdict.checker_state      # True / False / None
            record.content_fail_reason = (
                "" if verdict.verdict == "pass"
                else (verdict.reason or "；".join(verdict.notes))
            )
```

- [ ] **Step 6: 收拢 `_probe_toc` 的判定**

把这段（约 `:717-723`）：

```python
            chapters = apply_css_rule(d_html, _strip_rule_prefix(chapter_list_rule))
            chapters = [str(c).strip() for c in chapters if str(c or "").strip()]
            record.chapter_count = len(chapters)
            if not chapters:
                record.toc_complete = None
                record.toc_fail_reason = "chapterList 解析为空（规则跑不了或目录分页加载）"
                return None
```

替换为：

```python
            # 基础判定交 core.quality（非空即通过）；比例比对留在下面由 checker 叠加，
            # 因为那依赖 TEST_TITLES 参考数据，是 checker 独有的信息
            chapters, hits, rule_error = extract_all_nodes(
                d_html, _strip_rule_prefix(chapter_list_rule),
                Q.MATCHED_NODES_LIMIT, Q.MAX_MATCHED_HTML_CHARS)
            chapters = [str(c).strip() for c in chapters if str(c or "").strip()]
            record.chapter_count = len(chapters)
            toc_verdict = judge_list_step("toc", chapters, "".join(hits), rule_error,
                                          int(record.source_type or 0))
            if toc_verdict.verdict != "pass":
                record.toc_complete = toc_verdict.checker_state   # False 或 None
                record.toc_fail_reason = (toc_verdict.reason
                                          or "；".join(toc_verdict.notes))
                return None
```

后面原有的「取参考表 → 比例比对」那段（`ref = self.test_titles.get(record.search_hit)` 起）**保持不动**。

- [ ] **Step 7: 跑全量回归**

```bash
.venv/Scripts/python.exe -m unittest discover -s tests -v
```

Expected: `OK`

- [ ] **Step 8: 提交**

```bash
git add core/checker.py tests/test_checker_judge.py
git commit -m "refactor(checker): 判定收拢到 core.quality，删除 ruleContent.image 死分支，缓存版本升 6"
```

> **提交后不要立刻跑全量校验。** 全量重校验是 3700 源、耗时长且对源站有请求量，必须主动安排时间窗，并先做基线快照（见文末「收尾」）。

---

## Task 7: `backend/api/ops.py` —— 剥离证据字段

「快速生成」任务的结果会被 `backend/jobs/runner.py:51` **写进 SQLite 的 `result_json`**，并经 `backend/api/jobs.py:47` 走 SSE 推送。若带上整页 HTML 与正文全文，等于把几 MB 塞进 jobs 表和推送流。

**Files:**
- Modify: `backend/api/ops.py:117-119`
- Test: `tests/test_ops_strip.py`（新建）

- [ ] **Step 1: 写失败测试**

创建 `tests/test_ops_strip.py`：

```python
# -*- coding: utf-8 -*-
"""快速生成任务的结果必须剥掉大体积证据字段。"""

import unittest

from backend.api.ops import strip_evidence


class StripEvidenceTests(unittest.TestCase):
    def _sample(self):
        return {
            "steps": [{
                "name": "content", "ok": True, "verdict": "pass",
                "reason": "", "notes": ["正文较短"], "has_notes": True,
                "values": ["很长的正文" * 1000],
                "matched_html": "<div>" * 1000,
                "evidence": {"chars": 5000},
            }],
            "pages": [{"id": "chapter", "html": "<html>" * 10000}],
            "all_ok": True,
        }

    def test_strips_big_fields(self):
        v = strip_evidence(self._sample())
        self.assertEqual(v["pages"], [])
        self.assertEqual(v["steps"][0]["values"], [])
        self.assertEqual(v["steps"][0]["matched_html"], "")

    def test_keeps_verdict_and_evidence(self):
        v = strip_evidence(self._sample())
        s = v["steps"][0]
        self.assertEqual(s["verdict"], "pass")
        self.assertTrue(s["has_notes"])
        self.assertEqual(s["notes"], ["正文较短"])
        self.assertEqual(s["evidence"], {"chars": 5000})
        self.assertEqual(v["all_ok"], True)

    def test_does_not_mutate_input(self):
        src = self._sample()
        strip_evidence(src)
        self.assertEqual(len(src["pages"]), 1)      # 原对象未被改动
        self.assertTrue(src["steps"][0]["values"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑测试，确认失败**

```bash
.venv/Scripts/python.exe -m unittest tests.test_ops_strip -v
```

Expected: `ImportError: cannot import name 'strip_evidence'`

- [ ] **Step 3: 实现**

在 `backend/api/ops.py` 的模块级（import 之后、job handler 之前）新增：

```python
def strip_evidence(verify_result: dict) -> dict:
    """剥掉试跑结果里的大体积证据字段，供「快速生成」任务使用。

    该结果会写进 SQLite 的 result_json 并经 SSE 推送（runner.py:51 /
    jobs.py:47），整页 HTML 与正文全文会让它膨胀到几 MB。
    判定结论（verdict / reason / notes / evidence）完整保留，只丢证据原文。

    返回**新对象**，不改动入参（测试会守住这一点）。
    """
    if not verify_result:
        return verify_result
    steps = []
    for s in verify_result.get("steps", []) or []:
        new_step = dict(s)
        new_step["values"] = []
        new_step["matched_html"] = ""
        steps.append(new_step)
    return {**verify_result, "steps": steps, "pages": []}
```

然后把 `backend/api/ops.py:117-119` 附近改为：

```python
        if verify:
            v = await asyncio.to_thread(verify_chain, source, keyword, detail_url, pick)
            # 快速生成的结果会写库并走 SSE，剥掉体积大的证据字段；判定结论完整保留
            v = strip_evidence(v)
        else:
            v = {"steps": [], "all_ok": None, "skipped": True}
```

- [ ] **Step 4: 跑测试**

```bash
.venv/Scripts/python.exe -m unittest tests.test_ops_strip -v
```

Expected: `OK`

- [ ] **Step 5: 提交**

```bash
git add backend/api/ops.py tests/test_ops_strip.py
git commit -m "feat(ops): 快速生成任务剥离试跑证据字段，避免撑爆 jobs 表与 SSE"
```

---

## Task 8: 前端 —— `GET /api/sources/exists`

新建书源时若填了**已存在的域名**，`upsert_sources` 会按 URL 主键**静默覆盖原源的全部规则**，用户全程无感。这是本次数据丢失风险最高的一条。

**Files:**
- Modify: `backend/api/sources.py`（在 `get_detail` 之后新增）
- Modify: `frontend/src/api/sources.js`

- [ ] **Step 1: 加后端接口**

在 `backend/api/sources.py` 的 `get_detail` 函数之后插入：

```python
@router.get("/exists")
def source_exists(url: str, st=Depends(get_store)):
    """查域名是否已存在。新建书源保存前调用，用于阻止静默覆盖。

    比复用 /detail 再解析 404 错误串更稳（api/client.js 抛的是字符串错误）。
    """
    src = st.get_source(url)
    if not src:
        return {"exists": False, "name": ""}
    return {"exists": True, "name": str(src.get("bookSourceName", "") or "")}
```

- [ ] **Step 2: 手工验证接口**

```bash
.venv/Scripts/python.exe -m uvicorn backend.app:app --port 8787
```

另开一个终端：

```bash
curl -s "http://127.0.0.1:8787/api/sources/exists?url=https://example-not-exist.com"
```

Expected: `{"exists":false,"name":""}`

再拿一个库里真实存在的域名试（从 `GET /api/sources?limit=1` 拿）：

```bash
curl -s "http://127.0.0.1:8787/api/sources?limit=1"
curl -s "http://127.0.0.1:8787/api/sources/exists?url=<上一步拿到的 source_url>"
```

Expected: `{"exists":true,"name":"<源名>"}`

- [ ] **Step 3: 前端 API 封装**

在 `frontend/src/api/sources.js` 末尾追加：

```javascript
export const sourceExists = (url) =>
  api.get("/sources/exists?url=" + encodeURIComponent(url));
```

- [ ] **Step 4: 提交**

```bash
git add backend/api/sources.py frontend/src/api/sources.js
git commit -m "feat(sources): 新增 /sources/exists 接口，为覆盖确认提供依据"
```

---

# 阶段 B：前端

> **前端没有测试框架**（`frontend/package.json` 里只有 vite + vue，没有 vitest）。
> 所以阶段 B 的任务**不写自动化测试**，改用「改完怎么手工验证」的步骤。
> 这是现状约束，不是我跳过 TDD。

## Task 9: `RuleDebugDrawer.vue` —— 调试抽屉

**Files:**
- Create: `frontend/src/components/RuleDebugDrawer.vue`
- Modify: `frontend/src/styles.css`（加 `.dot.unknown` 等）

- [ ] **Step 1: 与 spec 的一处偏离（先读）**

spec §10 的方案是「后端在采集阶段对 HTML 做一次仅用于展示的轻量换行，前端按行渲染」。**本计划不这么做**，改为「`white-space: pre-wrap` 软换行 + 按字符偏移分片渲染」。

理由：**用户会把这段源码复制去改规则**。后端注入的换行会跟着被复制，复制出来的源码与真实响应**不一致**——调试工具给出改动过的数据，比给出难读的数据更糟。

替代方案同样能满足 spec 的三条要求（默认渲染前 N 字符、搜索在原始串上做、只渲染命中附近），而且不改变字符。

- [ ] **Step 2: 写组件**

创建 `frontend/src/components/RuleDebugDrawer.vue`：

```vue
<script setup>
// 试跑调试抽屉：把每一步的证据摊开。
// 三个子页签：提取结果（全文）/ 命中源码 / 整页源码。
//
// 设计取舍：**不对 HTML 注入换行**。
// 虽然注入后按行渲染更省事，但用户会把这段源码复制去改规则——
// 改过字符的源码会与真实响应不一致。改为 pre-wrap 软换行 +
// 按字符偏移分片渲染，保证「复制出来的就是原文」。
import { ref, computed, watch, nextTick } from "vue";

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  result: { type: Object, default: null },
  initialStep: { type: String, default: "" },
});
const emit = defineEmits(["update:modelValue", "goto"]);

const visible = computed({
  get: () => props.modelValue,
  set: (v) => emit("update:modelValue", v),
});

const STEP_LABELS = { search: "搜索", bookUrl: "详情链接", toc: "目录", content: "正文" };
//: 每次渲染的字符数。整页 HTML 可能 100 万字符，全量进 DOM 会卡
const RENDER_CHUNK = 20000;

const activeStep = ref("");
const subTab = ref("values");
const renderLimit = ref(RENDER_CHUNK);
const searchKey = ref("");
const activeHit = ref(0);

const steps = computed(() => (props.result && props.result.steps) || []);
const pages = computed(() => (props.result && props.result.pages) || []);
const current = computed(
  () => steps.value.find((s) => s.name === activeStep.value) || steps.value[0] || null,
);
const currentPage = computed(() => {
  if (!current.value || !current.value.page_id) return null;
  return pages.value.find((p) => p.id === current.value.page_id) || null;
});

// 三态圆点：fail 红 / unknown 灰 / pass 且有附注 黄 / 纯 pass 绿
function dotClass(s) {
  if (!s) return "";
  if (s.verdict === "fail") return "err";
  if (s.verdict === "unknown") return "unknown";
  return s.has_notes ? "warn" : "ok";
}
function tagType(s) {
  if (!s) return "info";
  if (s.verdict === "fail") return "danger";
  if (s.verdict === "unknown") return "info";
  return s.has_notes ? "warning" : "success";
}
const VERDICT_TEXT = { pass: "通过", fail: "失败", unknown: "无法判定" };
function verdictText(s) {
  return (s && VERDICT_TEXT[s.verdict]) || "";
}

// 搜索在**完整原文**上做（纯字符串扫描，结果不进 DOM），最多记 200 处
const hitOffsets = computed(() => {
  const key = searchKey.value.trim();
  const html = (currentPage.value && currentPage.value.html) || "";
  if (!key || !html) return [];
  const out = [];
  let from = 0;
  while (out.length < 200) {
    const i = html.indexOf(key, from);
    if (i < 0) break;
    out.push(i);
    from = i + Math.max(1, key.length);
  }
  return out;
});

// 只渲染前 renderLimit 个字符，避免百万字符全量进 DOM
const headText = computed(() => {
  const html = (currentPage.value && currentPage.value.html) || "";
  return html.slice(0, renderLimit.value);
});
const hasMore = computed(() => {
  const html = (currentPage.value && currentPage.value.html) || "";
  return renderLimit.value < html.length;
});

// 把已渲染的片段按命中位置切成 [普通, 高亮, 普通, ...]
const segments = computed(() => {
  const head = headText.value;
  const key = searchKey.value.trim();
  if (!key) return [{ text: head, hit: false, index: -1 }];
  const segs = [];
  let cursor = 0;
  let hitIndex = 0;
  hitOffsets.value.forEach((off) => {
    if (off >= head.length) return;
    if (off > cursor) segs.push({ text: head.slice(cursor, off), hit: false, index: -1 });
    segs.push({
      text: head.slice(off, off + key.length),
      hit: true,
      index: hitIndex,
    });
    hitIndex += 1;
    cursor = off + key.length;
  });
  if (cursor < head.length) segs.push({ text: head.slice(cursor), hit: false, index: -1 });
  return segs;
});

watch(() => props.modelValue, (show) => {
  if (!show) return;
  activeStep.value = props.initialStep || (steps.value[0] && steps.value[0].name) || "";
  subTab.value = "values";
  renderLimit.value = RENDER_CHUNK;
  searchKey.value = "";
  activeHit.value = 0;
});

// 跳到第 i 处命中（i 为负则向前），必要时先扩大渲染范围
function gotoHit(i) {
  const total = hitOffsets.value.length;
  if (!total) return;
  activeHit.value = ((i % total) + total) % total;
  const off = hitOffsets.value[activeHit.value];
  if (off + RENDER_CHUNK > renderLimit.value) renderLimit.value = off + RENDER_CHUNK;
  nextTick(() => {
    const el = document.getElementById("debug-hit-" + activeHit.value);
    if (el) el.scrollIntoView({ block: "center", behavior: "smooth" });
  });
}

function selectStep(name) {
  activeStep.value = name;
  subTab.value = "values";
  renderLimit.value = RENDER_CHUNK;
  searchKey.value = "";
  activeHit.value = 0;
}

function loadMore() {
  renderLimit.value += RENDER_CHUNK;
}

function copyMatched() {
  const text = (current.value && current.value.matched_html) || "";
  navigator.clipboard.writeText(text);
}
</script>

<template>
  <el-drawer v-model="visible" title="试跑调试" size="72%" destroy-on-close>
    <el-empty v-if="!steps.length" description="没有试跑结果" :image-size="80" />

    <template v-else>
      <div class="debug-step-tabs">
        <span v-for="s in steps" :key="s.name" class="debug-step-tab"
              :class="{ active: s.name === activeStep }" @click="selectStep(s.name)">
          <i class="dot" :class="dotClass(s)"></i>{{ STEP_LABELS[s.name] || s.name }}
        </span>
      </div>

      <div v-if="current" class="debug-head">
        <el-tag size="small" :type="tagType(current)">{{ verdictText(current) }}</el-tag>
        <span class="mono muted grow">{{ current.url }}</span>
      </div>

      <p v-if="current && current.reason" class="debug-reason">{{ current.reason }}</p>
      <p v-if="current && current.rule_error" class="debug-reason">
        规则无法离线回放：{{ current.rule_error }}
      </p>
      <ul v-if="current && current.notes && current.notes.length" class="debug-notes">
        <li v-for="(n, i) in current.notes" :key="i">
          {{ n }}
          <el-button v-if="n.indexOf('bookSourceType') >= 0" size="small" link
                     type="primary" @click="emit('goto', 'basic')">
            去改类型
          </el-button>
        </li>
      </ul>

      <el-tabs v-model="subTab">
        <el-tab-pane label="提取结果" name="values">
          <div v-if="current" class="debug-evidence">
            <span>条数 {{ current.evidence.values_total }}</span>
            <span>字符 {{ current.evidence.chars }}</span>
            <span>中文 {{ current.evidence.cjk_chars }}</span>
            <span>块级分隔 {{ current.evidence.block_seps }}</span>
            <span>标签占比 {{ current.evidence.tag_ratio }}</span>
            <span v-if="current.evidence.noise_hit">噪声命中「{{ current.evidence.noise_hit }}」</span>
          </div>
          <p class="muted" style="margin: 6px 0">
            这里是规则**实际取到的值**。正文规则通常只有 1 条、就是全文。
          </p>
          <div v-for="(v, i) in (current ? current.values : [])" :key="i" class="debug-value">
            <div class="debug-value-idx">#{{ i + 1 }}（{{ v.length }} 字符）</div>
            <pre class="debug-pre">{{ v }}</pre>
          </div>
          <el-empty v-if="current && !current.values.length"
                    description="没有取到值" :image-size="60" />
        </el-tab-pane>

        <el-tab-pane label="命中源码" name="matched">
          <p class="muted" style="margin: 6px 0">
            规则**选中的那块 DOM** 的 outerHTML——改规则时看这个，
            比在整页里猜要快得多。
          </p>
          <div class="toolbar">
            <el-button size="small" @click="copyMatched">复制</el-button>
          </div>
          <pre v-if="current && current.matched_html"
               class="debug-pre debug-pre-wrap">{{ current.matched_html }}</pre>
          <el-empty v-else description="没有命中片段" :image-size="60" />
        </el-tab-pane>

        <el-tab-pane label="整页源码" name="page">
          <template v-if="currentPage">
            <div class="toolbar" style="margin-bottom: 8px">
              <el-input v-model="searchKey" size="small" style="width: 200px"
                        placeholder="搜索（如 content）" clearable />
              <template v-if="hitOffsets.length">
                <el-button size="small" @click="gotoHit(activeHit - 1)">上一处</el-button>
                <el-button size="small" @click="gotoHit(activeHit + 1)">下一处</el-button>
                <span class="muted">第 {{ activeHit + 1 }} / {{ hitOffsets.length }} 处</span>
              </template>
              <span v-else-if="searchKey" class="muted">未找到</span>
            </div>
            <el-alert v-if="currentPage.truncated" type="warning" :closable="false"
                      show-icon style="margin-bottom: 8px"
                      :title="'原文 ' + currentPage.len + ' 字符，已截断到前 '
                              + currentPage.html.length + ' 字符'" />
            <pre class="debug-pre debug-pre-wrap"><template
              v-for="(seg, i) in segments" :key="i"><span
              v-if="!seg.hit">{{ seg.text }}</span><mark
              v-else :id="'debug-hit-' + seg.index"
              :class="{ 'debug-hit-active': seg.index === activeHit }">{{ seg.text }}</mark></template></pre>
            <div v-if="hasMore" class="toolbar" style="margin-top: 8px">
              <el-button size="small" @click="loadMore">
                加载更多（已渲染 {{ renderLimit }} / {{ currentPage.html.length }} 字符）
              </el-button>
            </div>
          </template>
          <el-empty v-else description="这一步没有抓到页面" :image-size="60" />
        </el-tab-pane>
      </el-tabs>
    </template>
  </el-drawer>
</template>

<style scoped>
.debug-step-tabs { display: flex; gap: 4px; margin-bottom: 12px; flex-wrap: wrap; }
.debug-step-tab {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 4px 10px; border-radius: 4px; cursor: pointer;
  border: 1px solid #dcdfe6; font-size: 13px;
}
.debug-step-tab.active { border-color: #409eff; color: #409eff; }
.debug-head { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
.debug-reason { color: #f56c6c; margin: 4px 0; }
.debug-notes { color: #e6a23c; margin: 4px 0; padding-left: 18px; line-height: 1.7; }
.debug-evidence {
  display: flex; gap: 14px; flex-wrap: wrap;
  color: #909399; font-size: 12px; margin-bottom: 8px;
}
.debug-value { margin-bottom: 10px; }
.debug-value-idx { color: #909399; font-size: 12px; margin-bottom: 2px; }
.debug-pre {
  font-family: Consolas, Monaco, monospace; font-size: 12px;
  background: #f5f7fa; padding: 8px; border-radius: 4px;
  max-height: 52vh; overflow: auto; margin: 0;
}
.debug-pre-wrap { white-space: pre-wrap; word-break: break-all; }
.debug-hit-active { outline: 2px solid #f56c6c; }
</style>
```

- [ ] **Step 3: 加圆点样式**

在 `frontend/src/styles.css` 的 `.dot.ok / .dot.warn / .dot.err` 那一组后面追加：

```css
/* 试跑调试：unknown（无法判定）用灰色，与 fail 的红明确区分 */
.dot.unknown { background: #909399; }
```

- [ ] **Step 4: 手工验证**

```bash
cd frontend && npm run dev
```

打开任意源的编辑弹窗 → 点「全链路试跑」→ 结果出来后在浏览器控制台执行：

```js
// 拿到结果对象供检查（组件内是局部的，这里只看数据形状）
```

先只验证**组件能挂载**：把抽屉临时挂到 `SourceEditDialog.vue`（Task 10 会正式接），或在 Vue DevTools 里确认无报错。这一步只要求**编译通过、无控制台报错**。

```bash
cd frontend && npm run build
```

Expected: 构建成功，无报错

- [ ] **Step 5: 提交**

```bash
git add frontend/src/components/RuleDebugDrawer.vue frontend/src/styles.css
git commit -m "feat(frontend): 新增试跑调试抽屉，摊开提取结果/命中片段/整页源码"
```

---

## Task 10: `SourceEditDialog.vue` —— 三态渲染 + 结果过期 + 抽屉接入

**Files:**
- Modify: `frontend/src/components/SourceEditDialog.vue`

- [ ] **Step 1: 引入抽屉组件与状态**

`SourceEditDialog.vue` 的 `<script setup>` 顶部 import 区（第 4-8 行附近）改为：

```javascript
import { ref, computed, watch, nextTick } from "vue";
import { ElMessage } from "element-plus";
import { api, subscribeJob } from "../api/client";
import { getDetail, listTags, saveSource, sourceExists } from "../api/sources";
import { mergeGroup, splitSystemUser } from "../utils/tags";
import { useMobile } from "../composables/useMobile";
import RuleDebugDrawer from "./RuleDebugDrawer.vue";
```

在 `const testResult = ref(null);` 之后追加：

```javascript
const testStale = ref(false);      // 规则已改动，结果过期
const debugVisible = ref(false);   // 调试抽屉
const debugStep = ref("");         // 抽屉打开时定位到哪一步
```

- [ ] **Step 2: 结果的「过期」逻辑（P1-④）**

在 `watch(() => props.modelValue, ...)` 之后追加（**注意 deep watch 要在 `form` 定义之后**）：

```javascript
// 规则一改，上一轮试跑结论就作废了。不标过期的话，用户改了规则
// 还看到绿色的「全部通过」，会据此保存——这正是本次要消除的误导。
watch(form, () => {
  if (testResult.value) testStale.value = true;
}, { deep: true });
```

在 `testRun()` 里，`testing.value = true;` 之后加 `testStale.value = false;`：

```javascript
async function testRun() {
  testing.value = true;
  testResult.value = null;
  testStale.value = false;          // 新一轮开始，先清过期标记
  try {
    testResult.value = await api.post("/rules/chain", {
      source: form.value,
      keyword: testKey.value || "我",
      detail_url: testDetailUrl.value || quickDetailUrl.value || "",
      pick: 1,
    });
  } catch (e) {
    testResult.value = { error: String(e.message) };
  } finally {
    testing.value = false;
  }
  expandTestFailures(testResult.value);
}
```

- [ ] **Step 3: 三态辅助函数**

在 `expandTestFailures` 之前插入：

```javascript
// 三态圆点与徽章：fail 红 / unknown 灰 / pass 且有附注 黄 / 纯 pass 绿
function dotClass(step) {
  if (step.verdict === "fail") return "err";
  if (step.verdict === "unknown") return "unknown";
  return step.has_notes ? "warn" : "ok";
}
function tagTypeOf(step) {
  if (step.verdict === "fail") return "danger";
  if (step.verdict === "unknown") return "info";
  return step.has_notes ? "warning" : "success";
}
const STEP_LABELS = { search: "搜索", bookUrl: "详情链接", toc: "目录", content: "正文" };

// 「全部通过」按 verdict 算，不再用 all_ok。**有附注的 pass 仍算通过**
function allPassed() {
  const steps = (testResult.value && testResult.value.steps) || [];
  return steps.length > 0 && steps.every((s) => s.verdict === "pass");
}
```

- [ ] **Step 4: 失败/疑似时切页签**

`expandTestFailures` 改为（原来只处理 `!s.ok`）：

```javascript
function expandTestFailures(res) {
  if (!res || !Array.isArray(res.steps)) return;
  // 失败和有附注的都要把人带到对应页签——附注是「通过了但有疑点」，
  // 不引导过去的话用户根本不会看到
  const interesting = res.steps.filter((s) => s.verdict === "fail" || s.has_notes);
  if (!interesting.length) return;
  const map = { search: "search", bookUrl: "search", toc: "toc", content: "content" };
  const target = interesting.map((s) => map[s.name]).filter(Boolean);
  if (!target.length) return;
  activeTab.value = "rules";
  activeRuleTab.value = target[0];
}
```

- [ ] **Step 5: `tabDot('rules')` 三态**

`tabDot` 里的 `rules` 分支改为：

```javascript
  if (name === "rules") {
    const steps = (testResult.value && testResult.value.steps) || [];
    if (steps.some((s) => s.verdict === "fail")) return "err";
    if (steps.some((s) => s.has_notes)) return "warn";
    return ["search", "detail", "toc", "content"].every(filled) ? "ok" : "warn";
  }
```

- [ ] **Step 6: 右侧卡片改成三态 + 加「查看证据」**

把模板里「全链路试跑」卡片的 `v-else-if="!testResult.error"` 那段（约第 580-588 行）整体替换为：

```html
          <template v-else-if="!testResult.error">
            <div v-for="s in testResult.steps" :key="s.name" class="quick-step">
              <el-tag size="small" :type="tagTypeOf(s)">
                {{ STEP_LABELS[s.name] || s.name }}
              </el-tag>
              <span class="muted">{{ s.detail }}</span>
            </div>
            <el-alert v-if="testStale" type="info" :closable="false" show-icon
                      style="margin-top: 10px"
                      title="规则已改动，结果已过期，请重跑" />
            <p v-else style="margin: 10px 0 0">
              <b>{{ allPassed() ? "全部通过" : "未全部通过" }}</b>
              <span v-if="testResult.steps.some((s) => s.has_notes)"
                    class="muted">（有疑点，见附注）</span>
            </p>
            <div class="toolbar" style="margin-top: 8px">
              <el-button size="small" type="primary" plain @click="openDebug()">
                查看证据
              </el-button>
            </div>
          </template>
```

在 `testRun()` 附近新增：

```javascript
function openDebug(step) {
  debugStep.value = step || "";
  debugVisible.value = true;
}
```

- [ ] **Step 7: 挂载抽屉**

在模板的 `</el-dialog>` 之前（`<template #footer>` 之后）插入：

```html
    <RuleDebugDrawer v-model="debugVisible" :result="testResult"
                     :initial-step="debugStep" />
```

> Task 12 会在这行上加一个 `@goto="onDebugGoto"`（类型不符 note 的「去改类型」入口）。现在先不加。

- [ ] **Step 8: 手工验证**

```bash
cd frontend && npm run dev
```

1. 打开源编辑弹窗 → 点「全链路试跑」→ 右侧应出现四行，带三态颜色的标签
2. 点「查看证据」→ 抽屉打开，四个步骤页签可切换
3. 「提取结果」页应能看到正文**全文**（不是「1234 字符」）
4. 切「整页源码」，搜一个页面里存在的 class 名 → 应能高亮并跳转
5. **关掉抽屉，改任意一个规则字段** → 右侧卡片应立刻出现「规则已改动，结果已过期」

- [ ] **Step 9: 提交**

```bash
git add frontend/src/components/SourceEditDialog.vue
git commit -m "feat(frontend): 试跑结果三态渲染、结果过期提示、接入调试抽屉"
```

---

## Task 11: `SourceEditDialog.vue` —— P0 三项（数据丢失级）

**这三条与本次调试功能无关，但每条都会让用户实实在在丢工作成果。** 尤其 P0-② 会造成主库数据损坏。

**Files:**
- Modify: `frontend/src/components/SourceEditDialog.vue`

- [ ] **Step 1: P0-① —— 未保存确认**

在 `<script setup>` 里新增：

```javascript
const savedSnapshot = ref("");     // 打开/保存时的表单快照
let rawDirty = false;              // 用户是否手改过「原始 JSON」文本域

// 表单是否有未保存改动
function isDirty() {
  if (!savedSnapshot.value) return false;
  return JSON.stringify(form.value) !== savedSnapshot.value;
}
```

在打开弹窗的 `watch` 里（`props.modelValue` 为真时）记录快照——**两处都要加**：新建分支的末尾和编辑分支的 `finally` 之前：

```javascript
    savedSnapshot.value = JSON.stringify(form.value);   // 新建分支：在 syncRawFromForm() 之后
    // ... 编辑分支：在 syncRawFromForm() 之后
```

`.el-dialog` 标签加两个属性并接上确认：

```html
  <el-dialog v-model="visible" :title="isNew ? '新建书源' : '编辑书源'"
             width="1120px" top="4vh" destroy-on-close class="edit-dialog"
             modal-class="edit-dialog-overlay"
             :close-on-click-modal="false" :before-close="handleBeforeClose">
```

```javascript
import { ElMessage, ElMessageBox } from "element-plus";

// 点遮罩/取消/ESC 都走这里。改了几十条规则误点空白处就全丢，
// 这个代价太大，必须拦一道
function handleBeforeClose(done) {
  if (!isDirty()) return done();
  ElMessageBox.confirm("有未保存的修改，确定要关闭吗？", "未保存", {
    confirmButtonText: "丢弃修改",
    cancelButtonText: "继续编辑",
    type: "warning",
  }).then(() => done()).catch(() => {});
}
```

页脚的「取消」按钮改为显式走同一条路：

```html
      <el-button @click="handleBeforeClose(() => (visible = false))">取消</el-button>
```

- [ ] **Step 2: P0-② —— 覆盖已有源的确认**

`save()` 改名为 `doSave()`，并新增包装的 `save()`：

```javascript
// 新建书源时若填了已存在的域名，后端 upsert_sources 会按 URL 主键
// **静默覆盖原源的全部规则**——用户全程无感。保存前先探一下。
async function save() {
  const s = JSON.parse(JSON.stringify(form.value));
  s.bookSourceType = Number(s.bookSourceType);
  if (![0, 1, 2, 3, 4].includes(s.bookSourceType)) s.bookSourceType = 0;
  if (!String(s.bookSourceName || "").trim()) return ElMessage.warning("名称不能为空");
  if (!String(s.bookSourceUrl || "").trim()) return ElMessage.warning("域名不能为空");

  if (isNew.value && !isDuplicate.value) {
    try {
      const r = await sourceExists(s.bookSourceUrl);
      if (r.exists) {
        try {
          await ElMessageBox.confirm(
            `域名已存在（源名：${r.name || "(无名)"}），继续将覆盖其全部规则。`,
            "确认覆盖", { type: "warning", confirmButtonText: "覆盖", cancelButtonText: "取消" },
          );
        } catch (e) {
          return;   // 用户取消
        }
      }
    } catch (e) {
      // 探测失败不阻塞保存，只在控制台留痕
      console.warn("exists 探测失败", e);
    }
  }
  await doSave(s);
}

async function doSave(s) {
  s.bookSourceGroup = mergeGroup(displaySystemTags.value, userTags.value);
  try {
    await saveSource(s, userTags.value, !!manualStatus.value);
    ElMessage.success("已保存");
    savedSnapshot.value = JSON.stringify(form.value);   // 保存成功后刷新快照
    emit("saved", s);
  } catch (e) {
    ElMessage.error("保存失败: " + e.message);
  }
}
```

> `isDuplicate` 在 Task 12（P1-⑥）引入。本步骤先把 P0-② 做完，**暂时把 `!isDuplicate.value` 去掉**，Task 12 再加回来。若你按顺序做，这里写 `if (isNew.value) {` 即可。

- [ ] **Step 3: P0-③ —— 原始 JSON 快照陈旧**

**只有两个状态**，不做字符串 diff：

- `rawDirty`：用户在文本域里手改过（`@input` 置真，`applyRawJson` 成功后清零）
- 表单变更时若 `rawDirty` 为假 → 自动重新生成快照

`applyRawJson()` 末尾加 `rawDirty = false;`：

```javascript
    rawJsonError.value = "";
    rawDirty = false;                 // 已应用到表单，文本域与表单一致了
    ElMessage.success("已应用到表单");
```

模板里的原始 JSON 文本域加 `@input`：

```html
            <el-alert v-if="rawDirty" type="warning" :closable="false" show-icon
                      style="margin-bottom: 8px"
                      title="你正在手改这段 JSON。点「从表单生成」会覆盖你的改动。" />
            <el-input v-model="rawJsonText" type="textarea" :rows="18" class="raw-json"
                      @input="rawDirty = true" />
```

在 Task 10 那个 `watch(form, ...)` 里补一行自动同步：

```javascript
watch(form, () => {
  if (testResult.value) testStale.value = true;
  // 用户没动过文本域时保持快照新鲜——否则「应用到表单」会把表单改动全部回滚
  if (!rawDirty) rawJsonText.value = JSON.stringify(form.value, null, 2);
}, { deep: true });
```

「应用到表单」按钮加二次确认：

```javascript
function applyRawJson() {
  try {
    const obj = JSON.parse(rawJsonText.value || "{}");
    if (!obj || typeof obj !== "object" || Array.isArray(obj)) {
      throw new Error("必须是 JSON 对象");
    }
    const apply = () => {
      form.value = { ...blank(), ...obj };
      const parsed = splitSystemUser(form.value.bookSourceGroup || "");
      systemTags.value = parsed.system;
      manualStatus.value = "";
      userTags.value = parsed.user;
      rawJsonError.value = "";
      rawDirty = false;
      ElMessage.success("已应用到表单");
    };
    // 手改过就确认一次：这一步会整份替换表单，不可撤销
    if (rawDirty) {
      ElMessageBox.confirm("将用这段 JSON 整份替换当前表单，确定吗？", "应用到表单", {
        type: "warning", confirmButtonText: "替换", cancelButtonText: "取消",
      }).then(apply).catch(() => {});
      return;
    }
    apply();
  } catch (e) {
    rawJsonError.value = e.message;
    ElMessage.error("JSON 解析失败: " + e.message);
  }
}
```

- [ ] **Step 4: 手工验证**

```bash
cd frontend && npm run dev
```

1. 打开源编辑 → 改两条规则 → **点遮罩空白处** → 应弹「有未保存的修改」；点「继续编辑」后规则还在
2. 新建书源 → 域名填一个**已存在**的域名 → 保存 → 应弹「域名已存在（源名：xxx）」
3. 打开源编辑 → 改规则 → 切「原始 JSON」→ 看到的应是**最新表单**（不是打开时的旧快照）
4. 在「原始 JSON」里手改几个字 → 出现黄色提示条 → 点「应用到表单」→ 应弹二次确认

- [ ] **Step 5: 提交**

```bash
git add frontend/src/components/SourceEditDialog.vue
git commit -m "fix(frontend): 编辑弹窗三处静默数据丢失——遮罩关闭、覆盖已有源、JSON 快照回滚"
```

---

## Task 12: `SourceEditDialog.vue` —— P1 三项

**Files:**
- Modify: `frontend/src/components/SourceEditDialog.vue`

- [ ] **Step 1: P1-⑤ —— 基本信息页签圆点**

`tabDot` 里的 `basic` 分支改为：

```javascript
  if (name === "basic") {
    const ok = String(form.value.bookSourceName || "").trim()
            && String(form.value.bookSourceUrl || "").trim();
    return ok ? "ok" : "err";
  }
```

> 原来的 `if (name === "basic") return "ok";` 恒定返回绿点，名称/域名为空时也是绿的——零信息量。

- [ ] **Step 2: P1-⑥ —— 另存为新源**

在状态区（`const isNew = computed(...)` 附近）加：

```javascript
const isDuplicate = ref(false);    // 「另存为新源」模式
```

`isNew` 改为两个真值来源：

```javascript
const isNew = computed(() => isDuplicate.value || !props.sourceUrl);
```

打开弹窗的 `watch` 里重置它（新建分支与编辑分支开头各加一次）：

```javascript
  isDuplicate.value = false;
```

标题旁边加按钮——把 `.el-dialog` 的 `:title` 换成插槽形式：

```html
    <template #header>
      <div class="dialog-header">
        <span>{{ isDuplicate ? "另存为新源" : (isNew ? "新建书源" : "编辑书源") }}</span>
        <el-button v-if="!isNew && !isDuplicate" size="small" link type="primary"
                   @click="startDuplicate">另存为新源</el-button>
      </div>
    </template>
```

```javascript
// 编辑模式域名不可改，但原先只提示「请另存为新源」却没有这个入口
function startDuplicate() {
  isDuplicate.value = true;
  form.value.bookSourceUrl = "";      // 清空域名是刻意的：另存必须换个域名
  ElMessage.info("已切换为另存模式，请填写新的域名");
}
```

域名输入框的解禁条件由 `:disabled="!isNew"` 改为：

```html
                <el-input v-model="form.bookSourceUrl" :disabled="!isNew" />
```

（`isNew` 已含 `isDuplicate`，所以无需改动这行——**只需确认它是 `:disabled="!isNew"`**。若不是则改成它。）

提示文案也改一下：

```html
                <span v-if="!isNew" class="muted">编辑模式下域名不可改；需要更换请点右上角「另存为新源」。</span>
```

最后把 Task 11 Step 2 里暂时去掉的 `!isDuplicate.value` 加回来：

```javascript
  if (isNew.value && !isDuplicate.value) {
```

> 另存模式**不做**「已存在」确认——因为用户此前的域名已被清空，走的是全新域名，不该再拿旧域名去查。

- [ ] **Step 3: 类型不符 note 的「去改类型」入口（设计文档 §6.3 / §15.2）**

判定给出「实测为图片，与声明的 📖小说 不一致」之后，用户需要一条去改类型的路。**不做一键写入**——改类型是写操作，应该走用户明确确认的保存流程；这里只把人送到「基本信息」页签。

抽屉那一侧已经在 Task 9 里备好了：`defineEmits` 含 `goto`，「提取结果」页签的 note 上挂了「去改类型」按钮。这里只需要接住它：

```javascript
// 抽屉请求跳到某个页签（目前只有「去改类型」用它）
function onDebugGoto(tab) {
  debugVisible.value = false;
  activeTab.value = tab;
}
```

模板里的抽屉挂载改为：

```html
    <RuleDebugDrawer v-model="debugVisible" :result="testResult"
                     :initial-step="debugStep" @goto="onDebugGoto" />
```

- [ ] **Step 3: 加 header 样式（承接上面的「另存为新源」标题插槽）**

`SourceEditDialog.vue` 的 `<style scoped>` 末尾追加：

```css
.dialog-header { display: flex; align-items: center; gap: 12px; }
```

- [ ] **Step 4: 手工验证**

```bash
cd frontend && npm run dev
```

1. 新建状态打开弹窗 → 名称/域名留空 → 「基本信息」页签应是**红点**
2. 编辑一个源 → 点标题旁「另存为新源」→ 域名输入框**变可编辑且已清空**、标题变「另存为新源」
3. 填个新域名保存 → 列表里多出一条新源 → **原源不受影响**（回到列表搜一下原源名的规则还在）
4. 拿一个**声明为小说、实际是漫画**的源试跑 → 抽屉「提取结果」页的 note 里应有「去改类型」按钮 → 点击后**抽屉关闭、弹窗切到「基本信息」页签，且未发生任何写入**

- [ ] **Step 5: 提交**

```bash
git add frontend/src/components/SourceEditDialog.vue frontend/src/components/RuleDebugDrawer.vue
git commit -m "feat(frontend): 页签圆点反映必填校验，补「另存为新源」与「去改类型」入口"
```

---

# 收尾

## 收尾 · 批次 A 的验收（**必做**）

`checker` 判定收拢**必然造成星级迁移**，因为现在漫画源的 `content_ok` 实际走的是「文本长度 > 100」（图片兜底是死代码，见设计文档 1.4 与 15.1③）。收拢后空规则对图片源返回 `pass`，`content_ok` 从 `False` 变为 `True`/`None`，而 `calc_stars` 在 `None` 时回退静态判定 → 星级会变。

**没有这一步，就只能靠感觉说「改好了」。**

- [ ] **Step 1: 收拢前导出基线快照**

**必须在部署 Task 6 之前做。** 从库里直接读缓存，不必联网：

```bash
.venv/Scripts/python.exe -c "
import json, sqlite3
con = sqlite3.connect('data/sources.sqlite3')
con.row_factory = sqlite3.Row
rows = con.execute('SELECT source_url, quality_stars, health, toc_complete, content_ok FROM v_sources').fetchall()
out = {r['source_url']: {'stars': r['quality_stars'], 'health': r['health'],
                         'toc': r['toc_complete'], 'content': r['content_ok']} for r in rows}
json.dump(out, open('data/baseline_stars.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
print('已导出', len(out), '条到 data/baseline_stars.json')
"
```

> 若 `v_sources` 的列名与此不同，先跑 `sqlite3 data/sources.sqlite3 ".schema v_sources"` 确认。

- [ ] **Step 2: 部署 Task 6，触发一次全量重校验**

**主动安排时间窗**：3700 源，耗时长且对源站有请求量。

```bash
.venv/Scripts/python.exe -m cli.main check --refresh-cache
```

（具体子命令以 `cli/main.py` 的 `--refresh-cache` 所在的那个子命令为准，先跑 `--help` 确认。）

- [ ] **Step 3: diff 两份快照**

```bash
.venv/Scripts/python.exe -c "
import json, sqlite3
base = json.load(open('data/baseline_stars.json', encoding='utf-8'))
con = sqlite3.connect('data/sources.sqlite3'); con.row_factory = sqlite3.Row
now = {r['source_url']: {'stars': r['quality_stars'], 'health': r['health'],
                         'toc': r['toc_complete'], 'content': r['content_ok']}
       for r in con.execute('SELECT source_url, quality_stars, health, toc_complete, content_ok FROM v_sources')}
moved = [(u, base[u], now.get(u)) for u in base if now.get(u) and base[u] != now[u]]
print('总源数 %d，变化 %d' % (len(base), len(moved)))
for u, b, n in moved[:40]:
    print(' %-60s %s -> %s' % (u[:60], b, n))
"
```

- [ ] **Step 4: 逐项确认变化都落在预期内**

| 预期变化 | 方向 |
|---|---|
| 漫画源（实测正文为图片）的 `content_ok` 由 `False` → `True`/`None` | 修正误杀 |
| 空规则 + 无 webView 的文本源 `content_ok` 由 `True` → `False` | 修正误放 |
| 含 JS/XPath/`\|\|` 等语法的源由 `False` → `None`（`unknown`） | 修正误判（工具的能力边界） |

**出现上表之外的变化 → 停下排查，不要刷新缓存。** 排查手段：对单个源跑

```bash
.venv/Scripts/python.exe -c "
from core.checker import run_check
from core.loader import load_sources
srcs = [s for s in load_sources('data/candidates.json') if s.get('bookSourceUrl') == '<出问题的域名>']
for r in run_check(srcs, probe_depth=3):
    print(r.url, r.health, r.quality_stars, r.toc_complete, r.content_ok, r.content_fail_reason)
"
```

## 收尾 · 端到端人工验收

完整清单在设计文档 §13.1（15 条）。**下面这几条是本次改动最容易被做漏的**，务必逐条走：

| # | 操作 | 预期 |
|---|---|---|
| 1 | 拿一个**已知可用的漫画源**试跑 | content 步**绿**；「提取结果」能看到图片 URL 列表；「命中源码」能看到选中的 DOM 块 |
| 2 | 拿一个 **content 规则为空的小说源**试跑 | content 步**红**，reason 写出「Legado 会把章节链接当作正文」 |
| 3 | 拿一个 **content 规则含 `<js>` 的源**试跑 | content 步**灰**，reason 写出「Rhino」，**不是红** |
| 4 | 拿一个正文只有几十字的源试跑 | content 步**绿 + 黄点**，「提取结果」里能看到那几十个字 |
| 5 | 找一个**必须带 Referer 才出正文**的源 | 试跑能抓到正文（验 header 透传真的生效了） |
| 6 | 在一个漫画源上试跑，看 content 步的 note | 应出现「实测为图片，与声明的 📖小说 不一致」一类的提示 |

## 收尾 · 回退须知

- 每个 Task 一次提交，可单独 revert。
- **`core/checker.py` 那个提交（Task 6）是唯一有回归风险的**：影响全部源的批量校验。回退后试跑与批量校验会恢复「同源不同判」，但试跑调试功能不受影响。
- **回退 Task 6 时必须一并回退 `CACHE_VERSION`**（6 改回 5）。否则新旧两种缓存会被反复作废，每次校验都变成全量重跑。
- 本次**不涉及任何批量写源数据的操作**（类型判定改造已推迟到二期），因此回退不需要数据备份。

## 收尾 · 已知且接受的遗留

- **类型判定的全部改造已推迟**（设计文档 §15.3）。`reclassify.py` 一行未动，「判不出音频/下载源」「判据系统性偏小说」「`image_rule` 死分支」全部保留。
- **取值规则末段语义差异**未对齐（设计文档 §7.3）：`content: "id.content"` 这类「末段写裸选择器」的规则，我们给出正文、Legado 返回空。属于**误放**方向，需独立评估与灰度。
- **`@html:` 前缀是自创的**（设计文档 §16.2 第 6 条），本次只是把它归入 `unknown` 检测之外，未移除。


