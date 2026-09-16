# -*- coding: utf-8 -*-
"""找重复书源（**只读**）：规则完全相同、只有地址/署名/排序/备注不同的那些。

**为什么不能按域名去重**：同一个域名下常有多个**不同的**源——实测
``m.suixkan.com`` 一个域名下有 5 种（「随心看吧」4 条重复 + 「阅友小说」2 种 +
「随心看」1 条），按域名合并会把能用的源误删。

**为什么不能按地址去重**（现有的 `core.loader.dedupe_sources`）：它的键是地址原文，
而分享圈的习惯是在地址后面挂署名——``#guaner`` / ``#♤guaner`` / ``#关耳`` / ``#🎃``
都是同一条源被转发几次的产物，地址不同、内容一字不差。那套去重有意的"署名不同即
两个源"（对齐 Legado 的 ``getSourceKey()``）在这里恰好一条都抓不到。

所以这里换一个判据：**比"行为字段"**——把与"这条源怎么工作"无关的字段排除再比。

**输出只读**：只打印/写报告，绝不改库。删哪条由人定——跨域名的组（镜像站）尤其
需要人看一眼，程序分不出"两个都能用"和"一个是坏的镜像"。
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from core.loader import _normalize_url
from core.models import HEALTH_NAMES, Health

#: 与「这条源怎么工作」无关的字段，比较时排除。每一条都有理由，**不要图省事扩大**：
#:   bookSourceUrl    地址本身——重复的正是"地址不同、内容相同"这种东西
#:   customOrder      App 里的手动排序序号（每条源天然不同）
#:   lastUpdateTime   导入/保存时间
#:   respondTime      上次响应耗时（运行元数据）
#:   bookSourceGroup  分组——本项目自己的工具会重建它，不是源的内容
#:   bookSourceComment 备注——分享时常被塞进水印/来源字样
NON_BEHAVIORAL = ("bookSourceUrl", "customOrder", "lastUpdateTime", "respondTime",
                  "bookSourceGroup", "bookSourceComment")

#: 端口后缀。``\d*`` 而不是 ``\d+``：库里真实存在 `http://app.wanshu.com:` 这种
#: 端口写空了的畸形地址，按 ``\d+`` 去不掉那个冒号，于是一个**同域名**的重复组
#: 会被标成"跨域名"——那类是要人判断、别自动删的，白让人多看一眼
_PORT_RE = re.compile(r":\d*$")


def rule_signature(raw: Dict[str, Any]) -> str:
    """行为指纹：排除 :data:`NON_BEHAVIORAL` 后，规范化 JSON 的 sha1。"""
    d = {k: v for k, v in (raw or {}).items() if k not in NON_BEHAVIORAL}
    blob = json.dumps(d, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def host_of(url: str) -> str:
    """URL → 域名（去署名、去路径、去端口、小写）。"""
    host = (url or "").split("#", 1)[0].split("//", 1)[-1].split("/", 1)[0]
    return _PORT_RE.sub("", host).lower()


def _member(src: Dict[str, Any], item: Optional[Dict[str, Any]],
            order: int) -> Dict[str, Any]:
    url = str(src.get("bookSourceUrl", "") or "")
    return {
        "order": order,                      # 组内序号，仅供报告里指认
        "url": url,
        "name": str(src.get("bookSourceName", "") or ""),
        "comment": str(src.get("bookSourceComment", "") or "").strip(),
        "host": host_of(url),
        "has_fragment": "#" in url,
        "has_port": bool(_PORT_RE.search(url.split("#", 1)[0].split("//", 1)[-1])),
        "health": str((item or {}).get("health", "") or ""),
        "stars": int((item or {}).get("quality_stars", 0) or 0),
        "checked_at": str((item or {}).get("checked_at", "") or ""),
    }


def _keep_rank(m: Dict[str, Any]) -> Tuple:
    """建议保留哪个：**先看能不能用**，再看地址干不干净，最后看导入早晚。

    可用性排在最前，是因为跨域名的组里镜像站的差别很大（一个通、一个死了）——
    这时候"地址更干净"要让位给"真的能用"。
    """
    return (0 if m["health"] == Health.OK else 1,
            -m["stars"],
            1 if m["has_fragment"] else 0,     # 带署名/畸形端口的排在后面
            1 if m["has_port"] else 0,
            0 if m["url"].startswith("https://") else 1,
            m["order"])


def find_dup_groups(sources: Sequence[Dict[str, Any]],
                    checks: Optional[Dict[str, Dict[str, Any]]] = None
                    ) -> List[Dict[str, Any]]:
    """把书源按行为指纹分组，只返回**多于一条**的组。

    ``checks`` 是 ``Store.checks_map()`` 的结果（键已规范化），用于在报告里标出
    每条的可用性、并让"建议保留"优先挑能用的那条；不传（比如直接看一个 JSON 文件）
    就只按地址干净程度和先后顺序判断。
    """
    checks = checks or {}
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for src in sources:
        if not isinstance(src, dict):
            continue
        groups.setdefault(rule_signature(src), []).append(src)

    out: List[Dict[str, Any]] = []
    for sig, members in groups.items():
        if len(members) < 2:
            continue
        rows = [_member(s, checks.get(_normalize_url(str(s.get("bookSourceUrl", "") or ""))), i)
                for i, s in enumerate(members)]
        hosts = sorted({r["host"] for r in rows})
        keep = min(rows, key=_keep_rank)
        out.append({
            "signature": sig,
            "hosts": hosts,
            "same_host": len(hosts) == 1,
            "members": rows,
            "keep": keep["order"],
            "redundant": len(rows) - 1,
        })
    # 组大的在前；同样大时同域名的在前（那类更安全、更该先处理）
    out.sort(key=lambda g: (-len(g["members"]), not g["same_host"], g["hosts"][0]))
    return out


def summarize(groups: Sequence[Dict[str, Any]], total: int) -> Dict[str, int]:
    """统计：总数 / 组数 / 两种类别各自的条数与可精简数。"""
    same = [g for g in groups if g["same_host"]]
    cross = [g for g in groups if not g["same_host"]]
    return {
        "sources": total,
        "groups": len(groups),
        "same_host_groups": len(same),
        "same_host_rows": sum(len(g["members"]) for g in same),
        "same_host_redundant": sum(g["redundant"] for g in same),
        "cross_host_groups": len(cross),
        "cross_host_rows": sum(len(g["members"]) for g in cross),
        "cross_host_redundant": sum(g["redundant"] for g in cross),
    }


def _suffix(m: Dict[str, Any]) -> str:
    bits = []
    if m["health"]:
        bits.append(HEALTH_NAMES.get(m["health"], m["health"]))
    if m["stars"]:
        bits.append("%d★" % m["stars"])
    if m["checked_at"]:
        bits.append(m["checked_at"])
    if m["comment"]:
        bits.append("备注:" + m["comment"].replace("\n", " ")[:20])
    return ("  " + " ".join(bits)) if bits else ""


def render_report(groups: Sequence[Dict[str, Any]], total: int,
                  source_label: str = "") -> str:
    """把分组渲染成给人看的清单。**只读**，不产生任何写入。"""
    s = summarize(groups, total)
    lines: List[str] = ["# 重复书源清单（只读）", ""]
    if source_label:
        lines.append("数据来源：%s" % source_label)
    lines += [
        "共 %d 条源，其中 **%d 组规则完全相同**（相差的只有地址/署名/排序/备注/分组）："
        % (s["sources"], s["groups"]),
        "",
        "- **同一域名**：%d 组 / %d 条 → 可精简 **%d 条**（这类是安全的重复）"
        % (s["same_host_groups"], s["same_host_rows"], s["same_host_redundant"]),
        "- **跨域名**：%d 组 / %d 条 → 逐组自己看（可能是镜像站，也可能其中一条已经不能用了）"
        % (s["cross_host_groups"], s["cross_host_rows"]),
        "",
        "判据：比较**行为字段**（规则、类型、搜索地址、header、cookie 开关……），"
        "排除地址、App 排序、更新时间、上次耗时、分组、备注。",
        "「建议保留」按「先能用的、再地址干净的、最后先导入的」挑一条，**仅供参考**。",
        "本清单不改任何数据——删哪条由你定。",
        "",
    ]
    if not groups:
        lines.append("没有发现规则完全相同的源。")
        return "\n".join(lines) + "\n"

    for i, g in enumerate(groups, 1):
        kind = "同一域名" if g["same_host"] else "跨域名"
        title = "、".join(g["hosts"][:3]) + ("…" if len(g["hosts"]) > 3 else "")
        lines.append("---")
        lines.append("")
        lines.append("## %d. %d 条 · %s（%s，可精简 %d 条）"
                     % (i, len(g["members"]), title, kind, g["redundant"]))
        lines.append("")
        for m in g["members"]:
            mark = "**[建议保留]** " if m["order"] == g["keep"] else ""
            lines.append("- %s`%s` 「%s」%s"
                         % (mark, m["url"], m["name"], _suffix(m)))
        lines.append("")
    return "\n".join(lines) + "\n"
