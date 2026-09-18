# -*- coding: utf-8 -*-
"""
高性能书源文件加载 / 合并 / 去重。

使用 orjson 解析（C 实现，比标准库 json 快 5~10 倍），
4395 个源 / 30MB 的文件毫秒级完成。
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Set

try:
    import orjson
except ImportError:  # pragma: no cover
    import json as _json

    class orjson:  # type: ignore
        OPT_INDENT_2 = 1
        OPT_NON_STR_KEYS = 2
        OPT_SORT_KEYS = 4

        @staticmethod
        def loads(b):
            return _json.loads(b)

        @staticmethod
        def dumps(o, option=0):
            return _json.dumps(
                o,
                ensure_ascii=False,
                indent=2 if option & orjson.OPT_INDENT_2 else None,
                sort_keys=bool(option & orjson.OPT_SORT_KEYS),
            ).encode("utf-8")


def load_json_file(path: str) -> Any:
    """读取 JSON 文件（支持大文件）。"""
    with open(path, "rb") as f:
        return orjson.loads(f.read())


def dump_json_file(path: str, data: Any, indent: int = 2) -> None:
    """写出 JSON 文件，UTF-8 无 BOM，ensure_ascii=False 保留中文。"""
    if indent:
        raw = orjson.dumps(data, option=orjson.OPT_INDENT_2 | orjson.OPT_NON_STR_KEYS)
    else:
        raw = orjson.dumps(data, option=orjson.OPT_NON_STR_KEYS)
    with open(path, "wb") as f:
        f.write(raw)


def _normalize_url(url: str) -> str:
    """URL 归一化：去空白、尾部斜杠、转小写，用于去重。"""
    return (url or "").strip().rstrip("/").lower()


def _record_key(src: Dict[str, Any]) -> str:
    """书源唯一键：优先 URL，其次名称。"""
    url = _normalize_url(str(src.get("bookSourceUrl", "") or ""))
    if url:
        return f"url:{url}"
    name = str(src.get("bookSourceName", "") or "").strip()
    return f"name:{name}"


def dedupe_sources(sources: List[Dict[str, Any]], prefer_keep: str = "first") -> List[Dict[str, Any]]:
    """
    按 URL（缺失时用名称）去重。

    :param prefer_keep: "first" 保留第一个出现的；"last" 保留最后一个。
    """
    seen: Set[str] = set()
    result: List[Dict[str, Any]] = []
    order = sources if prefer_keep == "first" else reversed(sources)
    for src in order:
        key = _record_key(src)
        if key in seen:
            continue
        seen.add(key)
        result.append(src)
    if prefer_keep == "last":
        result.reverse()
    return result


def merge_sources(
    primary: List[Dict[str, Any]],
    incoming: List[Dict[str, Any]],
    mode: str = "replace",
) -> List[Dict[str, Any]]:
    """
    合并两份书源列表。

    :param mode:
        - "replace": 新增书源覆盖同名/同URL的旧书源（适合更新）
        - "keep":    保留旧书源，忽略同名新书源
        - "both":    都保留（可能导致重复）
    """
    if mode == "both":
        return primary + incoming
    key_to_idx: Dict[str, int] = {}
    result: List[Dict[str, Any]] = []
    for src in primary:
        key = _record_key(src)
        key_to_idx[key] = len(result)
        result.append(src)
    for src in incoming:
        key = _record_key(src)
        if key in key_to_idx:
            if mode == "replace":
                result[key_to_idx[key]] = src  # 覆盖
        else:
            key_to_idx[key] = len(result)
            result.append(src)
    return result


def fingerprint(src: Dict[str, Any]) -> str:
    """书源指纹：对核心字段做 hash，用于检测"内容变了但 URL 没变"。"""
    core = {
        "name": src.get("bookSourceName"),
        "url": _normalize_url(str(src.get("bookSourceUrl", "") or "")),
        "searchUrl": src.get("searchUrl"),
        "ruleSearch": src.get("ruleSearch"),
        "ruleToc": src.get("ruleToc"),
        "ruleContent": src.get("ruleContent"),
        "exploreUrl": src.get("exploreUrl"),
    }
    raw = orjson.dumps(core, option=orjson.OPT_SORT_KEYS | orjson.OPT_NON_STR_KEYS)
    return hashlib.md5(raw).hexdigest()
