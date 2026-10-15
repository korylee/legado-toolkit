# -*- coding: utf-8 -*-
"""找重复书源（**只读**）：决定「这条源怎么干活」的**核心字段**相同的那些。

**为什么按核心字段、不按全量字段**：一条源的行为由四个字段决定——

  ``searchUrl``             搜什么（请求发到哪）
  ``ruleSearch.bookList``   搜索结果怎么列
  ``ruleToc.chapterList``   目录怎么取
  ``ruleContent.content``   正文怎么读

这四个之外的字段要么是展示/元数据（名称、排序、备注、分组、校验词），要么是
转发链路上的噪音（同一字段 null / "" / 缺键三种形态、``concurrentRate: 0``
等价于不写、``enabledExplore`` 开着但 ``exploreUrl`` 是空的空开关）。全量比对
会被这些噪音拆散——po5.net 三条转发在四字段上完全一致，全量比对却一条都抓不到。

**为什么不能按地址去重**（App 的 ``getSourceKey()``、库里的 URL 唯一索引同理）：
键是地址原文，而分享圈的习惯是在地址后面挂署名——``#guaner`` / ``#♤guaner`` /
``#关耳`` / ``#🎃`` 都是同一条源被转发几次的产物，地址不同、内容一字不差。

**为什么域名只是硬边界不是判据**：同一域名下可以住多种真不同的源（改版前后、
多作者各写一套）。四字段把「行为相同」钉住，域名把「能合并」钉住：同域名 +
四字段相同 → 可合并档；四字段相同但跨域名 → 镜像档（只提示）；同域名但四字段
不同 → 各自独立，不是重复。

名称是展示名，改名不等于换源——比较里不参与；App 的 ``getSourceKey()`` 仍按
地址原文认源，两件事互不影响。

**输出只读**：只打印/写报告，绝不改库。删哪条由人定。
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from core.loader import _normalize_url
from core.models import Health
from core.urls import abs_url, split_url_options

#: 决定「这条源怎么干活」的核心字段。**不要图省事扩大**：每加一个字段都是在
#: 重新引入全量比对被否决的那批噪音——要加先回答「上游 App 拿它改变哪一步行为」。
CORE_FIELDS = ("searchUrl", "ruleSearch.bookList", "ruleToc.chapterList",
               "ruleContent.content")

#: 端口后缀。``\d*`` 而不是 ``\d+``：库里真实存在 `http://app.wanshu.com:` 这种
#: 端口写空了的畸形地址，按 ``\d+`` 去不掉那个冒号，于是一个**同域名**的重复组
#: 会被标成"跨域名"——那类是要人判断、别自动删的，白让人多看一眼
_PORT_RE = re.compile(r":\d*$")


def _pick(raw: Dict[str, Any], path: str) -> Any:
    """按点号路径取值（一层嵌套），取不到返回 None。"""
    section, _, field = path.partition(".")
    part = (raw or {}).get(section)
    if isinstance(part, dict):
        return part.get(field)
    return None


def _prune_blank(v: Any) -> Any:
    """递归剔除「没写」的值（None / 空串 / 空白串 / 空容器）。

    同一条源被不同工具导出，空规则一会是 ``null``、一会是 ``""``、一会整个键
    没有——上游 App 反序列化后行为完全相同（空规则等于没写规则）。
    """
    if isinstance(v, dict):
        out = {k: pv for k, v2 in v.items()
               if (pv := _prune_blank(v2)) not in (None, "", [], {})}
        return out
    if isinstance(v, str):
        return v.strip() or None
    if isinstance(v, list):
        out = [pv for pv in (_prune_blank(x) for x in v) if pv not in (None, "", [], {})]
        return out or None
    return v


def _search_url_key(src: Dict[str, Any]) -> str:
    """searchUrl 的比较键：剥 `,{json}` 选项、**剥站点前缀**、去署名与尾斜杠。

    App 拿 ``bookSourceUrl`` 当 baseUrl 把 searchUrl 拼成完整地址再发（WebBook.
    searchBookAwait → AnalyzeUrl → NetworkUtils.getAbsoluteURL，已查证），
    ``so.php?q=…``、``/so.php?q=…``、``https://www.po5.net/so.php?q=…`` 三种写法
    发出去的是**同一个请求**；转发圈顺手改写法的比挂署名还常见。

    补全后又把 scheme+host+端口剥掉：重复分组的**站点维度由 site_key 承担**
    （mergeable/mirror 的分组键），指纹只管「路径与参数」。不剥的话 host 会从
    searchUrl 侧漏进指纹——同一条源的镜像站（规则一字不差、域名不同）会被
    误判成不同源，镜像档就永远空了。相对写法补全只是为了归一 ``./x`` / ``x``
    这类路径形态，补全完再剥是同一条链路的两半。
    """
    raw = str(src.get("searchUrl", "") or "")
    pure, _opts = split_url_options(raw)
    pure = pure.strip()
    if not pure:
        return ""
    if not pure.startswith(("http://", "https://", "javascript:")):
        base = str(src.get("bookSourceUrl", "") or "").split("#", 1)[0].strip()
        pure = abs_url(base, pure)
    if pure.startswith(("http://", "https://")):
        # 剥 scheme://host[:port]，保留路径与查询；找不到分隔的 "/" 就没有路径
        rest = pure.split("://", 1)[1]
        slash = rest.find("/")
        pure = rest[slash:] if slash != -1 else ""
    return _normalize_url(pure).rstrip("/")


def _core_parts(raw: Dict[str, Any]) -> Dict[str, Any]:
    """抽四个核心字段并各自归一。"""
    src = raw or {}
    return {
        "searchUrl": _search_url_key(src),
        "ruleSearch.bookList": _prune_blank(_pick(src, "ruleSearch.bookList")),
        "ruleToc.chapterList": _prune_blank(_pick(src, "ruleToc.chapterList")),
        "ruleContent.content": _prune_blank(_pick(src, "ruleContent.content")),
    }


def rule_signature(raw: Dict[str, Any]) -> str:
    """行为指纹：四个核心字段归一后的 sha1。"""
    blob = json.dumps(_core_parts(raw), sort_keys=True,
                      ensure_ascii=False, separators=(",", ":"))
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
        "probe_depth": int((item or {}).get("probe_depth", 0) or 0),
        "search_hit": str((item or {}).get("search_hit", "") or ""),
        "toc_complete": (item or {}).get("toc_complete"),
        "content_ok": (item or {}).get("content_ok"),
        "checked_at": str((item or {}).get("checked_at", "") or ""),
    }


def _verification_rank(m: Dict[str, Any]) -> Tuple:
    """同一健康状态内按已验证事实排序，不合成新的质量分数。"""
    if m["content_ok"] is True:
        return (0,)
    if m["toc_complete"] is True:
        return (1,)
    if m["search_hit"]:
        return (2,)
    if m["probe_depth"]:
        return (3,)
    return (4,)


def _keep_rank(m: Dict[str, Any]) -> Tuple:
    """建议保留哪个：先看健康状态，再看验证事实，最后看地址和导入顺序。

    可用性排在最前，是因为跨域名的组里镜像站的差别很大（一个通、一个死了）——
    这时候"地址更干净"要让位给"真的能用"。
    """
    return (0 if m["health"] == Health.OK else 1,
            _verification_rank(m),
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
