# -*- coding: utf-8 -*-
"""本机引擎（App 真源码）的结论 → `checks` 那份口径：失败分因、六档、星级。

**为什么要有这一层**：App 引擎回的是 `state` + 异常原文（`reason` / `root` /
`root_stack`），而列表要的是六档健康、星级、「验到哪一步」。这一层只做映射——
判据全部**复用本地那套**（`evaluate_stars` 的阶梯、`err_desc` 的措辞、
`dns_verdict_text` 的三句判词），不另写第二份（AGENTS #10）。

**两条边界**（照本地口径，不许放宽）：

1. **失败要分因，但不把「这次没验成」判成「源坏了」**：`reset` / `tls` 是被墙特征 →
   需翻墙；`dns` 要拿 :mod:`core.dns_check` 的**外部视角**交叉
   验证（两个公共 DNS 都说域名不存在才判死）；其余（超时、连接被拒、原因不明）一律
   「待验证」——重跑是共同的下一步动作。`classify_transport_error` 的原话：**别在这里
   改成 DEAD**，那等于用一次本机解析失败判死。
2. **引擎自身的问题单列**（Koin 缺绑定、OOM…）：那是**我们没跑成**，不是源的结论
   （AGENTS #4）。档位只能是「待验证」，而且 `error` 里要说明白，否则用户会拿它去改源。

结论行里的异常类名只是**观测**，把它判成哪一档由本模块决定（AGENTS #11：判定里不能
放"我们自己写进去的结论"当输入——那三个字段是引擎原样抛出来的，不是我们的判断）。
"""
from __future__ import annotations

import asyncio
import json
import concurrent.futures
import time
from typing import Any, Dict, List, Optional, Tuple

from core import dns_check
from core.checker import (CACHE_VERSION, classify_transport_error, dns_verdict_text,
                          err_desc, evaluate_stars, static_rule_complete)
from core.loader import _normalize_url, fingerprint
from core.models import Engine, Health
from core.settings_store import DEPTH_CONTENT, DEPTH_SEARCH, DEPTH_TOC

#: 归因结果。前七个与本地传输层那套**同名同义**（`classify_transport_error` /
#: `err_desc` 直接认它们）；后两个是 App 结论特有的桶。
CAUSE_DNS = "dns"
CAUSE_TIMEOUT = "timeout"
CAUSE_RESET = "reset"
CAUSE_TLS = "tls"
CAUSE_CERT = "cert"
CAUSE_PROXY = "proxy"
CAUSE_OTHER = "other"
#: 源自己的规则 / 配置错（JS 语法错、搜索 URL 为空、取值路径不存在…）：下一步是**修**，
#: 不是重跑，也不是删源。
CAUSE_RULE = "rule"
#: 引擎自身没跑成（Koin 缺绑定、OOM、环境缺口）：**不是源的结论**。
CAUSE_SELF = "self"

#: `reason` / `root` / `root_stack` 里的特征 → 归因。**顺序有意义**：更具体的先判
#: ——`NoDefinitionFoundException` 的栈里也可能带 `Socket…` 字样；而 `Socket closed`
#: 是本地关掉的连接，不是「连接被重置」（那一类才是被墙特征），所以它落在兜底里。
_CAUSE_PATTERNS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    (CAUSE_SELF, ("NoDefinitionFoundException", "ExceptionInInitializerError",
                  "OutOfMemoryError", "Koin")),
    (CAUSE_RULE, ("ScriptException", "EcmaError", "搜索url不能为空",
                  "PathNotFoundException", "Expected URL scheme",
                  "json string can not be null")),
    (CAUSE_CERT, ("CertPathValidatorException", "CertificateException",
                  "CertificateExpiredException", "证书")),
    (CAUSE_TLS, ("SSLHandshakeException", "SSLException", "SSLProtocolException")),
    (CAUSE_RESET, ("Connection reset", "StreamResetException", "Connection shutdown",
                   "Broken pipe", "Connection closed")),
    (CAUSE_TIMEOUT, ("SocketTimeoutException", "InterruptedIOException",
                     "TimeoutCancellationException", "timed out", "timeout")),
    (CAUSE_DNS, ("UnknownHostException", "UnresolvedAddressException",
                 "No address associated")),
    (CAUSE_PROXY, ("Proxy",)),
    (CAUSE_OTHER, ("Exception", "Error")),
)

