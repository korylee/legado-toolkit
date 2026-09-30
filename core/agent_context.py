# -*- coding: utf-8 -*-
"""Build a small, deterministic and redacted context for future Agent actions.

This module deliberately consumes a normalized debug snapshot. It does not decide the
page layer, call a model, perform network I/O, or execute a debug run. The page-layer
判据 remains in :mod:`core.page_layer`; this module only selects the facts that are
allowed to cross the Agent boundary.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit

LAYERS = ("L1", "L2", "L3", "L4", "L5")
_STAT_KEYS = ("links", "images", "images_with_src", "text_len")
_CAPABILITY_KEYS = (
    "jvm_debug", "app_debug", "webview_runtime", "login_context", "cookie_jar",
)
_SECRET_KEY_RE = re.compile(
    r"(?:cookie|authorization|password|passwd|secret|token|api[_-]?key|credential|"
    r"private[_-]?key|cipher(?:text)?|session[_-]?id)", re.I,
)
_SECRET_VALUE_RE = re.compile(
    r"(?:bearer\s+\S+|(?:cookie|authorization|token|secret|password)\s*[:=]\s*\S+|"
    r"\b[A-Fa-f0-9]{32,}\b|\b[A-Za-z0-9+/]{80,}={0,2}\b)", re.I,
)


def _text(value: Any, limit: int = 160) -> str:
    """Normalize a non-sensitive short label without preserving arbitrary markup."""
    value = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)] + "…"


def _sensitive_summary(value: Any) -> dict[str, Any]:
    raw = str(value or "")
    return {
        "type": type(value).__name__,
        "length": len(raw),
        "sha256": hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest(),
    }


def _redact(value: Any, *, key: str = "", limit: int = 160) -> Any:
    """Return a bounded scalar; never copy a value that looks like a secret."""
    if _SECRET_KEY_RE.search(str(key)) or (isinstance(value, str) and _SECRET_VALUE_RE.search(value)):
        return _sensitive_summary(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _text(value, limit)
    return _sensitive_summary(value)


def _path_shape(value: Any) -> str:
    """Keep an endpoint shape while dropping host, query, fragment and credentials."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
        path = parsed.path or ("/" if parsed.netloc else raw)
    except ValueError:
        path = raw.split("?", 1)[0].split("#", 1)[0]
    path = re.sub(r"\b\d{2,}\b", ":id", path)
    path = re.sub(r"/[A-Fa-f0-9]{16,}(?=/|$)", "/:id", path)
    return _text(path, 120)


def _json_shape(value: Any, depth: int = 0) -> Any:
    """Describe JSON structure without copying response values."""
    if depth >= 3:
        return "object"
    if isinstance(value, Mapping):
        keys = sorted(
            _text(key, 60) for key in value
            if not _SECRET_KEY_RE.search(str(key))
        )[:30]
        return {"type": "object", "keys": keys}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        shapes = [_json_shape(item, depth + 1) for item in value[:6]]
        item_types = sorted({
            str(shape.get("type", "object")) if isinstance(shape, Mapping) else str(shape)
            for shape in shapes
        })
        return {"type": "array", "item_types": item_types}
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    return "string"


