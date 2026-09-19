# -*- coding: utf-8 -*-
"""界面文案机检：**术语表与标点规范的唯一事实来源**。

它解决什么问题
--------------
文案的真实问题不是错别字，是同一件事有四种叫法（回放 / 重放 / 试跑 / 重跑），
以及内部黑话直出（口径、降级、补抓）。这两类靠人自觉必然漂——本仓库所有稳定
下来的约定都做成了机械可验证的（AGENTS #7 / #8 / #10 是同一条原则的三次应用，
这里是第四次）。所以规则写成数据放这里，由 ``tests/test_copy.py`` 挂进
unittest，新写进代码的说法当场被拦住。

**注释不是文案。** 本仓库中文注释极多，注释里的「回放」「口径」是写给改代码的
人看的（那里精确优先，见 AGENTS #10 的分工表），**不在检查范围**。提取时先屏蔽
注释——这是本模块最要紧的一件事：不屏蔽就是满屏误报，工具立刻失去信用。

用法
----
    python -m tools.check_copy                    # 全量报告（按文件分组）
    python -m tools.check_copy --errors           # 只看必改项（= 测试口径）
    python -m tools.check_copy --update-baseline  # 清理后收窄基线

术语表要改就往 ``TERM_RULES`` 里改一条，**别在别处再抄一份**（AGENTS #10）。
"""

from __future__ import annotations

import ast
import hashlib
import json
import pathlib
import re
import sys
from typing import Dict, List, Tuple

# ------------------------------------------------------------------ 术语表

#: 术语表：键是要禁掉的词，值是（该说成什么，为什么）。
#:
#: 为什么每个词都要写「为什么」：这些词单看都不像问题，**不写理由就会被改回去**。
#: 界面总称统一为「调试」的依据是上游 App 自己就叫调试
#: （`legado-with-MD3` strings.xml：``debug_source``=调试源、``debug``=调试、
#: ``debug_finished``=调试结束），我们不该另造词。
TERM_RULES: Dict[str, Tuple[str, str]] = {
    "回放": ("调试", "上游 App 自己就叫「调试」；本地那层叫「本地调试」"),
    "重放": ("重新调试", "动作说「重新调试本页」，「重放」是引擎内部说法"),
    "试跑": ("调试", "我们自造的词，App 与用户都不这么说"),
    "补抓": ("抓取", "说「我们抓到的页面」，不暴露内部机制"),
    "口径": ("判定标准", "内部黑话"),
    "降级": ("暂按「命中」计", "说结果，不说过程"),
    "指纹": ("特征", "「指纹」是内部算法名"),
    "复检": ("复查", "与「复核」并成一个词"),
    "复核": ("复查", "与「复检」并成一个词"),
}

#: 词边界护栏：中文不写空格，子串匹配会撞上「跨词巧合」。实测
#: 「直接调试跑的是…」里的「试跑」其实是「调试 + 跑」，不是我们要抓的那个词。
#: 键是术语，值是**紧邻前一个字符**——等于它就跳过这一处匹配。按需加，别预加。
_TERM_GUARDS = {"试跑": "调"}

#: 永久例外标记：必须**逐字保留**的串（换词表的键、协议字面量等）在行尾写
#: ``# copy-ok: 理由``。这类例外**不进基线**——基线是「只减不增」的欠账清单，
#: 往里塞永久条目等于把闸门关掉（AGENTS #18）。
COPY_OK = "copy-ok"

#: 中文后面跟半角冒号（``失败: xxx``）。中文文案用全角「：」。
_HALF_COLON = re.compile(r"[\u4e00-\u9fa5]:(?=\s|$)")
#: 半角括号里包着中文（``(待交叉验证)``）。中文文案用全角「（）」。
_HALF_PAREN = re.compile(r"\([^()]*[\u4e00-\u9fa5][^()]*\)")
#: 长括号解释：括号里 6 字以上，且整条文案超过 24 字（括号里多半在复述机制）。
_PAREN_LONG = re.compile(r"（[^）]{6,}）")
#: 一行装不下的一句话（超过这个长度就该断句）
LONG_TEXT = 40


