# -*- coding: utf-8 -*-
"""
诊断报告生成：对书源集合输出可读的统计与问题清单。

报告内容：
- 总量与类型分布
- 分组混乱度（原分组数、命名噪音）
- 健康状态分布（校验后）
- 失效标签统计
- 无搜索规则 / 无目录规则 / 疑似重复源
- 建议启用的源清单
"""

from __future__ import annotations

from collections import Counter
from typing import Dict, List

from core.models import (BookSourceRecord, Health, DEAD_TAG_PATTERNS,
                         BOOK_SOURCE_TYPE_NAMES, HEALTH_NAMES)


def build_report(
    records: List[BookSourceRecord],
    source_path: str = "",
    include_duplicates: bool = True,
) -> str:
    """生成 Markdown 格式诊断报告。"""
    total = len(records)
    enabled = [r for r in records if r.enabled]
    disabled = total - len(enabled)
    lines: List[str] = []
    lines.append("# Legado 书源诊断报告")
    lines.append("")
    if source_path:
        lines.append(f"- 来源文件：`{source_path}`")
    lines.append(f"- 书源总数：**{total}**（启用 {len(enabled)} / 停用 {disabled}）")
    lines.append("")

    # ---- 类型分布
    lines.append("## 一、内容类型分布")
    lines.append("")
    lines.append("| 类型 | 数量 | 占比 |")
    lines.append("|------|-----:|-----:|")
    type_counter = Counter(r.source_type for r in enabled)
    # 类型名的**唯一来源**是 models 那份，别在这里抄第二份（这里原来抄了一份，
    # 靠注释「保持一致」维持，那正是会漂的写法）。兜底 `.get(t, t)`：
    # Legado 没有 4，认不出就显示原始数值
    type_names = BOOK_SOURCE_TYPE_NAMES
    for t in sorted(type_counter, key=lambda x: -type_counter[x]):
        c = type_counter[t]
        lines.append(f"| {type_names.get(t, t)} | {c} | {c/len(enabled)*100:.1f}% |")
    lines.append("")

    # ---- 健康状态分布
    health_ok = [r for r in enabled if r.health == Health.OK]
    # ---- 星级分布（校验后）：覆盖全部源（0★=未评级/不可达，含失效/需登录等）
    starred = list(enabled)
    if starred:
        lines.append("## 二、星级分布（优质度检测）")
        lines.append("")
        lines.append("| 星级 | 数量 | 说明 |")
        lines.append("|------|-----:|------|")
        star_counts = Counter(r.quality_stars for r in starred)
        star_desc = {
            5: "命中 + 目录完整 + 正文可用",
            4: "命中 + 目录完整（正文未达标）",
            3: "命中 或 规则完整（弱证据档）",
            2: "可达 + 搜索连通（无规则证据）",
            1: "可达（可访问，但搜索未验证）",
            0: "未评级/不可达",
        }
        # 本次统计中的最大验证深度：≥2 的源其目录/正文维度为实测，其余为静态规则判定
        depth_max = max((r.probe_depth for r in enabled if r.probe_depth), default=1)
        lines.append("")
        lines.append(f"> 星级为阶梯规则（1★可达 → 2★搜索连通 → 3★命中或规则完整 → 4★目录完整 → 5★正文可用）。")
        lines.append(f"> 命中源按实测判定（无法验证的维度回退静态规则）；未命中但规则完整的源最高 3★。")
        lines.append(f"> 本次最高验证深度 **{depth_max}**（--probe-depth 3 时命中源为目录+正文实测；浅探测为静态规则判定）。")
        lines.append("")
        for s in sorted(star_counts, reverse=True):
            c = star_counts[s]
            lines.append(f"| {'★' * s + '☆' * (5 - s)} | {c} | {star_desc.get(s, '')} |")
        lines.append("")

        # ---- 深度验证异常源（仅 --probe-depth 2/3 时有数据）
        deep = [r for r in enabled if r.toc_complete is not None or r.content_ok is not None]
        if deep:
            toc_fail = sorted(
                [r for r in deep if r.toc_complete is False],
                key=lambda r: r.chapter_count,
            )
            content_fail = [
                r for r in deep if r.toc_complete is True and r.content_ok is False
            ]
            # "无法完成"仅指尝试过深度验证但未能给出结论的源；
            # 目录已判定不完整（toc_complete is False）的归入"疑似目录不完整"，不在此列（避免重复）
            unverifiable = [
                r for r in deep
                if r.toc_complete is not False
                and ((r.toc_complete is None and r.toc_fail_reason)
                     or (r.content_ok is None and r.content_fail_reason))
            ]
            if toc_fail:
                lines.append(f"### 疑似目录不完整（{len(toc_fail)} 个，章节数低于参考阈值）")
                lines.append("")
                lines.append("| 源名称 | 命中作品 | 章节数 | 原因 |")
                lines.append("|--------|----------|------:|------|")
                for r in toc_fail[:15]:
                    lines.append(f"| {r.name} | {r.search_hit or '-'} | {r.chapter_count} | {r.toc_fail_reason} |")
                if len(toc_fail) > 15:
                    lines.append(f"- …共 {len(toc_fail)} 个")
                lines.append("")
            if content_fail:
                lines.append(f"### 目录完整但正文验证失败（{len(content_fail)} 个）")
                lines.append("")
                lines.append("| 源名称 | 命中作品 | 原因 |")
                lines.append("|--------|----------|------|")
                for r in sorted(content_fail, key=lambda r: r.name)[:15]:
                    lines.append(f"| {r.name} | {r.search_hit or '-'} | {r.content_fail_reason} |")
                if len(content_fail) > 15:
                    lines.append(f"- …共 {len(content_fail)} 个")
                lines.append("")
            if unverifiable:
                lines.append(f"### 深度验证无法完成（{len(unverifiable)} 个，规则非CSS或网络失败，该维度回退静态规则判定）")
                lines.append("")
                lines.append("| 源名称 | 命中作品 | 原因 |")
                lines.append("|--------|----------|------|")
                for r in sorted(unverifiable, key=lambda r: r.name)[:15]:
                    reason = r.toc_fail_reason or r.content_fail_reason
                    lines.append(f"| {r.name} | {r.search_hit or '-'} | {reason} |")
                if len(unverifiable) > 15:
                    lines.append(f"- …共 {len(unverifiable)} 个")
                lines.append("")

        # ---- 优质 TOP（五星源）
        top = sorted(
            [r for r in starred if r.quality_stars >= 4],
            key=lambda r: (r.quality_stars, -r.search_response_ms if r.search_response_ms else 0, r.name),
            reverse=False,
        )
        # 按星级降序、响应时间升序
        top.sort(key=lambda r: (-r.quality_stars, r.search_response_ms or 10 ** 9))
        if top:
            lines.append("### 优质源 TOP（4★以上）")
            lines.append("")
            lines.append("| 星级 | 源名称 | 类型 | 命中作品 | 目录 | 搜索响应 |")
            lines.append("|------|--------|------|----------|-----:|----------|")
            for r in top[:25]:
                toc_col = f"{r.chapter_count}章" if r.chapter_count else "-"
                lines.append(
                    f"| {'★' * r.quality_stars} | {r.name} | {r.type_name} | "
                    f"{r.search_hit or '-'} | {toc_col} | {r.search_response_ms or '-'}ms |"
                )
            lines.append("")

        # ---- 响应速度排行（同星级内排序参考）
        fast = sorted(
            [r for r in enabled if r.health == Health.OK and r.quality_stars >= 3],
            key=lambda r: r.search_response_ms or r.response_time_ms,
        )
        if fast:
            lines.append("### 最快响应源 TOP（可用且3★以上）")
            lines.append("")
            lines.append("| 源名称 | 类型 | 星级 | 响应 |")
            lines.append("|--------|------|------|------|")
            for r in fast[:15]:
                ms = r.search_response_ms or r.response_time_ms
                lines.append(f"| {r.name} | {r.type_name} | {'★' * r.quality_stars} | {ms}ms |")
            lines.append("")

    if health_ok:
        lines.append("## 三、健康状态分布（已校验）")
        lines.append("")
        lines.append("| 状态 | 数量 | 占比 |")
        lines.append("|------|-----:|-----:|")
        health_counter = Counter(r.health for r in enabled)
        # **不再自带一份名字表**：原来这里抄了第三份（措辞还和后端不一致：
        # 「🔒需登录/验证」vs「🔒需验证」），新增一个健康态时漏改是必然的。
        # 唯一权威是 core/models.py 的 HEALTH_NAMES（`.get(h, h)` 兜住未来新增值）
        for h in sorted(health_counter, key=lambda x: -health_counter[x]):
            c = health_counter[h]
            lines.append(f"| {HEALTH_NAMES.get(h, h)} | {c} | {c/len(enabled)*100:.1f}% |")
        lines.append("")

        # ---- 疑似被墙源清单
        gfw = [r for r in enabled if r.health == Health.GFW]
        if gfw:
            lines.append(f"### 疑似被墙源（{len(gfw)} 个，可开代理复检）")
            lines.append("")
            for r in sorted(gfw, key=lambda r: r.name)[:15]:
                lines.append(f"- {r.name} `{r.url}` ({(r.error or '').split('（')[0]})")
            if len(gfw) > 15:
                lines.append(f"- …共 {len(gfw)} 个")
            lines.append("")

        # ---- 可用源分组预览
        lines.append("### 可用源按新分组预览")
        lines.append("")
        from core.organizer import summarize_grouping
        summary = summarize_grouping([r for r in records if r.health == Health.OK])
        lines.append("| 分组 | 数量 |")
        lines.append("|------|-----:|")
        for item in summary[:30]:
            lines.append(f"| {item['group']} | {item['count']} |")
        lines.append("")

    # ---- 原分组混乱度
    lines.append("## 四、原分组混乱度")
    lines.append("")
    orig_groups = Counter((r.group or "(空)") for r in enabled)
    noise_groups = [g for g in orig_groups if any(p in g for p in DEAD_TAG_PATTERNS)]
    lines.append(f"- 原分组总数：**{len(orig_groups)}** 个（含大量噪音分组）")
    lines.append(f"- 命名中带「失效」等噪音标记的分组：{len(noise_groups)} 个")
    if noise_groups:
        lines.append(f"  - 示例：{', '.join(noise_groups[:10])}")
    lines.append("")

    # ---- 问题清单
    lines.append("## 五、问题源清单")
    lines.append("")
    no_search = [r for r in enabled if not r.has_search]
    no_toc = [
        r for r in enabled
        if not (r.raw.get("ruleToc") or {}).get("chapterList", "")
    ]
    no_content = [
        r for r in enabled
        if not (r.raw.get("ruleContent") or {}).get("content", "")
    ]
    dead_tagged = [r for r in enabled if r.dead_tagged]
    lines.append(f"- 无搜索规则：**{len(no_search)}** 个")
    lines.append(f"- 无目录规则（chapterList 为空）：**{len(no_toc)}** 个")
    lines.append(f"- 无正文规则（content 为空）：**{len(no_content)}** 个")
    lines.append(f"- 分组/备注标注「失效」：**{len(dead_tagged)}** 个")
    lines.append("")

    # ---- 重复检测
    if include_duplicates:
        dup = _find_duplicates(records)
        if dup:
            lines.append("## 六、疑似重复源（同 URL 或同名称）")
            lines.append("")
            for group_key, items in list(dup.items())[:20]:
                lines.append(f"- **{group_key}**：{len(items)} 个")
                for it in items[:5]:
                    lines.append(f"  - {it.name} `{it.url}`")
            if len(dup) > 20:
                lines.append(f"- …共 {len(dup)} 组重复")
            lines.append("")

    # ---- 建议
    lines.append("## 七、使用建议")
    lines.append("")
    if health_ok:
        good = health_ok[:10]
        lines.append(f"- 本次校验出 **{len(health_ok)}** 个可用源，建议优先从「✅可用」分组中挑选。")
        lines.append(f"- 推荐示例：{', '.join(r.name for r in good[:8])}")
    lines.append("- 可用但需登录的源（🔒需登录），导入后需在 App 内完成登录。")
    lines.append("- 无搜索规则但目录/正文可用的源，只能通过「发现」浏览，无法关键词搜索。")
    lines.append("")
    return "\n".join(lines)


def _find_duplicates(records: List[BookSourceRecord]) -> Dict[str, List[BookSourceRecord]]:
    """按 URL（归一化后）与名称查找重复。"""
    from core.loader import _normalize_url
    groups: Dict[str, List[BookSourceRecord]] = {}
    for r in records:
        key = _normalize_url(r.url) or r.name.strip()
        if not key:
            continue
        groups.setdefault(key, []).append(r)
    return {k: v for k, v in groups.items() if len(v) > 1}