#: 结论行的 `stage` → 本地那根深度轴（搜索 2 / 目录 3 / 正文 4）。
_STAGE_DEPTH = {"search": DEPTH_SEARCH, "toc": DEPTH_TOC, "content": DEPTH_CONTENT}

#: 这些 state 本身就把话说清了，不再按异常分因。
_STATE_HEALTH = {
    "ok": Health.OK,
    "login_wall": Health.AUTH,
    "no_result": Health.PENDING,
    "timeout": Health.PENDING,
    "empty_js_shell": Health.PENDING,
    "invalid": Health.PENDING,
}


def classify_cause(row: Dict[str, Any]) -> str:
    """结论行的异常原文 → 归因；分不出来返回 ``""``（调用方按保守处理）。"""
    blob = " ".join(str(row.get(k) or "") for k in ("reason", "root", "root_stack"))
    for cause, patterns in _CAUSE_PATTERNS:
        if any(p in blob for p in patterns):
            return cause
    return ""


def _brief(row: Dict[str, Any], limit: int = 80) -> str:
    """异常原文的一小段——写进 `error` 给用户看，细节仍在结论行里。"""
    text = str(row.get("root") or row.get("reason") or "").strip()
    return text[:limit]


def _detail(row: Dict[str, Any]) -> str:
    """底层异常类名（`err_desc` 只在「网络异常」那一档上附它）。"""
    text = str(row.get("reason") or "")
    name = text.split(":", 1)[0].strip()
    return name if name.endswith(("Exception", "Error")) else ""


def _run_probe(probe, host: str) -> Tuple[str, str]:
    """DNS 交叉验证。``probe`` 可注入（测试与调用点都不必联网）。"""
    if probe is not None:
        return probe(host)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # 跑批那条路在 FastAPI 的线程池里，没有事件循环 → 直接起一个
        return asyncio.run(dns_check.probe(host))
    # 已经在事件循环里：`asyncio.run` 会炸，丢到一个临时线程里跑
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(dns_check.probe(host))).result()


def health_for(row: Dict[str, Any], *, host: str = "",
               probe=None) -> Tuple[str, str]:
    """结论行 → ``(六档, 给用户看的原因)``。

    `host` 只在归因为 dns 时用来跑交叉验证（空则维持「待复查」）。
    """
    state = str(row.get("state") or "")
    if state in _STATE_HEALTH:
        # 非 error 的几态：判词直接用结论行自带的那句（它是我们 Kotlin 侧按术语表写的），
        # 没有就留空——**不要在这里另编一句**
        return _STATE_HEALTH[state], ("" if state == "ok" else str(row.get("reason") or ""))
    cause = classify_cause(row)
    if cause == CAUSE_SELF:
        return Health.PENDING, "本机引擎这次没跑成（不是源的问题）：%s" % _brief(row)
    if cause == CAUSE_RULE:
        return Health.PENDING, "源的规则/配置有问题（下一步是修）：%s" % _brief(row)
    if cause == CAUSE_DNS:
        if not host:
            return Health.PENDING, "DNS 解析失败待复查（没有域名可交叉验证）"
        return dns_verdict_text(*_run_probe(probe, host))
    # 传输层：口径与本地**同一份**（reset/tls→需翻墙，其余→待验证）。证书不被信任
    # 也走这里（它落 pending，原因由 err_desc("cert") 写进 error）——那一档已撤

    return classify_transport_error(cause), err_desc(cause, _detail(row))


def _has_search(row: Dict[str, Any]) -> bool:
    """源有没有声明搜索规则。分不出时按「有」——星级那边只影响封顶，不判死。"""
    if str(row.get("state") or "") == "invalid":
        return False
    blob = str(row.get("reason") or "") + str(row.get("root") or "")
    return "搜索url不能为空" not in blob


def _hit_name(row: Dict[str, Any]) -> str:
    """命中的书名（星级只看有没有命中，取第一条给它看）。"""
    sample = row.get("sample")
    if isinstance(sample, list) and sample:
        return str(sample[0])
    return ""