def _term_hits(text: str, term: str) -> int:
    """术语在文案里出现的次数，**排除跨词巧合**（见 ``_TERM_GUARDS``）。"""
    guard = _TERM_GUARDS.get(term, "")
    count, idx = 0, text.find(term)
    while idx >= 0:
        if not guard or text[idx - 1:idx] != guard:
            count += 1
        idx = text.find(term, idx + len(term))
    return count


def check_text(text: str) -> List[Tuple[str, str, str]]:
    """一条文案 → ``[(level, rule, why)]``。level 为 error 时是测试口径。"""
    hits: List[Tuple[str, str, str]] = []
    for term, (better, why) in TERM_RULES.items():
        if _term_hits(text, term):
            hits.append(("error", "术语·" + term, "%s（→ %s）" % (why, better)))
    if _HALF_COLON.search(text):
        hits.append(("error", "标点·半角冒号", "中文后用全角「：」"))
    if _HALF_PAREN.search(text):
        hits.append(("error", "标点·半角括号", "中文用全角「（）」"))
    # 句法类只对「一句话」下判断。选择器 / JSON / URL 不长在句法上，
    # 对它们报「太长」「有破折号」是噪声——噪声多了工具就没人看了
    if is_prose(text):
        if "——" in text:
            hits.append(("warn", "句法·破折号", "破折号前后是两件事，断成两句"))
        if len(text) > LONG_TEXT:
            hits.append(("warn", "句法·长句", "一句话一件事，超过一行就断句"))
        if len(text) > 24 and _PAREN_LONG.search(text):
            hits.append(("warn", "句法·括号解释", "括号里别复述机制"))
    return hits


# ------------------------------------------------------------------ 提取

_HAN = re.compile(r"[\u4e00-\u9fa5]")


def _blank_js_comments(text: str) -> str:
    """把 JS/Vue 注释内容替换成空格。

    **保留长度与换行**，这样偏移量仍然能用来算行号——报错要指到行，
    指错了比不指更糟。字符串字面量里的 ``//``（如 ``https://``）在引号态内，
    不会被当成注释起点。
    """
    chars = list(text)
    i, n, quote = 0, len(text), None
    while i < n:
        c = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if quote is None:
            if c == "/" and nxt == "/":
                end = text.find("\n", i)
                end = n if end < 0 else end
                for k in range(i, end):
                    if chars[k] != "\n":
                        chars[k] = " "
                i = end
                continue
            if c == "/" and nxt == "*":
                end = text.find("*/", i + 2)
                end = n if end < 0 else end + 2
                for k in range(i, end):
                    if chars[k] != "\n":
                        chars[k] = " "
                i = end
                continue
            if c in "\"'`":
                quote = c
            i += 1
            continue
        # 引号态内：跳过转义，直到收尾引号
        if c == "\\":
            i += 2
            continue
        if c == quote:
            quote = None
        i += 1
    return "".join(chars)


def _blank_html_comments(text: str) -> str:
    return re.sub(r"<!--.*?-->",
                  lambda m: re.sub(r"[^\n]", " ", m.group(0)), text, flags=re.S)


#: 浏览器控制台里的输出：**给开发者看的**，与注释同理，不是用户文案。
#: 它有自己的惯例（``[模块] 事件:`` 这种前缀用半角），不该按界面文案的标准管。
_CONSOLE_CALL = re.compile(r"console\.(?:log|warn|error|info|debug)\s*\(")


def _blank_console_lines(text: str) -> str:
    """整行屏蔽 ``console.*`` 调用（按行处理，行尾换行与偏移量都不动）。"""
    return "\n".join(" " * len(line) if _CONSOLE_CALL.search(line) else line
                     for line in text.split("\n"))


