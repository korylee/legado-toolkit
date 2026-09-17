# -*- coding: utf-8 -*-
"""找重复书源（**只读**）：**行为字段**完全相同，只有地址/名称/排序/备注/分组不同的那些。

**为什么不能按域名去重**：同一个域名下常有多个**不同的**源——实测
``m.suixkan.com`` 一个域名下有 5 种（「随心看吧」4 条重复 + 「阅友小说」2 种 +
「随心看」1 条），按域名合并会把能用的源误删。

**为什么不能按地址去重**（现有的 `core.loader.dedupe_sources`）：它的键是地址原文，
而分享圈的习惯是在地址后面挂署名——``#guaner`` / ``#♤guaner`` / ``#关耳`` / ``#🎃``
都是同一条源被转发几次的产物，地址不同、内容一字不差。那套去重有意的"署名不同即
两个源"（对齐 Legado 的 ``getSourceKey()``）在这里恰好一条都抓不到。

所以这里换一个判据：**比"行为字段"**——把与"这条源怎么工作"无关的字段排除再比。

名称是展示名，改名不等于换源——**A 档按行为字段比较，名称不参与**；
App 的 ``getSourceKey()`` 仍按地址原文认源，两件事互不影响。

**输出只读**：只打印/写报告，绝不改库。删哪条由人定——跨域名的组（镜像站）尤其
需要人看一眼，程序分不出"两个都能用"和"一个是坏的镜像"。
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from core.loader import _normalize_url
from core.models import HEALTH_NAMES, Health

#: 与「这条源怎么工作」无关的字段，比较时排除。每一条都有理由，**不要图省事扩大**：
#:   bookSourceUrl    地址本身——重复的正是"地址不同、内容相同"这种东西
#:   bookSourceName   展示名——改名不等于换源，同规则不同名照样是重复
#:   customOrder      App 里的手动排序序号（每条源天然不同）
#:   lastUpdateTime   导入/保存时间
#:   respondTime      上次响应耗时（运行元数据）
#:   bookSourceGroup  分组——本项目自己的工具会重建它，不是源的内容
#:   bookSourceComment 备注——分享时常被塞进水印/来源字样
NON_BEHAVIORAL = ("bookSourceUrl", "bookSourceName", "customOrder", "lastUpdateTime",
                  "respondTime", "bookSourceGroup", "bookSourceComment")

#: 端口后缀。``\d*`` 而不是 ``\d+``：库里真实存在 `http://app.wanshu.com:` 这种
#: 端口写空了的畸形地址，按 ``\d+`` 去不掉那个冒号，于是一个**同域名**的重复组
#: 会被标成"跨域名"——那类是要人判断、别自动删的，白让人多看一眼
_PORT_RE = re.compile(r":\d*$")


def rule_signature(raw: Dict[str, Any]) -> str:
    """行为指纹：排除 :data:`NON_BEHAVIORAL`（含名称）后，规范化 JSON 的 sha1。"""
    d = {k: v for k, v in (raw or {}).items() if k not in NON_BEHAVIORAL}
    blob = json.dumps(d, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def host_of(url: str) -> str:
    """URL → 域名（去署名、去路径、去端口、小写）。"""
    host = (url or "").split("#", 1)[0].split("//", 1)[-1].split("/", 1)[0]
    return _PORT_RE.sub("", host).lower()


def site_key(url: str) -> str:
    """合并硬边界：host + 有效端口（scheme 不参与，http/https 视为同一站点）。

    与 :func:`host_of` 的区别：host_of 是展示用的"同域名"，会把端口整个剥掉；
    ``a.com:8080`` 和 ``a.com`` 因此看起来同域名，但它们是两个服务，不能合并。
    空端口（``http://a.com:``）按无端口处理；http:80 / https:443 归一为无端口。
    """
    raw = str(url or "").strip()
    if not raw:
        return ""
    if "//" not in raw.split("#", 1)[0]:
        raw = "http://" + raw.lstrip("/")
    try:
        parts = urlsplit(raw)
        host = (parts.hostname or "").lower()
        if not host:
            return ""
        try:
            port = parts.port
        except ValueError:
            port = None
    except ValueError:
        return ""
    if port is None or (parts.scheme == "http" and port == 80) \
            or (parts.scheme == "https" and port == 443):
        return host
    return "%s:%d" % (host, port)


def same_site(a: str, b: str) -> bool:
    """两个 URL 是否在同一个可合并站点（host + 有效端口；两者都必须非空）。"""
    ka, kb = site_key(a), site_key(b)
    return bool(ka) and ka == kb


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
        raw_sites = [site_key(str(s.get("bookSourceUrl", "") or "")) for s in members]
        sites = sorted({s for s in raw_sites if s})
        keep = min(rows, key=_keep_rank)
        out.append({
            "kind": "rules",
            "key": sig,
            "signature": sig,
            "hosts": hosts,
            "sites": sites,
            "same_host": len(hosts) == 1,
            "same_site": len(sites) == 1 and all(raw_sites),
            "members": rows,
            "keep": keep["order"],
            "redundant": len(rows) - 1,
        })
    # 组大的在前；同样大时同域名的在前（那类更安全、更该先处理）
    out.sort(key=lambda g: (-len(g["members"]), not g["same_site"], g["hosts"][0]))
    return out


def _group_by(sources: Sequence[Dict[str, Any]],
              checks: Optional[Dict[str, Dict[str, Any]]],
              key_fn, kind: str) -> List[Dict[str, Any]]:
    """按任意 key 分组（>1 条才成组），成员形状与 find_dup_groups 一致。

    只做归并；A / B / C 的重叠、默认勾选与展示顺序由 API/前端决定。
    """
    checks = checks or {}
    buckets: Dict[str, List[Dict[str, Any]]] = {}
    for src in sources:
        if not isinstance(src, dict):
            continue
        key = key_fn(src)
        if not key:
            continue
        buckets.setdefault(key, []).append(src)

    out: List[Dict[str, Any]] = []
    for key, members in buckets.items():
        if len(members) < 2:
            continue
        rows = [_member(s, checks.get(_normalize_url(str(s.get("bookSourceUrl", "") or ""))), i)
                for i, s in enumerate(members)]
        hosts = sorted({r["host"] for r in rows})
        keep = min(rows, key=_keep_rank)
        out.append({
            "kind": kind,
            "key": key,
            "hosts": hosts,
            "members": rows,
            "keep": keep["order"],
            "redundant": len(rows) - 1,
        })
    # 组大的在前；同大小时按 key 稳定排序，便于测试与翻页
    out.sort(key=lambda g: (-len(g["members"]), g["key"]))
    return out


def _host_key(src: Dict[str, Any]) -> str:
    return host_of(str(src.get("bookSourceUrl", "") or ""))


def _name_key(src: Dict[str, Any]) -> str:
    # 名称只做空白归一，不做大小写/别名——「同名」是给人看的展示名，宁窄勿宽
    return " ".join(str(src.get("bookSourceName", "") or "").split())


def find_host_groups(sources: Sequence[Dict[str, Any]],
                     checks: Optional[Dict[str, Dict[str, Any]]] = None
                     ) -> List[Dict[str, Any]]:
    """B 档：同域名归并（不判规则是否相同）。空域名不参与分组。"""
    return _group_by(sources, checks, _host_key, "host")


def find_name_groups(sources: Sequence[Dict[str, Any]],
                     checks: Optional[Dict[str, Dict[str, Any]]] = None
                     ) -> List[Dict[str, Any]]:
    """C 档：同名归并（跨域名）。空名称不参与分组。"""
    return _group_by(sources, checks, _name_key, "name")


def _rows_of(members, checks):
    return [_member(s, checks.get(_normalize_url(str(s.get("bookSourceUrl", "") or ""))), i)
            for i, s in enumerate(members)]


def find_mergeable_groups(sources: Sequence[Dict[str, Any]],
                          checks: Optional[Dict[str, Dict[str, Any]]] = None
                          ) -> List[Dict[str, Any]]:
    """**可合并** A 档：同一站点（host + 有效端口）+ 行为指纹相同。

    站点是硬边界：跨 host / 跨非默认端口一律不进来（见 find_mirror_groups）。
    """
    checks = checks or {}
    buckets: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for src in sources:
        if not isinstance(src, dict):
            continue
        site = site_key(str(src.get("bookSourceUrl", "") or ""))
        if not site:
            continue
        buckets.setdefault((site, rule_signature(src)), []).append(src)
    out: List[Dict[str, Any]] = []
    for (site, sig), members in buckets.items():
        if len(members) < 2:
            continue
        rows = _rows_of(members, checks)
        keep = min(rows, key=_keep_rank)
        out.append({
            "kind": "mergeable", "key": sig, "signature": sig, "site": site,
            "hosts": sorted({r["host"] for r in rows}), "same_host": True,
            "members": rows, "keep": keep["order"], "redundant": len(rows) - 1,
        })
    out.sort(key=lambda g: (-len(g["members"]), g["site"], g["key"]))
    return out


def find_mirror_groups(sources: Sequence[Dict[str, Any]],
                       checks: Optional[Dict[str, Dict[str, Any]]] = None
                       ) -> List[Dict[str, Any]]:
    """**只读镜像**：行为指纹相同，但成员跨了多个站点（host + 有效端口）。"""
    checks = checks or {}
    buckets: Dict[str, List[Dict[str, Any]]] = {}
    for src in sources:
        if not isinstance(src, dict):
            continue
        buckets.setdefault(rule_signature(src), []).append(src)
    out: List[Dict[str, Any]] = []
    for sig, members in buckets.items():
        if len(members) < 2:
            continue
        sites = sorted({site_key(str(s.get("bookSourceUrl", "") or "")) for s in members})
        sites = [s for s in sites if s]
        if len(sites) < 2:
            continue
        rows = _rows_of(members, checks)
        keep = min(rows, key=_keep_rank)
        out.append({
            "kind": "mirror", "key": sig, "signature": sig, "sites": sites,
            "hosts": sorted({r["host"] for r in rows}), "same_host": False,
            "members": rows, "keep": keep["order"], "redundant": len(rows) - 1,
        })
    out.sort(key=lambda g: (-len(g["members"]), g["key"]))
    return out


#: /api/sources/dups 接受的 kind 值；顺序即默认展示顺序。
DUP_KINDS = ("rules", "mergeable", "host", "name", "mirror")


def find_groups(sources: Sequence[Dict[str, Any]],
                checks: Optional[Dict[str, Dict[str, Any]]] = None,
                kinds=("rules",)) -> List[Dict[str, Any]]:
    """按 kind 组合三档分组；未知 kind 忽略（由 API 层负责报 400）。"""
    want = set(kinds or ())
    out: List[Dict[str, Any]] = []
    if "rules" in want:
        out += find_dup_groups(sources, checks)
    if "mergeable" in want:
        out += find_mergeable_groups(sources, checks)
    if "host" in want:
        out += find_host_groups(sources, checks)
    if "name" in want:
        out += find_name_groups(sources, checks)
    if "mirror" in want:
        out += find_mirror_groups(sources, checks)
    return out


def summarize_by_kind(groups: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, int]]:
    """按 kind 统计组数 / 条数 / 可精简数，供 /api/sources/dups 的 summary 用。"""
    out: Dict[str, Dict[str, int]] = {}
    for g in groups:
        kind = str(g.get("kind", "rules") or "rules")
        row = out.setdefault(kind, {"groups": 0, "rows": 0, "redundant": 0})
        row["groups"] += 1
        row["rows"] += len(g.get("members") or [])
        row["redundant"] += int(g.get("redundant", 0) or 0)
    return out


def summarize(groups: Sequence[Dict[str, Any]], total: int) -> Dict[str, int]:
    """统计：总数 / 组数 / 两种类别各自的条数与可精简数。"""
    same = [g for g in groups if g.get("same_site", g.get("same_host"))]
    cross = [g for g in groups if not g.get("same_site", g.get("same_host"))]
    return {
        "sources": total,
        "groups": len(groups),
        "same_site_groups": len(same),
        "same_site_rows": sum(len(g["members"]) for g in same),
        "same_site_redundant": sum(g["redundant"] for g in same),
        "cross_site_groups": len(cross),
        "cross_site_rows": sum(len(g["members"]) for g in cross),
        "cross_site_redundant": sum(g["redundant"] for g in cross),
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
        "- **同一站点**（域名 + 有效端口）：%d 组 / %d 条 → 可精简 **%d 条**（这类是可合并的重复）"
        % (s["same_site_groups"], s["same_site_rows"], s["same_site_redundant"]),
        "- **跨站点**：%d 组 / %d 条 → 逐组自己看（可能是镜像站，也可能其中一条已经不能用了）"
        % (s["cross_site_groups"], s["cross_site_rows"]),
        "",
        "判据：比较**行为字段**（规则、类型、搜索地址、header、cookie 开关……），"
        "排除地址、名称、App 排序、更新时间、上次耗时、分组、备注。",
        "「建议保留」按「先能用的、再地址干净的、最后先导入的」挑一条，**仅供参考**。",
        "本清单不改任何数据——删哪条由你定。",
        "",
    ]
    if not groups:
        lines.append("没有发现规则完全相同的源。")
        return "\n".join(lines) + "\n"

    for i, g in enumerate(groups, 1):
        kind = "同一站点" if g.get("same_site", g.get("same_host")) else "跨站点"
        shown = g.get("sites") or g["hosts"]
        title = "、".join(shown[:3]) + ("…" if len(shown) > 3 else "")
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