def checks_row(row: Dict[str, Any], *, batch: str, checked_at: str = "",
               raw: Optional[Dict[str, Any]] = None,
               has_search: Optional[bool] = None,
               probe=None) -> Optional[Dict[str, Any]]:
    """一条 App 结论 → 一条 checks 行（形状同 `Store.save_checks` 吃的 dict）。

    返回 ``None`` ＝这条结论不该落库（没 URL）。
    """
    url = _normalize_url(str(row.get("url") or ""))
    if not url:
        return None
    host = url.split("//", 1)[-1].split("/")[0] if "//" in url else ""
    health, note = health_for(row, host=host, probe=probe)

    state = str(row.get("state") or "")
    stage = str(row.get("stage") or "search")
    cost_ms = int(row.get("cost_ms") or 0)
    # 搜索**真的跑过**才认它连通（口径同本地：search_response_ms > 0）
    search_ms = cost_ms if state in ("ok", "no_result") else 0
    toc_ok = row.get("toc_complete") if stage in ("toc", "content") else None
    content_ok = row.get("content_ok") if stage == "content" else None
    if has_search is None:
        has_search = _has_search(row)
    stars, basis = evaluate_stars(health, has_search, search_ms, _hit_name(row),
                                 toc_ok, content_ok, raw)
    return {
        "url": url,
        "v": CACHE_VERSION,
        "engine": Engine.JVM,
        "fingerprint": fingerprint(raw) if raw else "",
        "health": health,
        "error": note,
        "status_code": None,
        "response_time_ms": cost_ms,
        "search_hit": _hit_name(row),
        "search_response_ms": search_ms,
        "search_probed": bool(search_ms),
        "quality_stars": stars,
        "star_basis": basis,
        "quality_tags": ["规则完整"] if static_rule_complete(raw) else [],
        "probe_depth": _STAGE_DEPTH.get(stage, DEPTH_SEARCH),
        "chapter_count": int(row.get("toc_count") or 0),
        "toc_complete": toc_ok,
        "toc_fail_reason": "",
        "content_ok": content_ok,
        "content_fail_reason": "",
        "content_response_ms": None,
        "checked_at": checked_at or time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def checks_rows(rows: List[Dict[str, Any]], *, batch: str, checked_at: str = "",
                sources: Optional[Dict[str, Dict[str, Any]]] = None,
                probe=None) -> List[Dict[str, Any]]:
    """一批 App 结论 → checks 行。

    ``sources``：``{url: 源 JSON}``，用来算指纹、静态规则完整度与「有没有搜索规则」
    （给不出的行也能落库，只是星级少了静态回退那一档）。
    DNS 交叉验证**按主机缓存**：同一主机的源常常成批出现（实测 35% 的主机有两条以上）。
    """
    cache: Dict[str, Tuple[str, str]] = {}

    def cached_probe(h: str) -> Tuple[str, str]:
        if h not in cache:
            cache[h] = _run_probe(probe, h)
        return cache[h]

    out: List[Dict[str, Any]] = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        url = _normalize_url(str(r.get("url") or ""))
        src = (sources or {}).get(url)
        item = checks_row(r, batch=batch, checked_at=checked_at,
                          raw=(src or {}).get("raw"), probe=cached_probe)
        if item:
            out.append(item)
    return out

def store_checks(rows: List[Dict[str, Any]], *, batch: str,
                 store=None, probe=None) -> int:
    """把一批 App 结论按 checks 口径落库（含六档 / 星级 / 深度）+ 重建组名。

    **两条路共用这一份**：产品（``POST /api/jvm/run``）与 CLI
    （``scripts/jvm_readback.py``）——各写一份必然漂，而这个端点的两次 500 都是
    「同一件事两处各写一遍」的产物（lessons §六十九）。
    """
    from core.store import Store
    own = store is None
    st = store or Store()
    try:
        # 源 JSON：算指纹、静态规则完整度、有没有搜索规则都要它
        sources: Dict[str, Dict[str, Any]] = {}
        for r in st.conn.execute(
                "SELECT source_url, raw_json FROM sources WHERE deleted_at = ''"):
            url = _normalize_url(str(r["source_url"] or ""))
            if not url:
                continue
            try:
                raw = json.loads(r["raw_json"] or "{}")
            except Exception:
                continue
            sources[url] = {"raw": raw}
        items = checks_rows(rows, batch=batch, sources=sources, probe=probe)
        n = st.save_checks(items)
        if n:
            # 组名是从 checks 推出来的派生字段——观测变了就要重建（AGENTS #11）
            st.rebuild_system_tags([it["url"] for it in items])
        return n
    finally:
        if own:
            st.close()