_STR_DOUBLE = re.compile(r'"([^"\n]*)"')
_STR_SINGLE = re.compile(r"'([^'\n]*)'")
_TEXT_NODE = re.compile(r">([^<>{}]*[\u4e00-\u9fa5][^<>{}]*)<")
#: 看着像选择器 / JSON / URL / 模板占位的串：句法类提示对它们没有意义
_CODEY = re.compile(r"^\s*[\[{<]|://|@css:|@json:|@html:|^\s*[.#@]")


def is_prose(text: str) -> bool:
    """够不够「一句话」。中文字符太少的多半是选择器、JSON 或 URL。"""
    han = len(_HAN.findall(text))
    return han >= 4 and han >= len(text) * 0.2 and not _CODEY.search(text)


def _template_span(code: str) -> Tuple[int, int]:
    """``.vue`` 里 ``<template>`` 段的范围。文本节点**只在这里找**。

    整个文件一起找会把 ``<script>`` 里的箭头函数当成标签边界——实测
    ``.map((v) => String(v)…`` 整段被报成了文案。
    """
    start = code.find("<template")
    if start < 0:
        return 0, 0
    end = code.rfind("</template>")
    return start, (end + len("</template>")) if end > start else len(code)


def frontend_candidates(path: pathlib.Path) -> List[Tuple[int, int, str]]:
    """``.vue`` / ``.js`` 里用户看得到的字符串：字面量 + ``<template>`` 里的文本节点。

    返回 ``(起始行, 结束行, 文案)``——结束行是给 ``copy-ok`` 例外用的（多行字符串
    的标记可能写在收尾那行）。
    """
    raw = path.read_text(encoding="utf-8")
    code = _blank_console_lines(_blank_html_comments(_blank_js_comments(raw)))
    found: List[Tuple[int, int, str]] = []

    def add(match) -> None:
        # 取**捕获组**而不是整段匹配：整段带着引号或 ``>``/``<`` 边界，
        # 算长度时会把它们算进去（实测把 35 字的提示报成了「长句」）
        text = " ".join(match.group(1).split())
        if _HAN.search(text):
            start = raw.count("\n", 0, match.start(1)) + 1
            end = raw.count("\n", 0, match.end(1)) + 1
            found.append((start, end, text))

    for pattern in (_STR_DOUBLE, _STR_SINGLE):
        for m in pattern.finditer(code):
            add(m)
    start, end = _template_span(code)
    if end > start:
        for m in _TEXT_NODE.finditer(code, start, end):
            add(m)
    return found


def python_candidates(path: pathlib.Path) -> List[Tuple[int, int, str]]:
    """``.py`` 里用户看得到的字符串：**docstring 不算**（那是给改代码的人看的）。

    用 ``ast`` 而不是正则：本仓库的 Python 注释与文档字符串全是中文，
    正则分不出「文档」与「要显示给用户的话」。
    返回 ``(起始行, 结束行, 文案)``，与前端侧同形。
    """
    src = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []          # 语法错误的文件由测试流程管，文案检查不该抢这个角色
    doc_ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", []) or []
            first = body[0] if body else None
            if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                doc_ids.add(id(first.value))
    found: List[Tuple[int, int, str]] = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in doc_ids and _HAN.search(node.value)):
            end = getattr(node, "end_lineno", node.lineno) or node.lineno
            found.append((node.lineno, end, " ".join(node.value.split())))
    return found


#: 要扫的目录与后缀。``tools/`` 自己不扫（本模块的 docstring 里全是反面例子）。
TARGETS = (("frontend/src", (".vue", ".js")),
           ("core", (".py",)),
           ("backend", (".py",)),
           ("cli", (".py",)))


