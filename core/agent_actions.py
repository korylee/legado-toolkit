# -*- coding: utf-8 -*-
"""Strict validation for model-produced Agent proposals.

This is a protocol boundary, not an executor. A validated proposal is still only a
suggestion: callers must perform the requested verification before applying any rule
or strategy, and this module never calls the network, App, model, or source store.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence, Set
from typing import Any

from core.agent_context import LAYERS

ACTIONS = (
    "suggest_rule",
    "run_jvm_debug",
    "run_app_debug",
    "inspect_runtime",
    "suggest_api_rule",
    "ask_user",
    "stop_unsupported",
)
REASON_CODES = (
    "candidate_available",
    "needs_runtime",
    "needs_jvm_debug",
    "needs_app_debug",
    "needs_network_evidence",
    "needs_login",
    "insufficient_evidence",
    "rule_not_supported",
    "unsupported_layer",
    "user_confirmation_required",
)
_VERIFICATION_METHODS = ("jvm", "app", "user")
_TOP_LEVEL = {"action", "layer", "reason_code", "proposal", "verification", "evidence_refs"}
_VERIFICATION_FIELDS = {"required", "method"}
_ACTION_FIELDS = {
    "suggest_rule": {"field", "rule", "strategy"},
    "run_jvm_debug": {"target", "query"},
    "run_app_debug": {"target", "query"},
    "inspect_runtime": {"runtime_field", "scope"},
    "suggest_api_rule": {"field", "method", "path", "json_path", "rule", "strategy"},
    "ask_user": {"question", "options"},
    "stop_unsupported": set(),
}
_STRATEGY_FIELDS = {"requires_webview", "runtime_field", "content_mode"}
_FIELD_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,79}$")
_EVIDENCE_ID_RE = re.compile(r"^evidence-[0-9]{3,}$")


class AgentProposalError(ValueError):
    """A machine-readable protocol rejection."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _fail(code: str, message: str) -> None:
    raise AgentProposalError(code, message)


def _strict_text(value: Any, field: str, *, limit: int = 160) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail("invalid_field", "%s 必须是非空字符串" % field)
    value = value.strip()
    if len(value) > limit:
        _fail("field_too_long", "%s 超过 %d 个字符" % (field, limit))
    return value


def _field_name(value: Any, field: str = "field") -> str:
    value = _strict_text(value, field, limit=80)
    if not _FIELD_RE.fullmatch(value):
        _fail("invalid_field", "%s 不是受控字段名" % field)
    return value


