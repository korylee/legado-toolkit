# -*- coding: utf-8 -*-
"""Agent 调试循环：观察 → 提议 → rule_try 自查 → 交回建议表。

它与 ``core.autofill`` 的分工：那边是「一次请求出整表」（免费初选的用户入口），
这边是**带自查的循环**——模型提议按取样类型分组一次给出（一族字段共享 DOM
大纲），每条规则先经 :func:`core.rule_try` 快验，取不到的当场换下一条候选；
换的也不行时该行**只留原因、不出规则文本**（不装成功，也不把已知不可用的
规则摆进界面）。可用的提议**升格为普通建议**（``rule`` 落位）。
用户看到的每行都带 ``try_result``，最终验收仍走「重调本步验证」
（AGENTS #3：rule_try 只回答「选不选得中」，不是验收）。

预算纪律（``core.agent_actions``，AGENTS #24 的执行侧）：
  - 模型调用每轮 ≤ ``MODEL_CALLS_PER_TURN``（按 kind 分组后至多几族，
    超限把剩余字段如实标「未提议」）；
  - rule_try 每轮 ≤ ``RULE_TRY_PER_TURN``（超限剩余行标「未自查」，不静默）；
  - 整个循环是**一次用户显式点击**触发的同步编排——没有后台自动循环。

失败原因纪律（AGENTS #4）：模型没配 / 调用失败 / 没给候选，原因原样写进
``budget.model_stop`` 一路走到界面，**不冒充「次数已用完」**。

入口 :func:`run_loop` 收齐全部材料（html / structured / source / base_url），
无模块级状态——两个用户同时点互不污染。「一键生成源」复用它时给伪调试材料
（pages + structured 同形状）即可。
"""
from __future__ import annotations

import asyncio
from typing import Any, Callable, Dict, List, Optional, Tuple

from core import agent_actions
from core.autofill import (
    FIELD_LABELS,
    autofill_detail,
    _candidates_for_ai,
    _engine_values,
    _field_kind,
)


