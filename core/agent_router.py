# -*- coding: utf-8 -*-
"""Deterministic routing for the constrained Agent workflow.

The router chooses a next action from an already normalized AgentContext. It never
calls a model, network, App, debugger, or source store. A returned plan is guidance,
not a verified result and must still pass the AgentProposal protocol before a model
proposal is accepted.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any


_LAYERS = {"L1", "L2", "L3", "L4", "L5"}


def _capabilities(context: Mapping[str, Any]) -> Mapping[str, Any]:
    value = context.get("capabilities")
    return value if isinstance(value, Mapping) else {}


def _evidence_refs(context: Mapping[str, Any]) -> list[str]:
    raw = context.get("evidence_refs")
    if not isinstance(raw, list):
        return []
    return [str(item.get("id")) for item in raw
            if isinstance(item, Mapping) and item.get("id")]


def _target(context: Mapping[str, Any]) -> tuple[str, str]:
    raw = context.get("target")
    if not isinstance(raw, Mapping):
        return "", ""
    return str(raw.get("step") or ""), str(raw.get("want") or "")


def _engine_action(context: Mapping[str, Any], *, prefer_app: bool = False) -> tuple[str, str] | None:
    capabilities = _capabilities(context)
    order = ("app_debug", "jvm_debug") if prefer_app else ("jvm_debug", "app_debug")
    for capability in order:
        if capabilities.get(capability) is True:
            return ("run_app_debug" if capability == "app_debug" else "run_jvm_debug", capability)
    return None


def _runtime_field(runtime: Mapping[str, Any]) -> str:
    explicit = runtime.get("runtime_field")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    keys = runtime.get("keys", runtime.get("object_keys"))
    if not isinstance(keys, list):
        return ""
    values = [str(value).strip() for value in keys if str(value).strip()]
    preferred = ("params.chapter_images", "chapter_images", "params.content", "content")
    return next((name for name in preferred if name in values), values[0] if values else "")


def _verification_method(context: Mapping[str, Any], *, prefer_app: bool = False) -> str:
    engine = _engine_action(context, prefer_app=prefer_app)
    return engine[1].removesuffix("_debug") if engine else "user"


def _base(context: Mapping[str, Any], action: str, reason_code: str,
          reason: str, *, mode: str, requires_model: bool,
          method: str = "user") -> dict[str, Any]:
    step, want = _target(context)
    return {
        "layer": str(context.get("layer") or "").strip().upper(),
        "step": step,
        "want": want,
        "action": action,
        "reason_code": reason_code,
        "reason": reason,
        "mode": mode,
        "requires_model": requires_model,
        "proposal": None,
        "verification": {"required": True, "method": method},
        "evidence_refs": _evidence_refs(context),
    }


def route_agent_context(context: Mapping[str, Any] | None,
                        *, model_available: bool = False) -> dict[str, Any]:
    """Return one deterministic next-action plan for a normalized AgentContext.

    ``model_available`` only affects whether an unresolved candidate/API case may
    be handed to the Agent. It never changes the Layer verdict or executes work.
    The plan deliberately exposes ``fallback`` when a preferred engine is absent,
    so callers can render a truthful manual next step instead of silently stopping.
    """
    context = context if isinstance(context, Mapping) else {}
    layer = str(context.get("layer") or "").strip().upper()
    candidates = context.get("candidates")
    candidates = candidates if isinstance(candidates, list) else []
    network = context.get("network")
    network = network if isinstance(network, list) else []
    runtime = context.get("runtime")
    runtime = runtime if isinstance(runtime, Mapping) else {}

    # 规则为空是**全局前置**（与层无关）：规则没写，换通道也读不出东西。
    # 所以它排在层判定之前——不然 L3 上会给出「先取运行时材料」那种白费的动作。
    target_raw = context.get("target")
    if isinstance(target_raw, Mapping) and target_raw.get("rule_empty") is True:
        plan = _base(context, "edit_rule", "rule_missing",
                     "这一步没有可用的取值规则，先补规则；换通道也读不出东西",
                     mode="deterministic", requires_model=False)
        plan["fallback"] = "补一条规则后重新调试"
        return plan

    if layer not in _LAYERS:
        plan = _base(context, "stop_unsupported", "unsupported_layer",
                     "没有有效的页面 Layer 判定，暂不能选择自动动作",
                     mode="deterministic", requires_model=False)
        plan["fallback"] = "补充页面 Layer 证据"
        return plan

    if layer == "L1":
        candidate = next((item for item in candidates
                          if isinstance(item, Mapping) and item.get("rule")), None)
        if candidate:
            method = _verification_method(context)
            plan = _base(context, "suggest_rule", "candidate_available",
                         "已有本地候选，可先用规则初筛，再交真实引擎验收",
                         mode="deterministic", requires_model=False, method=method)
            plan["candidate"] = dict(candidate)
            return plan
        plan = _base(context, "suggest_rule", "insufficient_evidence",
                     "本地候选尚未收敛，需要补充规则候选或解释缺口",
                     mode="agent" if model_available else "deterministic",
                     requires_model=model_available)
        plan["fallback"] = "补充列表/字段证据后重新生成候选"
        return plan

    if layer == "L2":
        engine = _engine_action(context)
        if engine:
            action, method = engine
            return _base(context, action, "needs_runtime",
                         "页面有容器但缺少运行时内容，先用真实引擎取得页面材料",
                         mode="deterministic", requires_model=False, method=method.removesuffix("_debug"))
        plan = _base(context, "ask_user", "user_confirmation_required",
                     "页面需要运行时材料，但当前没有可用的本机或 App 调试通道",
                     mode="deterministic", requires_model=False)
        plan["fallback"] = "选择本机调试或连 App 调试"
        return plan

    if layer == "L3":
        from core.agent_pocket_comic import build_pocket_comic_strategy

        pocket = build_pocket_comic_strategy(context)
        if pocket.get("ready"):
            plan = _base(context, "suggest_rule", "candidate_available",
                         "已确认章节运行时图片可访问，可生成正文图片草稿；仍需 App 实测验收",
                         mode="deterministic", requires_model=False,
                         method="app")
            plan["strategy"] = pocket["strategy"]
            plan["draft"] = pocket["draft"]
            plan["verification_note"] = pocket["verification_note"]
            return plan
        if runtime:
            runtime_field = _runtime_field(runtime)
            if runtime_field:
                plan = _base(context, "inspect_runtime", "needs_runtime",
                             "页面存在动态或加密迹象，已有运行时摘要，先检查运行时对象",
                             mode="deterministic", requires_model=False, method="user")
                plan["runtime_field"] = runtime_field
                return plan
            plan = _base(context, "ask_user", "insufficient_evidence",
                         "已有运行时摘要但没有具体字段名，暂不能选择检查对象",
                         mode="deterministic", requires_model=False)
            plan["fallback"] = "指定要检查的运行时字段"
            return plan
        engine = _engine_action(context, prefer_app=True)
        if engine:
            action, method = engine
            return _base(context, action, "needs_app_debug" if method == "app_debug" else "needs_jvm_debug",
                         "页面存在动态或加密迹象，不能从密文猜算法，先取得运行时材料",
                         mode="deterministic", requires_model=False, method=method.removesuffix("_debug"))
        plan = _base(context, "ask_user", "user_confirmation_required",
                     "页面存在动态或加密迹象，但当前没有可用的运行时调试通道",
                     mode="deterministic", requires_model=False)
        plan["fallback"] = "选择本机调试或连 App 调试"
        return plan

    if layer == "L4":
        if network:
            plan = _base(context, "suggest_api_rule", "needs_network_evidence",
                         "已有接口形状证据，可据此生成接口候选；候选仍需真实引擎验收",
                         mode="agent" if model_available else "deterministic",
                         requires_model=model_available,
                         method=_verification_method(context))
            plan["fallback"] = "根据接口路径和响应形状手动填写候选接口规则"
            return plan
        engine = _engine_action(context)
        if engine:
            action, method = engine
            return _base(context, action, "needs_network_evidence",
                         "页面疑似接口取数但还没有网络证据，先取得受控接口摘要",
                         mode="deterministic", requires_model=False, method=method.removesuffix("_debug"))
        plan = _base(context, "ask_user", "user_confirmation_required",
                     "需要网络证据才能生成接口候选，但当前没有可用调试通道",
                     mode="deterministic", requires_model=False)
        plan["fallback"] = "选择本机调试或连 App 调试"
        return plan

    # L5 is a source/session fact, not something the model may infer.
    plan = _base(context, "ask_user", "needs_login",
                 "页面包含登录事实，需要用户选择会话或登录上下文",
                 mode="deterministic", requires_model=False)
    plan["fallback"] = "选择已有登录会话，或确认暂不处理登录墙"
    return plan


# Short alias for callers that already use the route-* naming convention.
route_agent = route_agent_context
