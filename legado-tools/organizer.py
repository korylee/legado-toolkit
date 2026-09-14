# -*- coding: utf-8 -*-
"""
分组整理：按内容类型 + 健康状态重建清晰分组。

原文件里 4395 个源散落在数百个混乱分组（含"失效""全部来自论坛"等噪音），
本模块把每个源重新归入形如「📖小说★★★★★」的清晰分组（类型与星级合为同一个标记），
同时保留原始分组信息到 bookSourceComment 尾部以便回溯。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

from models import BookSourceRecord, Health, BOOK_SOURCE_TYPE_NAMES, HEALTH_NAMES

# 分组排序：类型优先，健康次之，星级再之
TYPE_ORDER = {0: 0, 2: 1, 1: 2, 3: 3, 4: 4}
HEALTH_ORDER = {
    Health.OK: 0,
    Health.AUTH: 1,
    Health.GFW: 2,
    Health.NO_SEARCH: 3,
    Health.TIMEOUT: 4,
    Health.DEAD: 5,
    Health.ERROR: 6,
    Health.SKIPPED: 7,
}


STATUS_GROUP_NAMES = {
    Health.OK: "可用", Health.AUTH: "待验证", Health.GFW: "需代理复检",
    Health.DEAD: "已失效", Health.NO_SEARCH: "待验证", Health.TIMEOUT: "待验证",
    Health.ERROR: "待验证", Health.SKIPPED: "待验证",
}


def infer_health_from_group(group: str) -> str:
    """从旧分组迁移可确认的健康状态；无法确认时保守归为待验证。"""
    value = str(group or "")
    if "需翻墙" in value or "被墙" in value or "🌐" in value:
        return Health.GFW
    if "失效" in value or "❌" in value:
        return Health.DEAD
    if "需验证" in value or "需登录" in value or "🔒" in value:
        return Health.AUTH
    if "✅" in value or "可用" in value:
        return Health.OK
    return Health.AUTH


def group_title(source_type: int, health: str, stars: int = 0, style: str = "status") -> str:
    """生成面向使用的分组；默认不把星级和临时检测证据写入分组。"""
    type_name = BOOK_SOURCE_TYPE_NAMES.get(source_type, "❓未知")
    if style != "status":
        health_name = HEALTH_NAMES.get(health, health)
        if stars and health == Health.OK:
            star_str = "★" * min(stars, 5) + "☆" * (5 - min(stars, 5))
            return f"{type_name}{star_str}"
        return f"{type_name},{health_name}"
    return f"{type_name},{STATUS_GROUP_NAMES.get(health, '待验证')}"


def _clean_comment(comment: str) -> str:
    """移除测试命中与历史原分组，保留用户手写说明。"""
    lines = []
    for line in str(comment or "").splitlines():
        if "[原分组]" in line:
            continue
        if line.strip().lower().startswith("// error:"):
            continue
        line = re.sub(r"命中《[^》]*》[，,]?", "", line)
        line = re.sub(r"[，,]\s*$", "", line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def quality_tags_str(rec: BookSourceRecord) -> str:
    """把质量标签拼成可进 group 的短标签列表（不含星级）。"""
    tags = [t for t in rec.quality_tags if t != "原创" or True]
    return ",".join(tags) if tags else ""


# 分组重建时保留的标签白名单（规范化规则：子串模式 -> 规范标签）。
# 成人向（18禁/H漫/🔞/成人/涩）统一归一为 R18；其余按语义原样规范化。
# 优先级：靠前的规则先匹配（如 r18 先于 18禁 等）。
PRESERVED_TAG_RULES: List[tuple] = [
    ("r18", "R18"), ("18禁", "R18"), ("18x", "R18"), ("成人", "R18"),
    ("h漫画", "R18"), ("h漫", "R18"), ("涩图", "R18"), ("涩漫", "R18"),
    ("🔞", "R18"),
    ("正版", "正版"),
    ("自制", "自制"), ("写源", "写源"),
    ("番茄", "番茄"), ("七猫", "七猫"), ("起点", "起点"), ("懒人", "懒人"),
    ("vpn", "VPN"), ("torrent", "Torrent"), ("精品", "精品"),
    ("韩漫", "韩漫"), ("日漫", "日漫"), ("耽漫", "耽美"), ("耽美", "耽美"),
    ("raw", "Raw"), ("pixiv", "Pixiv"), ("图源", "图源"),
    # 仅发现：搜索接口不可用，源仅供发现/直达访问（add_source.py 自动标记）
    ("仅发现", "仅发现"),
]

# 原分组标签的分隔符（逗号/分号/空格/竖线，全角半角）
_TAG_SEPARATORS_RE = re.compile(r"[,，;；\s|+/&]+")


def _preserved_group_tags(rec: BookSourceRecord) -> str:
    """从原始分组提取需保留的标签（白名单规范化，成人向归一为 R18）。

    原始分组按分隔符拆成片段，每个片段小写后逐一匹配 PRESERVED_TAG_RULES，
    命中则追加规范化标签；去重保序后以逗号拼接返回。
    """
    orig = rec.group or ""
    matched: List[str] = []
    for seg in _TAG_SEPARATORS_RE.split(orig):
        seg_lower = seg.lower()
        for pattern, canonical in PRESERVED_TAG_RULES:
            if pattern in seg_lower:
                if canonical not in matched:
                    matched.append(canonical)
                break  # 一个片段只产出一个规范标签（优先靠前的规则）
    return ",".join(matched)


def sort_records(records: List[BookSourceRecord]) -> List[BookSourceRecord]:
    """按类型 + 健康状态 + 星级降序 + 名称排序。"""
    return sorted(
        records,
        key=lambda r: (
            TYPE_ORDER.get(r.source_type, 9),
            HEALTH_ORDER.get(r.health, 9),
            -r.quality_stars,
            r.name,
        ),
    )


def organize_sources(
    records: List[BookSourceRecord],
    skip_disabled: bool = True,
    keep_original_group: bool = True,
    drop_dead: bool = False,
    keep_only_ok: bool = False,
) -> List[Dict[str, Any]]:
    """
    重建分组并返回可直接写回 Legado 的 JSON 列表。

    :param skip_disabled: 是否排除 enabled=false 的源
    :param keep_original_group: 是否在 comment 里保留原始分组
    :param drop_dead: 是否剔除「失效」源（Health.DEAD）。
        注意：需验证(AUTH)/被墙(GFW) 源不剔除——可能只是暂时反爬或需翻墙。
    :param keep_only_ok: 是否只保留「✅可用」(Health.OK) 源（精简导入版）。
    """
    if skip_disabled:
        records = [r for r in records if r.enabled]
    if drop_dead:
        records = [r for r in records if r.health != Health.DEAD]
    if keep_only_ok:
        records = [r for r in records if r.health == Health.OK]
    records = sort_records(records)

    result: List[Dict[str, Any]] = []
    for rec in records:
        new_rec = dict(rec.raw)  # 浅拷贝，保留全部字段
        # 主分组：类型 + 生命周期状态；星级只在报告中展示
        new_rec["bookSourceGroup"] = group_title(rec.source_type, rec.health, rec.quality_stars)
        # 追加质量标签分组（逗号分隔实现对同一源的多个标签分组覆盖）
        tags = quality_tags_str(rec)
        # 保留原分组中的特殊标记（R18/18禁 等，防止整理后丢失）
        preserved = ",".join(
            tag for tag in _preserved_group_tags(rec).split(",")
            if tag in ("R18", "正版", "原创", "仅发现")
        )
        # 质量标签不作为日常分组：规则完整、平台来源等可变证据只进报告。
        tags = ",".join(t for t in tags.split(",") if t in ("原创",))
        extra = [t for t in (preserved, tags) if t]
        if extra:
            new_rec["bookSourceGroup"] += "," + ",".join(extra)
        if keep_original_group:
            new_rec["bookSourceComment"] = _clean_comment(rec.raw.get("bookSourceComment", ""))
        result.append(new_rec)
    return result


def summarize_grouping(records: List[BookSourceRecord]) -> List[Dict[str, Any]]:
    """生成分组统计（供报告使用）。"""
    from collections import Counter
    counter: Counter = Counter()
    for r in records:
        if not r.enabled:
            continue
        counter[group_title(r.source_type, r.health, r.quality_stars)] += 1
    summary = [{"group": g, "count": c} for g, c in counter.most_common()]
    return summary
