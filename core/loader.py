# -*- coding: utf-8 -*-
"""
书源 JSON 的读写与指纹（导入 / 导出共用）。

使用 orjson 解析（C 实现，比标准库 json 快 5~10 倍），
4395 个源 / 30MB 的文件毫秒级完成。
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict

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