def _stats(page: Mapping[str, Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    raw = page.get("stats")
    if isinstance(raw, Mapping):
        for key in _STAT_KEYS:
            value = raw.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                out[key] = max(0, int(value))
    return out


def _evidence_refs(raw: Any, *, layer: str) -> list[dict[str, Any]]:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return []
    out = []
    for item in raw[:20]:
        if not isinstance(item, Mapping):
            continue
        ref: dict[str, Any] = {}
        for key in ("kind", "why", "source", "note"):
            value = _redact(item.get(key), key=key, limit=120)
            if value not in ("", None):
                ref[key] = value
        line = item.get("line")
        if isinstance(line, (int, float)) and not isinstance(line, bool):
            ref["line"] = max(0, int(line))
        # L3 snippets are the most likely place for ciphertext to leak. The reason
        # and source line are enough for a later bounded evidence fetch.
        if layer != "L3" and isinstance(item.get("snippet"), str):
            snippet = _redact(item["snippet"], key="snippet", limit=100)
            if isinstance(snippet, str) and snippet:
                ref["snippet"] = snippet
        if ref:
            out.append(ref)
    out.sort(key=lambda item: (
        str(item.get("kind", "")), str(item.get("source", "")),
        int(item.get("line", 0) or 0), str(item.get("why", "")),
    ))
    for index, item in enumerate(out, 1):
        item["id"] = "evidence-%03d" % index
    return out


def _candidate(item: Mapping[str, Any]) -> dict[str, Any] | None:
    kind = _text(item.get("kind"), 32)
    rule = _redact(item.get("rule"), key="rule", limit=180)
    if not kind and not rule:
        return None
    out: dict[str, Any] = {}
    for key, limit in (("kind", 32), ("intent", 48), ("rule", 180), ("status", 32)):
        value = _redact(item.get(key), key=key, limit=limit)
        if value not in ("", None):
            out[key] = value
    for key in ("count", "hits", "uniq"):
        value = item.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            out[key] = max(0, int(value))
    ratio = item.get("ratio")
    if isinstance(ratio, (int, float)) and not isinstance(ratio, bool):
        out["ratio"] = round(max(0.0, min(1.0, float(ratio))), 6)
    samples = item.get("samples")
    if isinstance(samples, Sequence) and not isinstance(samples, (str, bytes)):
        out["samples"] = [_redact(sample, key="sample", limit=120) for sample in samples[:3]]
    return out


def _candidates(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return []
    out = [candidate for item in raw if isinstance(item, Mapping)
           for candidate in [_candidate(item)] if candidate]
    out.sort(key=lambda item: (
        str(item.get("kind", "")), str(item.get("rule", "")),
        int(item.get("count", item.get("hits", 0)) or 0),
    ))
    return out[:8]


def _runtime(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        return {}
    out: dict[str, Any] = {}
    for key in ("channel", "status", "available"):
        value = _redact(raw.get(key), key=key, limit=40)
        if value not in ("", None):
            out[key] = value
    object_types = raw.get("object_types", raw.get("types"))
    if isinstance(object_types, Sequence) and not isinstance(object_types, (str, bytes)):
        out["object_types"] = sorted({_text(value, 50) for value in object_types if value})[:30]
    keys = raw.get("keys", raw.get("object_keys"))
    if isinstance(keys, Sequence) and not isinstance(keys, (str, bytes)):
        out["keys"] = sorted({_text(value, 60) for value in keys if value and not _SECRET_KEY_RE.search(str(value))})[:40]
    counts = raw.get("counts")
    if isinstance(counts, Mapping):
        out["counts"] = {
            _text(key, 50): max(0, int(value))
            for key, value in counts.items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        }
    return out


def _network(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return []
    out = []
    for item in raw[:30]:
        if not isinstance(item, Mapping):
            continue
        entry: dict[str, Any] = {}
        method = _redact(item.get("method"), key="method", limit=12)
        if method:
            entry["method"] = method.upper() if isinstance(method, str) else method
        status = item.get("status", item.get("status_code"))
        if isinstance(status, (int, float)) and not isinstance(status, bool):
            entry["status"] = int(status)
        content_type = _redact(item.get("content_type"), key="content_type", limit=60)
        if content_type:
            entry["content_type"] = content_type
        path = item.get("path_shape", item.get("url"))
        shaped = _path_shape(path)
        if shaped:
            entry["path_shape"] = shaped
        json_shape = item.get("json_shape")
        if isinstance(json_shape, (str, list, dict)):
            entry["json_shape"] = _json_shape(json_shape)
        paths = item.get("candidate_paths")
        if isinstance(paths, Sequence) and not isinstance(paths, (str, bytes)):
            entry["candidate_paths"] = sorted({_path_shape(path) for path in paths if path})[:12]
        if entry:
            out.append(entry)
    out.sort(key=lambda item: (
        str(item.get("method", "")), str(item.get("path_shape", "")),
        int(item.get("status", 0) or 0), str(item.get("content_type", "")),
    ))
    return out[:20]


def _capabilities(raw: Any) -> dict[str, bool]:
    if not isinstance(raw, Mapping):
        return {}
    return {key: bool(raw[key]) for key in _CAPABILITY_KEYS if key in raw}


def _add_gap(gaps: list[dict[str, str]], code: str, reason: str, needed: str = "") -> None:
    if any(item["code"] == code for item in gaps):
        return
    item = {"code": code, "reason": reason}
    if needed:
        item["needed"] = needed
    gaps.append(item)


def _gaps(snapshot: Mapping[str, Any], layer: str, page: Mapping[str, Any],
          runtime: Mapping[str, Any], network: list[dict[str, Any]],
          capabilities: Mapping[str, bool]) -> list[dict[str, str]]:
    gaps: list[dict[str, str]] = []
    supplied = snapshot.get("gaps")
    if isinstance(supplied, Sequence) and not isinstance(supplied, (str, bytes)):
        for item in supplied[:12]:
            if isinstance(item, Mapping) and item.get("code") and item.get("reason"):
                _add_gap(gaps, _text(item["code"], 60), _text(item["reason"], 180), _text(item.get("needed"), 80))
    stats = _stats(page)
    if layer == "L1" and not stats:
        _add_gap(gaps, "page_stats_missing", "没有页面统计，无法确认静态目标是否存在", "page_stats")
    elif layer == "L2" and not (stats or page.get("container") or page.get("shell")):
        _add_gap(gaps, "container_facts_missing", "没有容器或空壳事实，无法确认渲染缺口", "static_page_evidence")
    elif layer == "L3" and not runtime:
        _add_gap(gaps, "runtime_material_missing", "没有运行时对象摘要，无法确认动态数据形状", "jvm_or_app_runtime")
    elif layer == "L4" and not network:
        _add_gap(gaps, "network_material_missing", "没有接口摘要，无法确认取数路径和响应形状", "network_evidence")
    elif layer == "L5":
        login = page.get("login_marker") or snapshot.get("login_marker")
        if not login and not any(capabilities.get(key) for key in ("login_context", "cookie_jar")):
            _add_gap(gaps, "login_context_missing", "没有登录事实或会话能力，无法判断登录墙", "login_marker_or_session")
    gaps.sort(key=lambda item: item["code"])
    return gaps


def build_agent_context(snapshot: Mapping[str, Any] | None) -> dict[str, Any]:
    """Build a bounded Agent context from an already normalized debug snapshot."""
    snapshot = snapshot if isinstance(snapshot, Mapping) else {}
    layer = str(snapshot.get("layer") or "").strip().upper()
    if layer not in LAYERS:
        layer = ""
    target_raw = snapshot.get("target")
    target_raw = target_raw if isinstance(target_raw, Mapping) else {}
    target = {
        "step": _text(target_raw.get("step"), 32),
        "want": _text(target_raw.get("want"), 32),
    }
    step_raw = snapshot.get("step")
    step_raw = step_raw if isinstance(step_raw, Mapping) else {}
    step = {}
    for key, limit in (("verdict", 24), ("reason", 180)):
        value = _redact(step_raw.get(key), key=key, limit=limit)
        if value not in ("", None):
            step[key] = value
    if isinstance(step_raw.get("stale"), bool):
        step["stale"] = step_raw["stale"]
    page = snapshot.get("page")
    page = page if isinstance(page, Mapping) else {}
    runtime = _runtime(snapshot.get("runtime"))
    network = _network(snapshot.get("network"))
    capabilities = _capabilities(
        snapshot.get("capabilities", snapshot.get("source_capabilities"))
    )
    facts: dict[str, Any] = {}
    stats = _stats(page)
    if layer == "L1":
        facts["stats"] = stats
        if isinstance(page.get("has_wanted"), bool):
            facts["has_wanted"] = page["has_wanted"]
    elif layer == "L2":
        facts["container_stats"] = stats
        for key in ("container", "shell"):
            value = _redact(page.get(key), key=key, limit=80)
            if value not in ("", None):
                facts[key] = value
        facts["available_engines"] = capabilities
    elif layer == "L3":
        markers = page.get("markers", page.get("encryption_markers"))
        if isinstance(markers, Sequence) and not isinstance(markers, (str, bytes)):
            facts["markers"] = sorted({_text(marker, 80) for marker in markers if marker})[:20]
        facts["runtime_available"] = bool(runtime)
    elif layer == "L4":
        facts["network_count"] = len(network)
        facts["network_available"] = bool(network)
    elif layer == "L5":
        marker = page.get("login_marker", snapshot.get("login_marker"))
        if marker:
            facts["login_marker"] = _redact(marker, key="login_marker", limit=80)
        facts["login_capabilities"] = {
            key: capabilities[key] for key in ("login_context", "cookie_jar") if key in capabilities
        }
    gaps = _gaps(snapshot, layer, page, runtime, network, capabilities)
    if not layer:
        _add_gap(gaps, "layer_missing", "没有有效的页面 Layer 判定", "page_layer")
    gaps.sort(key=lambda item: item["code"])
    context: dict[str, Any] = {
        "schema_version": 1,
        "layer": layer,
        "target": target,
        "step": step,
        "facts": facts,
        "evidence_refs": _evidence_refs(page.get("evidence"), layer=layer),
        "candidates": _candidates(snapshot.get("candidates")),
        "capabilities": capabilities,
        "runtime": runtime,
        "network": network,
        "gaps": gaps,
    }
    return context


def canonical_context_json(context: Mapping[str, Any]) -> str:
    """Serialize a context for stable comparisons and cache keys."""
    return json.dumps(context, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
