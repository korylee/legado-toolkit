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

from core.models import BookSourceRecord, Health, BOOK_SOURCE_TYPE_NAMES, HEALTH_NAMES
from core.tags import extract_user_tags_from_group, merge_group, parse_group_tags

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
    """返回可进系统分组的质量标签；规则完整是系统标签。"""
    tags = [t for t in rec.quality_tags if t in ("规则完整",)]
    return ",".join(tags) if tags else ""

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
        # 系统标签 = 类型 + 健康状态 + 规则完整
        system_group = group_title(rec.source_type, rec.health, rec.quality_stars)
        system_quality = quality_tags_str(rec)
        if system_quality:
            system_group = merge_group(parse_group_tags(system_group), parse_group_tags(system_quality))
        # 用户标签 = 原分组里所有非系统标签，永久保留，不再白名单过滤
        user_tags = extract_user_tags_from_group(rec.group)
        new_rec["bookSourceGroup"] = merge_group(parse_group_tags(system_group), user_tags)
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