def _validate_strategy(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        _fail("invalid_proposal", "strategy 必须是对象")
    unknown = set(value) - _STRATEGY_FIELDS
    if unknown:
        _fail("forbidden_field", "strategy 含越权字段：%s" % ", ".join(sorted(map(str, unknown))))
    out: dict[str, Any] = {}
    if "requires_webview" in value:
        if not isinstance(value["requires_webview"], bool):
            _fail("invalid_field", "strategy.requires_webview 必须是布尔值")
        out["requires_webview"] = value["requires_webview"]
    if "runtime_field" in value:
        out["runtime_field"] = _field_name(value["runtime_field"], "strategy.runtime_field")
    if "content_mode" in value:
        mode = _strict_text(value["content_mode"], "strategy.content_mode", limit=40)
        if mode not in ("text", "media", "html"):
            _fail("invalid_field", "strategy.content_mode 不受支持")
        out["content_mode"] = mode
    if not out:
        _fail("invalid_proposal", "strategy 不能为空")
    return out


def _validate_proposal(action: str, value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        _fail("invalid_proposal", "proposal 必须是对象")
    unknown = set(value) - _ACTION_FIELDS[action]
    if unknown:
        _fail("forbidden_field", "proposal 含越权字段：%s" % ", ".join(sorted(map(str, unknown))))
    out: dict[str, Any] = {}
    if action in ("suggest_rule", "suggest_api_rule"):
        if "field" not in value or "rule" not in value:
            _fail("invalid_proposal", "%s 必须同时提供 field 和 rule" % action)
        out["field"] = _field_name(value["field"])
        out["rule"] = _strict_text(value["rule"], "rule", limit=500)
    if action == "suggest_api_rule":
        if "method" in value:
            method = _strict_text(value["method"], "method", limit=12).upper()
            if method not in ("GET", "POST", "PUT", "DELETE"):
                _fail("invalid_field", "method 不受支持")
            out["method"] = method
        if "path" in value:
            out["path"] = _strict_text(value["path"], "path", limit=240)
        if "json_path" in value:
            out["json_path"] = _strict_text(value["json_path"], "json_path", limit=240)
    if action in ("run_jvm_debug", "run_app_debug"):
        if "target" not in value:
            _fail("invalid_proposal", "%s 必须提供 target" % action)
        target = _strict_text(value["target"], "target", limit=20)
        if target not in ("search", "toc", "content", "explore"):
            _fail("invalid_field", "target 不受支持")
        out["target"] = target
        if "query" in value:
            out["query"] = _strict_text(value["query"], "query", limit=240)
    if action == "inspect_runtime":
        if "runtime_field" not in value:
            _fail("invalid_proposal", "inspect_runtime 必须提供 runtime_field")
        out["runtime_field"] = _field_name(value["runtime_field"], "runtime_field")
        if "scope" in value:
            out["scope"] = _strict_text(value["scope"], "scope", limit=80)
    if action == "ask_user":
        if "question" not in value:
            _fail("invalid_proposal", "ask_user 必须提供 question")
        out["question"] = _strict_text(value["question"], "question", limit=240)
        if "options" in value:
            options = value["options"]
            if not isinstance(options, Sequence) or isinstance(options, (str, bytes)) or not options:
                _fail("invalid_field", "options 必须是非空数组")
            if len(options) > 8:
                _fail("field_too_long", "options 最多包含 8 项")
            out["options"] = [_strict_text(option, "options[]", limit=80) for option in options]
    if "strategy" in value:
        out["strategy"] = _validate_strategy(value["strategy"])
    return out


def _validate_verification(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        _fail("invalid_verification", "verification 必须是对象")
    unknown = set(value) - _VERIFICATION_FIELDS
    if unknown:
        _fail("forbidden_field", "verification 含越权字段：%s" % ", ".join(sorted(map(str, unknown))))
    if value.get("required") is not True:
        _fail("verification_required", "verification.required 必须为 true")
    if "method" not in value:
        _fail("verification_required", "verification.method 必须提供")
    method = _strict_text(value["method"], "verification.method", limit=12)
    if method not in _VERIFICATION_METHODS:
        _fail("invalid_verification", "verification.method 不受支持")
    return {"required": True, "method": method}


def _validate_evidence(value: Any, available: Set[str] | None) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or not value:
        _fail("evidence_required", "evidence_refs 必须是非空数组")
    if len(value) > 20:
        _fail("field_too_long", "evidence_refs 最多包含 20 项")
    refs = []
    for ref in value:
        if not isinstance(ref, str) or not _EVIDENCE_ID_RE.fullmatch(ref):
            _fail("invalid_evidence_ref", "evidence_refs 含无效证据 ID")
        refs.append(ref)
    if len(set(refs)) != len(refs):
        _fail("invalid_evidence_ref", "evidence_refs 不能重复")
    if available is not None:
        unknown = set(refs) - set(available)
        if unknown:
            _fail("unknown_evidence_ref", "Proposal 引用了不存在的证据：%s" % ", ".join(sorted(unknown)))
    return refs


def validate_agent_proposal(
    payload: Mapping[str, Any], *, available_evidence_refs: Set[str] | None = None,
) -> dict[str, Any]:
    """Validate and return a canonical, side-effect-free proposal."""
    if not isinstance(payload, Mapping):
        _fail("invalid_json", "Agent 输出必须是 JSON 对象")
    unknown = set(payload) - _TOP_LEVEL
    missing = _TOP_LEVEL - set(payload)
    if unknown:
        _fail("forbidden_field", "Proposal 含越权顶层字段：%s" % ", ".join(sorted(map(str, unknown))))
    if missing:
        _fail("missing_field", "Proposal 缺少字段：%s" % ", ".join(sorted(missing)))
    action = _strict_text(payload["action"], "action", limit=32)
    if action not in ACTIONS:
        _fail("unknown_action", "未知 action：%s" % action)
    layer = _strict_text(payload["layer"], "layer", limit=4).upper()
    if layer not in LAYERS:
        _fail("invalid_layer", "layer 必须是 L1-L5")
    reason_code = _strict_text(payload["reason_code"], "reason_code", limit=64)
    if reason_code not in REASON_CODES:
        _fail("unknown_reason_code", "未知 reason_code：%s" % reason_code)
    return {
        "action": action,
        "layer": layer,
        "reason_code": reason_code,
        "proposal": _validate_proposal(action, payload["proposal"]),
        "verification": _validate_verification(payload["verification"]),
        "evidence_refs": _validate_evidence(payload["evidence_refs"], available_evidence_refs),
    }


def parse_agent_proposal(
    text: str, *, available_evidence_refs: Set[str] | None = None,
) -> dict[str, Any]:
    """Parse model text and apply the same strict protocol validation."""
    if not isinstance(text, str):
        _fail("invalid_json", "Agent 输出必须是 JSON 文本")
    try:
        payload = json.loads(text)
    except (TypeError, ValueError) as exc:
        _fail("invalid_json", "Agent 输出不是合法 JSON：%s" % exc)
    return validate_agent_proposal(payload, available_evidence_refs=available_evidence_refs)