async def run_loop(html: str, structured: Dict[str, Any], source: Dict[str, Any],
                   base_url: str, step: str = "bookUrl",
                   fields: Optional[List[str]] = None,
                   on_event: Optional[Callable[..., None]] = None,
                   on_llm: Optional[Callable[[Dict[str, Any]], None]] = None,
                   ) -> Dict[str, Any]:
    """跑一轮 Agent 循环。返回体与一键填充的 ``fields`` 同形（前端零改动渲染），
    每行多带 ``try_result``（rule_try 的结果 dict，或 None = 预算用尽未自查）。

    ``fields`` 限制只提议这些字段（FILL_SPECS 的蛇形键；None = 整组）——单字段
    「AI 重议」复用同一份循环，一颗按钮只影响一个字段的行。

    **两个观察口都是可选的**（不传就是纯计算，离线测试与别的调用方零负担）：
      - ``on_event(kind, **fields)``：过程事实（``ai_family`` / ``ai_try`` /
        ``ai_accepted`` / ``ai_failed``）——任务体据此写事件账本，界面吃流就有
        逐字段进度；
      - ``on_llm(record)``：一次模型调用的**输入输出**（提示词、模型原样输出、
        解析结果、token 用量）——任务体据此写 ``llm.jsonl``。
    两个回调抛异常都**不带崩**这次循环：账本是排障材料，不是运行的前置条件。

    提议段**按取样类型分组**（``FILL_SPECS`` 的 kind，text/media/link）：
    同族一次模型调用共享 DOM 大纲——逐字段问的代价不只是 N 倍 token，同一份
    90 行大纲重复喂 N 遍、延迟也是 N 次往返。落进建议的都经 rule_try 自查：
    可用的升格为普通建议；**验不过的不出规则文本**（用户实测：填的几乎都不可用，
    不如空着），只留原因。

    预算消耗与触顶标记在 ``budget``：``{model_calls, rule_tries,
    model_limit_reached, try_limit_reached, model_stop}``——``model_stop`` 是
    「整段 AI 为什么停了」（模型没配/调用失败/没给候选，原样原因），前端照它显示；
    ``model_limit_reached`` 只在真花满 ``MODEL_CALLS_PER_TURN`` 时为真。
    字段表按 ``core.autofill.FILL_SPECS[step]`` 展开（表扩即扩）。
    """
    from core.rule_try import rule_try as run_rule_try

    def _emit(event_kind: str, /, **payload: Any) -> None:
        # 事件名位置专用 + 形参不叫 kind / fields：事件数据里就有这两个键
        # （提议族的取样类型、这一族要问的字段），可被关键字命中的形参会撞名，
        # 而兜底 except 会把「got multiple values for argument」变成「账本少一类行」
        if on_event is None:
            return
        try:
            on_event(event_kind, **payload)
        except OSError:
            pass  # 账本写不进去不拦这次运行（纪律同 run_events）
        except Exception as e:
            # 我们自己的 bug 不许静默：这一行就是「事件账本里少了一类行」的唯一线索
            print("[agent_loop] 事件回调失败：%s: %s" % (type(e).__name__, e))

    out = autofill_detail(html, structured, step=step)
    rows: List[Dict[str, Any]] = out["fields"]
    if fields is not None:
        wanted = {str(f) for f in fields}
        rows = [r for r in rows if r.get("field") in wanted]
        out["fields"] = rows
    budget = {"model_calls": 0, "rule_tries": 0, "model_limit_reached": False,
              "try_limit_reached": False, "model_stop": ""}

    async def _try(rule: str) -> Optional[Dict[str, Any]]:
        """rule_try 一次（预算内），返回结果或 None（预算尽）。

        ``rule_try`` 是同步的（socket，最坏回落 ``subprocess.run`` 十分钟），而任务体
        跑在唯一的事件循环里：直接调会把 `/api/jobs/*` 与所有界面请求一起冻住，
        而「没有在跑的任务」正是别的请求要读的事实。放到线程池里。
        """
        if budget["rule_tries"] >= agent_actions.RULE_TRY_PER_TURN:
            budget["try_limit_reached"] = True
            return None
        budget["rule_tries"] += 1
        return await asyncio.to_thread(run_rule_try, source, step, html, base_url, rule)

    def _usable(checked: Optional[Dict[str, Any]]) -> bool:
        return bool(checked and checked.get("ok") and checked.get("count"))

    def _accept(row: Dict[str, Any], rule: str, why: str,
                checked: Optional[Dict[str, Any]]) -> None:
        """可用的提议**升格为普通建议**：rule 落位、ref_only/need_ai 撤掉。

        前端的写入路径只认 ``row.rule``——在数据上完成升格，比让前端为
        「AI 行」另开一条渲染路径简单且不会漏。``ai`` 留着记来源，
        ``try_result`` 带自查结论。
        """
        row["rule"] = rule
        row.pop("ref_only", None)
        row.pop("need_ai", None)
        row["ai"] = {"rule": rule, "why": why}
        row["try_result"] = checked

    # ---- 提议段（只对免费段没把握的行花钱；按 kind 分组，一族一次调用）----
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        if row.get("rule"):
            continue  # 免费段拍板的行：跳过提议，下面自查
        if budget["model_stop"]:
            continue  # 整段 AI 已停：原因在 budget.model_stop
        kind = _field_kind(step, row["field"])
        groups.setdefault(kind, []).append(row)
    for kind, group in groups.items():
        if budget["model_stop"]:
            continue  # 整段 AI 已停：不再为后面的族花钱，原因在 budget.model_stop
        if budget["model_calls"] >= agent_actions.MODEL_CALLS_PER_TURN:
            budget["model_limit_reached"] = True
            continue
        group_fields = [r["field"] for r in group]
        # 取样类型不能叫 kind：那是事件账本的保留键（事件名），撞了会把事件名换掉
        _emit("ai_family", sample_kind=kind, fields=group_fields)
        proposals, stop = await _propose_group(html, structured, step, kind, group,
                                               on_llm=on_llm)
        if proposals is None:
            # llm 未配置 / 调用失败 / 输出不是 JSON：停整段 AI（同因，逐行报是噪音），
            # 但原因**原样带出**（AGENTS #4）——不冒充「次数已用完」
            budget["model_stop"] = stop
            _emit("ai_family_failed", sample_kind=kind, fields=group_fields,
                  reason=str(stop)[:200])
            continue
        budget["model_calls"] += 1
        for row in group:
            field = row["field"]
            cands = proposals.get(field) or []
            accepted = False
            for cand in cands:
                rule = str(cand.get("rule") or "")
                why = str(cand.get("why") or "")
                checked = await _try(rule)
                _emit("ai_try", field=field, rule=rule, ok=bool(checked and checked.get("ok")),
                      count=int((checked or {}).get("count") or 0),
                      error=str((checked or {}).get("error") or "")[:200])
                if _usable(checked):
                    _accept(row, rule, why, checked)
                    accepted = True
                    break
            if accepted:
                _emit("ai_accepted", field=field, rule=str(row.get("rule") or ""),
                      count=int((row.get("try_result") or {}).get("count") or 0))
                continue
            # 模型的都不中：换候选头名（免费，不另花模型调用）
            tried = {str(c.get("rule") or "") for c in cands}
            alt = next((c for c in _candidates_for_ai(html, kind)
                        if str(c.get("rule") or "") and str(c["rule"]) not in tried), None)
            if alt:
                checked2 = await _try(str(alt["rule"]))
                _emit("ai_try", field=field, rule=str(alt["rule"]),
                      ok=bool(checked2 and checked2.get("ok")),
                      count=int((checked2 or {}).get("count") or 0),
                      error=str((checked2 or {}).get("error") or "")[:200])
                if _usable(checked2):
                    _accept(row, str(alt["rule"]),
                            "模型提议未选中，改用候选头名（轻量验证取到值）", checked2)
                    _emit("ai_accepted", field=field, rule=str(row.get("rule") or ""),
                          count=int((row.get("try_result") or {}).get("count") or 0))
                    continue
            # 都取不到：**不出规则文本**（宁空着），只留原因——落在输入框旁边的
            # 已知不可用规则正是「不如不填」的那个噪音
            row.pop("ai", None)
            row["ai_failed"] = True
            row["reason"] = ("模型提议了 %d 条，轻量验证都没选中" % len(cands)
                             if cands else "模型没有给出该字段的候选")
            row["try_result"] = None
            _emit("ai_failed", field=field, reason=str(row["reason"])[:200])

    # ---- 免费段有把握的行：程序分数 + 引擎真跑双重确认（成本低、价值高）----
    for row in rows:
        if row.get("rule") and "try_result" not in row:
            row["try_result"] = await _try(str(row["rule"]))

    out["budget"] = budget
    return out


