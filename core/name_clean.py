# -*- coding: utf-8 -*-
"""书源名称清洗：纯规则，不碰数据库，不碰 source_url。

调用方负责 dry-run / 人工确认 / 写库；本模块只提供
clean_source_name(name, url, existing_names=None)。

设计约束（决策见 lessons 第三十五节）：
- source_url 是源身份，任何情况下都不改；这里只动展示名。
- 品牌数字要保护：52书库 / SF轻小说 / PO18 / PO18文学 不能被去尾部数字误伤。
- 置信度只用于预览默认勾选；低置信度必须人确认。
- 规则作用顺序固定；reasons 记录每一步，便于报告与回归测试。
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Dict, Iterable, List, Optional

from core.dups import host_of

PROTECTED_EMOJI = set("🎧🎨📖📥📚🎵🎬📺🔞")

_URL_RE = re.compile(r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'*+,;=%]+", re.I)

_DOMAIN_RE = re.compile(
    r"(?<![\w.-])(?:[a-z0-9-]+\.)+"
    r"(?:com\.cn|net\.cn|org\.cn|com|cn|net|org|info|cc|tv|me|io|xyz|top|vip|la|us|uk|co|site|club|online|app|pro|link|art|fun|store|shop|wang|xin|ren|mobi|asia|biz|name|tw|hk|jp|kr|de|fr|ru|in|eu|ws|fm|im|li|gd|gg|to|at|be|ch|it|nl|se|no|dk|fi|pl|cz|es|pt|gr)"
    r"(?::\d+)?(?:/[^\s，,、;；|（）()\[\]【】<>《》]*)?",
    re.I,
)
_BRACKET_TECH_RE = re.compile(
    r"[（(\[【][^）)\]】]{0,80}?(?:api|自写|导入|兼容版|精校|转载|转)[^）)\]】]{0,80}?[）)\]】]",
    re.I,
)
_TECH_WORD_RE = re.compile(r"(?<![A-Za-z])api(?![A-Za-z])|vip兼容版", re.I)

_VERSION_RE = re.compile(r"[（(]?(?<!\d)v?\d+(?:\.\d+)+(?:\s*vip兼容版)?[）)]?", re.I)
_HASH_TAIL_RE = re.compile(r"(?<![A-Za-z])[#＃]{1,2}[^\s#]{1,24}$")
_TRAIL_DIGITS_RE = re.compile(
    r"^(?P<base>.+?)(?P<sep>[^\w\s])?(?P<digits>\d{1,4}(?:\.\d+)?)$")
_EMPTY_BRACKET_RE = re.compile(r"[（(\[【]\s*[）)\]】]")
_EDGE_TRIM = " \t\r\n~-–—_=+·•.。，,、;；!！?？:：'\"“”‘’|/\\"


def _dedupe(items):
    out = []
    for item in items:
        if item not in out:
            out.append(item)
    return out


def _is_decor(ch: str) -> bool:
    return ch in _EDGE_TRIM or (
        unicodedata.category(ch) in ("So", "Sk", "Sm") and ch not in PROTECTED_EMOJI)


def _trim_edge_symbols(text: str):
    reasons = []
    while text:
        if _is_decor(text[0]):
            text = text[1:].lstrip()
            reasons.append("strip_decor")
            continue
        break
    while text:
        if _is_decor(text[-1]):
            text = text[:-1].rstrip()
            reasons.append("strip_decor")
            continue
        break
    return text, reasons


def _domain_main(url: str) -> str:
    host = host_of(url)
    if not host:
        return ""
    if re.fullmatch(r"[0-9.]+", host):
        return host
    for prefix in ("www.", "m.", "wap.", "api.", "www7."):
        if host.startswith(prefix) and len(host) > len(prefix):
            host = host[len(prefix):]
            break
    return host.split(".", 1)[0].strip()


#: 清洗原因码 → 中文。**唯一一份**：预演接口把它整表下发给前端
#: （与 `/sources/tags/meta` 下发标签枚举同一个道理——枚举只在这里定义，
#: 前端不另抄一份）。键与 `clean_source_name` 往 `reasons` 里塞的值一一对应。
REASON_LABELS = {
    "nfkc": "全角/兼容字符归一",
    "normalize_space": "首尾与连续空白归一",
    "remove_tech_marker": "去掉技术后缀（api / 兼容版 等）",
    "remove_url": "去掉名字里的网址",
    "remove_domain": "去掉名字里的域名",
    "remove_version": "去掉版本号",
    "remove_copy_marker": "去掉转载/复制标记",
    "strip_decor": "去掉首尾装饰符号",
    "remove_trailing_number": "去掉尾部数字",
    "remove_empty_bracket": "去掉空括号",
    "fallback_domain": "名字为空/过短 → 回退域名主名",
    "no_safe_fallback": "名字不可用且无安全回退",
}


def clean_source_name(name: Any, url: str = "",
                      existing_names: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    """清洗单个展示名；返回 {name, confidence, reasons, changed}。"""
    original = str(name or "")
    reasons: List[str] = []
    confidences: List[float] = []
    text = original

    norm = unicodedata.normalize("NFKC", text)
    if norm != text:
        reasons.append("nfkc")
        confidences.append(0.98)
    collapsed = re.sub(r"\s+", " ", norm).strip()
    if collapsed != norm:
        reasons.append("normalize_space")
        confidences.append(0.98)
    text = collapsed

    new = _BRACKET_TECH_RE.sub(" ", text)
    if new != text:
        reasons.append("remove_tech_marker"); confidences.append(0.95); text = new
    new = _URL_RE.sub(" ", text)
    if new != text:
        reasons.append("remove_url"); confidences.append(0.98); text = new
    new = _DOMAIN_RE.sub(" ", text)
    if new != text:
        reasons.append("remove_domain"); confidences.append(0.95); text = new
    new = _TECH_WORD_RE.sub(" ", text)
    if new != text:
        reasons.append("remove_tech_marker"); confidences.append(0.95); text = new
    new = _VERSION_RE.sub(" ", text)
    if new != text:
        reasons.append("remove_version"); confidences.append(0.9); text = new

    text = re.sub(r"\s+", " ", text).strip()
    new = _HASH_TAIL_RE.sub("", text)
    if new != text:
        reasons.append("remove_copy_marker"); confidences.append(0.95); text = new
    # ## 尾标被删后会留下一个孤立的 #（如 R 369小说网#）；只有前一个字符不是
    # ASCII 字母/数字时才清，保护 C# / C#.NET 这类名字
    if len(text) > 1 and text[-1] in "#＃" and not (text[-2].isascii() and text[-2].isalnum()):
        text = text[:-1].rstrip()
        reasons.append("remove_copy_marker"); confidences.append(0.9)

    text = re.sub(r"\s+", " ", text).strip()
    match = _TRAIL_DIGITS_RE.match(text)
    if match and match.group("digits"):
        base = (match.group("base") or "").strip()
        sep = match.group("sep") or ""
        known = {str(n or "").strip() for n in (existing_names or ())}
        if base and (sep or base in known):
            text = base
            reasons.append("remove_trailing_number")
            confidences.append(0.8 if sep else 0.7)

    new = _EMPTY_BRACKET_RE.sub("", text)
    if new != text:
        reasons.append("remove_empty_bracket"); confidences.append(0.95); text = new
    text = re.sub(r"\s+", " ", text).strip()
    text, trimmed = _trim_edge_symbols(text)
    if trimmed:
        reasons.append("strip_decor"); confidences.append(0.85)

    if len(text) < 2:
        fallback = _domain_main(url)
        if len(fallback) >= 2:
            text = fallback
            reasons.append("fallback_domain")
            confidences.append(0.5)
        else:
            text = original.strip() or text
            reasons.append("no_safe_fallback")
            confidences.append(0.3)

    changed = text != original
    confidence = min(confidences) if confidences else 1.0
    if not changed and "no_safe_fallback" not in reasons:
        confidence = 1.0
    return {"name": text, "confidence": round(confidence, 2),
            "reasons": _dedupe(reasons), "changed": changed}


def clean_sources(sources, existing_names=None):
    """对一批书源 dict 做只读建议，返回旧名 → 新名的清单。"""
    items = [s for s in (sources or []) if isinstance(s, dict)]
    names = existing_names
    if names is None:
        names = {str(s.get("bookSourceName", "") or "") for s in items}
    out = []
    for src in items:
        url = str(src.get("bookSourceUrl", "") or "")
        old = str(src.get("bookSourceName", "") or "")
        res = clean_source_name(old, url, names)
        out.append({
            "url": url,
            "old_name": old,
            "new_name": res["name"],
            "confidence": res["confidence"],
            "reasons": res["reasons"],
            "changed": res["changed"],
        })
    return out
