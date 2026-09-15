# -*- coding: utf-8 -*-
"""标签规范化、系统标签判定、系统/用户标签拆分。"""

from __future__ import annotations

import re
from typing import Iterable, List, Sequence, Tuple

# 类型标签与 Legado 的 bookSourceType 一一对应（0/1/2/3），不含任何未定义类型
SYSTEM_TYPE_TAGS = {"📖小说", "🎧听书", "🎨漫画", "📥下载"}
SYSTEM_STATUS_TAGS = {"可用", "待验证", "已失效", "需代理复检"}
SYSTEM_QUALITY_TAGS = {"规则完整"}
SYSTEM_TAGS = SYSTEM_TYPE_TAGS | SYSTEM_STATUS_TAGS | SYSTEM_QUALITY_TAGS

# 用户标签别名映射：新源带进来的别名先归一，再按已有标签过滤。
# key 使用小写，值使用标准标签名。按需继续补充。
USER_TAG_ALIASES = {
    "h漫": "R18",
    "h漫画": "R18",
    "18禁": "R18",
    "18x": "R18",
    "成人": "R18",
    "涩图": "R18",
    "涩漫": "R18",
    "🔞": "R18",
    "精品排版": "精排",
    "排版好": "精排",
}

# 用户标签分隔符：逗号、分号、竖线
_USER_SPLIT_RE = re.compile(r"[,，;；|]+")
# 历史分组还可能用空格、斜杠、加号、& 作为分隔符
_LEGACY_SPLIT_RE = re.compile(r"[,，;；\s|+/&]+")
_STAR_RE = re.compile(r"[★☆]+")


def _clean(tag) -> str:
    return str(tag or "").strip().strip(",，;；|")


def _dedupe(items: Iterable[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for raw in items:
        tag = _clean(raw)
        if not tag or tag in seen:
            continue
        seen.add(tag)
        out.append(tag)
    return out


def normalize_tags(value) -> List[str]:
    """规范化用户标签输入，保序去重。"""
    parts: List[str] = []
    if isinstance(value, (list, tuple, set)):
        for item in value:
            parts.extend(_USER_SPLIT_RE.split(str(item or "")))
    else:
        parts.extend(_USER_SPLIT_RE.split(str(value or "")))
    return _dedupe(parts)


def canonical_tag(tag: str) -> str:
    """把单个标签按别名表归一。"""
    clean = _clean(tag)
    if not clean:
        return ""
    return USER_TAG_ALIASES.get(clean.lower(), clean)


def canonical_tags(value) -> List[str]:
    """把标签列表规范加别名映射，保序去重。"""
    return _dedupe(canonical_tag(tag) for tag in normalize_tags(value))


def parse_group_tags(group: str) -> List[str]:
    """解析历史分组字符串，兼容空格/斜杠/星标等旧格式。"""
    return _dedupe(_LEGACY_SPLIT_RE.split(str(group or "")))


def is_system_tag(tag: str) -> bool:
    return _clean(tag) in SYSTEM_TAGS


def _is_legacy_system_segment(segment: str) -> bool:
    tag = _clean(segment)
    if not tag:
        return True
    if tag in SYSTEM_TAGS:
        return True
    stripped = _STAR_RE.sub("", tag).strip()
    stripped = stripped.strip("✅❌🌐🔒❓")
    if stripped in SYSTEM_TAGS:
        return True
    if stripped in {"失效", "需验证", "需登录", "需翻墙", "被墙"}:
        return True
    if any(tag.startswith(prefix) for prefix in SYSTEM_TYPE_TAGS):
        return True
    if tag.startswith(("✅", "❌", "🌐", "🔒", "❓")):
        return True
    if all(ch in "✅❌🌐🔒❓★☆ " for ch in tag):
        return True
    return False


def split_system_user(tags) -> Tuple[List[str], List[str]]:
    """把标签列表拆成系统标签和用户标签。"""
    if isinstance(tags, str):
        tags = parse_group_tags(tags)
    ordered = _dedupe(tags or [])
    system: List[str] = []
    user: List[str] = []
    for tag in ordered:
        if is_system_tag(tag):
            system.append(tag)
        else:
            user.append(tag)
    return system, user


def merge_group(system_tags, user_tags) -> str:
    """合并系统标签和用户标签，保序去重。"""
    ordered = _dedupe(list(normalize_tags(system_tags)) + list(normalize_tags(user_tags)))
    return ",".join(ordered)


def extract_user_tags_from_group(group: str) -> List[str]:
    """从历史分组中提取非系统标签，兼容旧白名单标签。"""
    out: List[str] = []
    for segment in _LEGACY_SPLIT_RE.split(str(group or "")):
        tag = _clean(segment)
        if not tag or tag.startswith("命中"):
            continue
        tag = canonical_tag(tag)
        if tag and not _is_legacy_system_segment(tag):
            out.append(tag)
    return _dedupe(out)


def normalize_group(group: str) -> str:
    """把任意历史分组规范化为系统标签在前、用户标签在后。"""
    system, user = split_system_user(parse_group_tags(group))
    return merge_group(system, user)

