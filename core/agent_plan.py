# -*- coding: utf-8 -*-
"""首屏五格决策的判据：缺口唯一、fix 与 probe 分栏、AI 只认合格材料（规矩见 AGENTS #24）。

判据只此一份：界面、受限 Agent（`core.agent_router`）与以后的批处理都要回答「这一步下一步
做什么」，别处只消费它的输出。

**中文句子不在这里**：这里只出**码**与**动作种类**，取词在消费侧；两侧逐项比对由
`tests/test_agent_plan.py` 钉住（消费侧退场期间钉的是后端自洽，重接见 TODO
`agent-evidence-budget`）。

两条本地约定：`GAP_CODES` 的**元组顺序就是优先级**（只取第一个，其余进 `deferred_gaps`）；
**没给的事实不当成假**（读不到 ≠ 没有，AGENTS #12），每条判据都先确认那个事实真的在上下文里。
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Dict, List, Optional

from core.agent_actions import budget_class, fix_kinds, probe_kinds

#: 缺口码：**元组顺序就是优先级**。前端按这些码取中文句子，契约测试逐码比对。
#: `no_engine` 压过 `rule_missing`：下一步动作不同才分先后——还没跑时唯一能说的
#: 是「先跑一次调试」（跑起来才拿得到页面证据，规则空也会被真实验证）；跑过之后
#: 规则空才是主动作（#24③ 的「全局前置」语义是「换通道白费」，跑调试不是换通道）。
GAP_CODES = (
    "no_engine",          # 还没有真实引擎结果：后面的判断都无从谈起
    "rule_missing",       # 规则为空：跑过之后它就是第一优先
    "page_not_recorded",   # App 没为这一步记录页面
    "no_url",              # App 没请求这一步的页面，也没有可用的章节链接
    "page_fetch_missing",  # 页面没抓回来
    "webview_unsupported", # 这一段走的 WebView 能力本机没覆盖：拿不到材料，不是规则错
    "no_wanted_nodes",     # 这一页没有目标这类节点（改选择器没用）
    "runtime_missing",     # 动态层 + 本机引擎，缺运行时材料
    "no_hit",              # 规则在这份页面上一条都没选中
    "fail_content",        # 取到了值但判定不达标
    "stale",               # 规则改过，旧结论不作数
    "unknown",             # 引擎暂时无法判定
)

#: `fix` 的动作种类（前端据此映射到现成事件；不认识的一律不渲染）。
#: **权威来源在 `core/agent_actions.fix_kinds`**——这里只导入，别处不再抄一份
#: （AGENTS #10：同一份清单只在一处维护，另一处留指针）。
FIX_KINDS = fix_kinds()
#: 改源时改哪儿：补规则 / 换选择器 / 按层模板 / 登录
FIX_TARGETS = ("rule", "selector", "layer", "login")
#: 取证通道（权威来源同上：`core/agent_actions.probe_kinds`）
PROBE_KINDS = probe_kinds()
#: AI 提议的材料形状：喂错材料等于让它在错的东西上生成
MATERIAL_KINDS = {
    "L1": "page_html", "L2": "runtime_dom", "L3": "runtime_object",
    "L4": "network_shape", "L5": "session",
}

#: 需要换通道取材料的缺口：本机引擎拿不到运行时材料的那些
_PROBE_GAPS = frozenset((
    "no_url", "page_fetch_missing", "no_wanted_nodes",
    "runtime_missing", "webview_unsupported", "unknown",
))

#: 「改源就是声明这一层的取数方式」的层
_LAYER_FIX_LAYERS = frozenset(("L2", "L3", "L4"))


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _decision_gaps(context: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """按优先级排出缺口（只出码）。顺序即 `GAP_CODES`。

    事实分两层：**引擎结果**（`verdict` 非空 = 这一步真的跑过）与**引擎结果里
    的观测**（页面记录、命中数…）。App 侧事实缺口（页面没记录 / 没有 URL /
    页面没抓回）只在有引擎结果时判——「还没跑」不是「App 没记录」，没跑的
    时候唯一说得出口的缺口是 `no_engine`（AGENTS #12：没给的事实不当成假）。
    """
    layer = str(context.get("layer") or "")
    target = _mapping(context.get("target"))
    step = _mapping(context.get("step"))
    channel = str(context.get("channel") or "")
    engine_ran = bool(str(step.get("verdict") or "").strip())
    gaps: List[Dict[str, Any]] = []

    def add(code: str) -> None:
        gaps.append({"code": code})

    rule_empty = target.get("rule_empty") is True
    if rule_empty:
        add("rule_missing")
    if not engine_ran:
        add("no_engine")
    if engine_ran:
        if "page_id" in step and not str(step.get("page_id") or "").strip():
            add("page_not_recorded")
        elif "has_url" in step and step.get("has_url") is not True:
            add("no_url")
        elif "has_url" in step and context.get("page_present") is False:
            add("page_fetch_missing")
    if context.get("webview_unsupported") is True:
        # 这一段 App 走了 WebView 的某条能力边界，本机引擎**跑不了**它：给换通道的动作，
        # 不给「改规则」。与上面同族——判据只认显式给的事实（读不到 ≠ 有，见文件头约定）
        add("webview_unsupported")
    if context.get("has_wanted") is False:
        add("no_wanted_nodes")
    if (channel == "jvm" and step.get("verdict") == "unknown"
            and layer not in ("", "L1")):
        add("runtime_missing")
    if not rule_empty:
        # 取值类缺口只看引擎给的事实：取到 0 条 = 规则没选中任何节点；取到了但判不达标 =
        # 问题在内容。**`values_count` 缺失时不给这两档**——没给的事实不当成 0
        # （AGENTS #12），否则「这一步还没跑到取值」会被说成「一条都没选中」。
        verdict = str(step.get("verdict") or "")
        values_raw = step.get("values_count")
        if verdict in ("fail", "unknown") and values_raw is not None:
            add("no_hit" if int(values_raw or 0) == 0 else "fail_content")
    if not gaps and step.get("stale") is True:
        add("stale")
    if not gaps and step.get("verdict") == "unknown":
        add("unknown")
    # 收口排序：add() 的调用顺序跟着判据块走，不保证等于 GAP_CODES——
    # 「元组顺序就是优先级」在这里落地成事实，而不是靠调用方自觉
    order = {code: i for i, code in enumerate(GAP_CODES)}
    return sorted(gaps, key=lambda g: order.get(g["code"], len(order)))


def _first_candidate_rule(context: Mapping[str, Any]) -> str:
    candidates = context.get("candidates")
    if not isinstance(candidates, list):
        return ""
    for item in candidates:
        if isinstance(item, Mapping) and str(item.get("rule") or "").strip():
            return str(item["rule"]).strip()
    return ""


def _layer_fix(layer: str) -> Dict[str, Any]:
    if layer in _LAYER_FIX_LAYERS:
        return {"kind": "edit_rule", "target": "layer", "layer": layer}
    if layer == "L5":
        return {"kind": "edit_rule", "target": "login"}
    return {"kind": "edit_rule", "target": "selector"}


def _fix_for(code: str, layer: str, context: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    if code == "rule_missing":
        return {"kind": "edit_rule", "target": "rule"}
    if code == "no_engine":
        return {"kind": "run_debug"}
    if code in ("no_wanted_nodes", "runtime_missing"):
        return _layer_fix(layer)
    if code == "no_hit":
        rule = _first_candidate_rule(context)
        if rule:
            return {"kind": "apply_candidate", "rule": rule}
        return {"kind": "edit_rule", "target": "selector"}
    if code in ("fail_content", "stale"):
        return {"kind": "rerun"}
    # 页面都没记到 / 规则本地跑不了：没有可改的东西，取证才是唯一动作
    return None


def _probe_for(code: str, channel: str) -> Optional[Dict[str, Any]]:
    """取证动作：本机引擎拿不到材料时换到 App 再观测一次。

    真机已是最后一条通道，所以 `channel == "app"` 时不再给 probe——再给就只剩换回来。
    """
    if channel == "jvm" and code in _PROBE_GAPS:
        return {"kind": "app"}
    return None


def ai_slot(context: Mapping[str, Any], layer: str) -> Dict[str, Any]:
    """AI 补足资格：只看材料，与「层判得对不对」无关。"""
    material = MATERIAL_KINDS.get(layer, "page_html")
    signals = _mapping(context.get("signals"))
    target = _mapping(context.get("target"))
    step = _mapping(context.get("step"))

    def off(reason_code: str, kind: str = "") -> Dict[str, Any]:
        return {"eligible": False, "reason_code": reason_code, "material_kind": kind}

    if target.get("rule_empty") is True:
        return off("rule_missing")
    if signals.get("can_suggest") is False:
        return off("no_page")
    if signals.get("login_wall") is True:
        return off("login_wall", material)
    if signals.get("llm_ready") is False:
        return off("no_model", material)
    if step.get("stale") is True:
        return off("stale", material)
    if layer and layer != "L1":
        return off("material_mismatch", material)
    return {"eligible": True, "reason_code": "", "material_kind": material}


def _route(context: Mapping[str, Any], model_available: bool) -> Mapping[str, Any]:
    """层策略仍由 `core/agent_router` 给（同一份，别在这儿再写一遍）。"""
    from core.agent_router import route_agent_context

    try:
        return route_agent_context(context, model_available=model_available)
    except Exception as exc:  # pragma: no cover - 只在路由自身出错时
        # 路由挂了不该带走整屏：把原因留下，让界面能说清楚（AGENTS #4）
        return {"action": "ask_user", "reason_code": "insufficient_evidence",
                "reason": "路由失败：%s：%s" % (type(exc).__name__, exc),
                "mode": "deterministic", "requires_model": False, "fallback": ""}


def build_plan(context: Mapping[str, Any] | None, *,
               model_available: bool = False,
               routed: Mapping[str, Any] | None = None) -> Dict[str, Any]:
    """五格决策：现状 / 解决 / 取证 / AI 补足。

    `routed` 可传已算好的 `route_agent_context` 结果——同一个上下文只路由一次。
    """
    context = context if isinstance(context, Mapping) else {}
    layer = str(context.get("layer") or "").strip().upper()
    gaps = _decision_gaps(context)
    gap = gaps[0] if gaps else None
    channel = str(context.get("channel") or "")
    route = routed if isinstance(routed, Mapping) else _route(context, model_available)
    target = _mapping(context.get("target"))
    return {
        "layer": layer,
        "target": {"step": str(target.get("step") or ""),
                   "want": str(target.get("want") or "")},
        "gap": gap,
        "deferred_gaps": gaps[1:],
        "fix": _fix_for(gap["code"], layer, context) if gap else None,
        "probe": _probe_for(gap["code"], channel) if gap else None,
        "ai": ai_slot(context, layer),
        "action": str(route.get("action") or ""),
        "reason_code": str(route.get("reason_code") or ""),
        "reason": str(route.get("reason") or ""),
        "mode": str(route.get("mode") or ""),
        "requires_model": bool(route.get("requires_model")),
        "fallback": str(route.get("fallback") or ""),
        "verification": route.get("verification")
        or {"required": True, "method": "user"},
        "evidence_refs": [str(item.get("id"))
                          for item in (context.get("evidence_refs") or [])
                          if isinstance(item, Mapping) and item.get("id")],
    }