def has_copy_ok(path: pathlib.Path, start: int, end: int) -> bool:
    """``start..end`` 这几行里有没有 ``copy-ok`` 例外标记。

    **按原文查，不按屏蔽后的代码查**：JS/Vue 侧标记写成行尾注释，而注释在提取前
    已经被抹成空格了——在屏蔽后的文本里找它永远是假。
    范围取**字符串自己跨的行**（不是"本行 + 上一行"）：后者会把紧挨着的无关字符串
    一起放过，实测就是这么漏的。
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return False
    for probe in range(max(1, start), min(end, len(lines)) + 1):
        if COPY_OK in lines[probe - 1]:
            return True
    return False


def scan(root: pathlib.Path) -> List[dict]:
    """全量扫描，返回违规列表（未去重、未过滤基线）。"""
    out: List[dict] = []
    for rel, exts in TARGETS:
        for path in sorted((root / rel).rglob("*")):
            if path.suffix not in exts or not path.is_file():
                continue
            reader = python_candidates if path.suffix == ".py" else frontend_candidates
            try:
                candidates = reader(path)
            except (OSError, UnicodeDecodeError) as e:
                print("跳过 %s（读取失败：%s）" % (path, e), file=sys.stderr)
                continue
            for line, end, text in candidates:
                if has_copy_ok(path, line, end):
                    continue
                for level, rule, why in check_text(text):
                    out.append({
                        "file": str(path.relative_to(root)).replace("\\", "/"),
                        "line": line, "level": level, "rule": rule,
                        "why": why, "text": text,
                    })
    return out


# ------------------------------------------------------------------ 基线

BASELINE = pathlib.Path(__file__).with_name("copy_baseline.json")


def key_of(v: dict) -> str:
    """基线键 = 文件 + 规则 + 文案哈希。

    **不带行号**：行号会随无关的改动漂移，那样基线每周都要重生成，
    而每次重生成都会把新引入的违规顺手洗白。文案一变键就变，
    逼着人重新判断一次——这是刻意的。
    """
    digest = hashlib.sha1(v["text"].encode("utf-8")).hexdigest()[:12]
    return "%s|%s|%s" % (v["file"], v["rule"], digest)


def load_baseline() -> set:
    if not BASELINE.exists():
        return set()
    data = json.loads(BASELINE.read_text(encoding="utf-8"))
    return set(data.get("entries", []))


def save_baseline(keys) -> None:
    payload = {
        "note": ("已存在的违规清单：**只减不增**。清理一批就跑 "
                 "python -m tools.check_copy --update-baseline 收窄。"
                 "新增条目意味着把新违规洗白，别这么干。"),
        "entries": sorted(keys),
    }
    BASELINE.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8", newline="\n")


# ------------------------------------------------------------------ 入口

def main(argv: List[str]) -> int:
    root = pathlib.Path(__file__).resolve().parent.parent
    only_errors = "--errors" in argv
    update = "--update-baseline" in argv

    hits = scan(root)
    if only_errors:
        hits = [v for v in hits if v["level"] == "error"]
    base = load_baseline()
    fresh = [v for v in hits if key_of(v) not in base]

    if update:
        # 基线只记 **error** 档：闸门管的就是这一档，账本与口径必须一致。
        # warn 档（长句、破折号）需要人判断，写进基线只会让文件常年对不上。
        keys = {key_of(v) for v in scan(root) if v["level"] == "error"}
        save_baseline(keys)
        print("基线已更新：%d 条（仅 error 档）" % len(keys))
        return 0

    by_file: Dict[str, List[dict]] = {}
    for v in fresh:
        by_file.setdefault(v["file"], []).append(v)
    for file, items in sorted(by_file.items()):
        print("\n%s" % file)
        for v in sorted(items, key=lambda x: x["line"]):
            print("  %5d  [%s] %-14s %s" % (v["line"], v["level"], v["rule"], v["why"]))
            print("         %s" % v["text"][:100])
    stale = base - {key_of(v) for v in hits}
    print("\n新增违规 %d 条（基线内 %d 条）" % (len(fresh), len(hits) - len(fresh)))
    if stale:
        print("基线里有 %d 条已不再命中，可以 --update-baseline 收窄" % len(stale))
    return 1 if fresh else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
