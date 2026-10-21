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
    "edit_rule",
    "run_jvm_debug",
    "run_app_debug",
    "inspect_runtime",
    "suggest_api_rule",
    "ask_user",
    "stop_unsupported",
    "rule_try",
    "autofill_propose",
)

#: 每个动作的**预算类**——每轮 Agent 循环的计数维度
#: （``agent-evidence-budget`` 的协议位：字段从第一天就在，实现以后接）。
#: free 不计数；evidence 消耗取证次数（打真实引擎 / 读证据）；
#: model_call 发模型请求（花钱的动作只能由用户显式触发，见 ``/suggest-rule``）。
#: ``rule_try`` 是 free 但**限次**（RULE_TRY_PER_TURN）：daemon 路径不发网络、
#: 不调模型，可 Agent 自查用；拿它刷页面等于变相取证，超限要转 evidence。
ACTION_BUDGET = {
    "suggest_rule": "model_call",
    "edit_rule": "free",
    "run_jvm_debug": "evidence",
    "run_app_debug": "evidence",
    "inspect_runtime": "evidence",
    "suggest_api_rule": "model_call",
    "ask_user": "free",
    "stop_unsupported": "free",
    "rule_try": "free",
    "autofill_propose": "free",
}

#: rule_try 每轮上限（**契约位**：契约测试钉着这个数字的语义——超限转 evidence，
#: 不是拒绝执行）。5 = 一次提议（≤3 条候选）加复查的余量。
RULE_TRY_PER_TURN = 5

#: 每轮模型调用上限（agent-evidence-budget 的判据位：超限转用户动作）。
#: **口径是「一族一次」**：提议按取样类型分族（text / media / link），一族一次调用，
#: 所以 3 = 详情组三族各提一次。写死成 1 的后果是「点一次 AI 补全只有 text 那族被
#: 提议」——用户看到的是「点了没反应」，而原因藏在族边界里。
MODEL_CALLS_PER_TURN = 3

def _steps() -> tuple:
    """调试**步骤名**的白名单：派生自 `core.app_debug.SEGMENT_NAMES` 的取值
    （= 前端 STEP_LABELS 的键），**不另抄一份**——两份清单一改一边不报错，
    只会在某条提案上突然「step 不受支持」。
    与 `core.debug_keys.TARGETS` 不是同一套词表：那边是「调试入口目标」
    （有 info、没有 bookUrl）；惰性导入，agent_actions 本身是叶子模块。
    """
    from core.app_debug import SEGMENT_NAMES
    return tuple(SEGMENT_NAMES.values())


#: 路由动作 → 五格执行动作的对应：``agent_plan`` 的 fix/probe 与前端
#: ``debugAction.ACTION_KINDS`` 是执行侧词表，靠这层映射与路由侧对齐
#: （两份清单没法共享代码，契约测试逐项比对——AGENTS #22⑤）。
#: ``inspect_runtime`` 暂无执行侧实现（前端没有对应的 kind）：映射留空，
#: 有了再加——映射到一个预算类不符的动作等于把取证说成免费。
EXECUTION_ACTIONS = {
    "edit_rule": ("edit_rule",),
    "run_jvm_debug": ("run_debug",),
    "run_app_debug": ("run_debug",),
    "inspect_runtime": (),
    "suggest_rule": ("apply_candidate", "apply_and_rerun"),
    "suggest_api_rule": ("apply_candidate",),
    "ask_user": (),
    "stop_unsupported": (),
    "rule_try": ("try_rule",),
    "autofill_propose": (),
}
REASON_CODES = (
    "candidate_available",
    "rule_missing",
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
    "edit_rule": {"field"},
    "run_jvm_debug": {"target", "query"},
    "run_app_debug": {"target", "query"},
    "inspect_runtime": {"runtime_field", "scope"},
    "suggest_api_rule": {"field", "method", "path", "json_path", "rule", "strategy"},
    "ask_user": {"question", "options"},
    "stop_unsupported": set(),
    "rule_try": {"step", "rule", "field"},
    "autofill_propose": {"step"},
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


def _validate_step(value: Any) -> str:
    """调试步骤名的受控校验（白名单只有 :func:`_steps` 一份）。"""
    step = _strict_text(value, "step", limit=20)
    steps = _steps()
    if step not in steps:
        _fail("invalid_field", "step 不受支持（只能是 %s）" % " / ".join(steps))
    return step


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
    if action == "rule_try":
        if "step" not in value or "rule" not in value:
            _fail("invalid_proposal", "rule_try 必须同时提供 step 和 rule")
        out["step"] = _validate_step(value["step"])
        out["rule"] = _strict_text(value["rule"], "rule", limit=500)
        if "field" in value:
            out["field"] = _field_name(value["field"])
    if action == "autofill_propose":
        if "step" not in value:
            _fail("invalid_proposal", "autofill_propose 必须提供 step")
        out["step"] = _validate_step(value["step"])
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


def budget_class(action: str) -> str:
    """动作的预算类；不认识的按 free 兜底（调用方本就该先验 action）。"""
    return ACTION_BUDGET.get(str(action or ""), "free")


def fix_kinds() -> tuple:
    """解决类动作——``agent_plan.FIX_KINDS`` 的权威来源。

    五格 fix 是**执行侧**词表（前端 ``debugAction.ACTION_KINDS`` 的子集），
    从路由动作的映射展开得到：路由说「该 edit_rule」，执行侧能落的是哪几个 kind
    由这层映射钉住。
    """
    out: list[str] = []
    for action in ("edit_rule", "run_jvm_debug", "run_app_debug"):
        for kind in EXECUTION_ACTIONS.get(action, ()):
            if kind not in out:
                out.append(kind)
    # rerun 不来自路由（它是「结果已过期」的执行动作），执行侧补上
    if "rerun" not in out:
        out.append("rerun")
    return tuple(out)


def probe_kinds() -> tuple:
    """取证类动作——``agent_plan.PROBE_KINDS`` 的权威来源（换通道观测的通道词表）。"""
    return ("app", "jvm")