def run_loop_sync(html: str, structured: Dict[str, Any], source: Dict[str, Any],
                  base_url: str, step: str = "bookUrl",
                  fields: Optional[List[str]] = None) -> Dict[str, Any]:
    """:func:`run_loop` 的同步版：端点（``/agent-loop`` 走线程池，避免
    rule_try 的同步调用卡住事件循环）与测试共用这一份判据。"""
    return asyncio.run(run_loop(html, structured, source, base_url, step, fields))


async def _propose_group(html: str, structured: Dict[str, Any], step: str,
                         kind: str, group: List[Dict[str, Any]],
                         on_llm: Optional[Callable[[Dict[str, Any]], None]] = None,
                         ) -> Tuple[Optional[Dict[str, List[Dict[str, str]]]], str]:
    """一族字段（同取样类型）的模型提议。返回 ``({field: [candidates]}, "")`` 或
    ``(None, 终止原因)``。

    ``FIELD_LABELS`` 在这里是**给人看的标签**（提示词里的「要的是哪个字段」），
    权威词表在前端 RULE_FIELD_DEFS / Kotlin BOOKINFO_FIELD_LABELS；取样类型
    取自 FILL_SPECS（:func:`core.autofill._field_kind`），不另存一份。

    ``on_llm`` 收这一次调用的**输入输出原文**（提示词 / 模型原样输出 / 解析结论 /
    usage）——调用方负责落账本；抛异常不拦这次运行。

    失败原因**原样带出**（suggest 的 ``error`` 本来就是写给用户看的：没配模型 /
    调用失败 / 输出不是 JSON）——上层不再把它压成「次数已用完」（AGENTS #4）。
    """
    from core.repair.suggest import suggest_many

    try:
        proposals = await suggest_many({
            "html": html,
            "step": step,
            "step_label": "、".join(FIELD_LABELS.get(r["field"], r["field"])
                                    for r in group),
            "fields": [
                {"field": r["field"],
                 "want_label": FIELD_LABELS.get(r["field"], r["field"]),
                 "app_values": _engine_values(structured, r["field"], step=step)}
                for r in group
            ],
        })
    except Exception as e:
        return None, "模型提议没跑成：%s: %s" % (type(e).__name__, str(e)[:120])
    if on_llm is not None:
        try:
            on_llm({"kind": kind, "fields": [r["field"] for r in group],
                    "llm": proposals.get("llm"), "model": proposals.get("model"),
                    "usage": proposals.get("usage") or {},
                    "prompt": proposals.get("prompt") or "",
                    "response": proposals.get("response") or "",
                    "error": proposals.get("error") or ""})
        except OSError:
            pass  # 账本写不进去不拦这次运行
        except Exception as e:
            print("[agent_loop] 调用账本回调失败：%s: %s" % (type(e).__name__, e))
    if proposals.get("llm") != "ok":
        return None, str(proposals.get("error") or "模型没有给出候选")
    return proposals.get("fields") or {}, ""
