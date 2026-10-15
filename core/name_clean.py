# -*- coding: utf-8 -*-
"""Configuration-driven book-source display-name cleanup."""
from __future__ import annotations

import json
import regex as re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "name_clean.json"


def _compile_rule(pattern: str, flags: int = 0):
    return re.compile(pattern, flags)


def _replace_rule(pattern: str, replacement: str, text: str, flags: int = 0):
    return re.subn(pattern, replacement, text, flags=flags)


def _flags(value: str) -> int:
    out = 0
    for flag in str(value or ""):
        if flag == "i":
            out |= re.IGNORECASE
        elif flag == "m":
            out |= re.MULTILINE
        elif flag == "s":
            out |= re.DOTALL
        else:
            raise ValueError("不支持的正则标志：%s" % flag)
    return out


def load_config(path=CONFIG_PATH) -> Dict[str, Any]:
    target = Path(path)
    try:
        config = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError("名称清洗配置无法读取（%s）：%s" % (target, exc)) from exc
    if not isinstance(config, dict) or not isinstance(config.get("rules"), list):
        raise ValueError("名称清洗配置必须包含 rules 数组：%s" % target)

    rules = []
    ids = set()
    for item in config["rules"]:
        if not isinstance(item, dict):
            raise ValueError("名称清洗规则必须是对象：%s" % target)
        rule_id = str(item.get("id", "")).strip()
        pattern = str(item.get("pattern", ""))
        if not rule_id or not pattern or rule_id in ids:
            raise ValueError("名称清洗规则缺少 id/pattern 或 id 重复：%s" % target)
        flags = _flags(item.get("flags", ""))
        try:
            _compile_rule(pattern, flags | re.VERSION1)
        except re.error as exc:
            raise ValueError("名称清洗规则 %s 的正则无效：%s" % (rule_id, exc)) from exc
        confidence = float(item.get("confidence", 0.85))
        if not 0 <= confidence <= 1:
            raise ValueError("名称清洗规则 %s 的 confidence 必须在 0 到 1 之间" % rule_id)
        ids.add(rule_id)
        rules.append({
            "id": rule_id,
            "name": str(item.get("name", rule_id)),
            "description": str(item.get("description", "")),
            "pattern": pattern,
            "replacement": str(item.get("replacement", "")),
            "flags": str(item.get("flags", "")),
            "enabled": bool(item.get("enabled", True)),
            "confidence": confidence,
        })
    default_confidence = float(config.get("default_confidence", 0.95))
    if not 0 <= default_confidence <= 1:
        raise ValueError("名称清洗 default_confidence 必须在 0 到 1 之间：%s" % target)
    return {"default_confidence": default_confidence, "rules": rules}


def clean_source_name(name: Any, rules: Optional[Iterable[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Run enabled regex rules in configuration order; return an explainable suggestion."""
    original = str(name or "")
    text = original
    reasons: List[str] = []
    confidences: List[float] = []
    active_rules = list(rules) if rules is not None else load_config()["rules"]

    for rule in active_rules:
        if not rule.get("enabled", True):
            continue
        rule_id = str(rule["id"])
        try:
            text, count = _replace_rule(
                str(rule["pattern"]), str(rule.get("replacement", "")), text,
                flags=_flags(rule.get("flags", "")) | re.VERSION1)
        except re.error as exc:
            raise ValueError("名称清洗规则 %s 执行失败：%s" % (rule_id, exc)) from exc
        if count:
            reasons.append(rule_id)
            confidences.append(float(rule.get("confidence", 0.85)))

    if not text.strip():
        return {"name": original, "confidence": 0.3,
                "reasons": reasons + ["no_safe_fallback"], "changed": False}
    confidence = min(confidences) if confidences else 1.0
    return {"name": text, "confidence": round(confidence, 2),
            "reasons": list(dict.fromkeys(reasons)), "changed": text != original}


def clean_sources(sources, rules=None):
    """Preview a batch using one shared ordered rule configuration."""
    items = [item for item in (sources or []) if isinstance(item, dict)]
    out = []
    for source in items:
        url = str(source.get("bookSourceUrl", "") or "")
        old_name = str(source.get("bookSourceName", "") or "")
        result = clean_source_name(old_name, rules)
        out.append({
            "url": url,
            "old_name": old_name,
            "new_name": result["name"],
            "confidence": result["confidence"],
            "reasons": result["reasons"],
            "changed": result["changed"],
        })
    return out
