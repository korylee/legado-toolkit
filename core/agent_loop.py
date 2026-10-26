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
    field_kind,
)

#: 送模型的候选数据源条数上限（按「像数据」排序后的前 N）。实测一页 8-9 条，
#: 真实站点会更多；12 条足够覆盖，又不至于把键名段落撑爆。
RUNTIME_PROMPT_ITEMS = 12


async def run_loop(html: str, structured: Dict[str, Any], source: Dict[str, Any],
                   base_url: str, step: str = "bookUrl",
                   fields: Optional[List[str]] = None,
                   on_event: Optional[Callable[..., None]] = None,
                   on_llm: Optional[Callable[[Dict[str, Any]], None]] = None,
                   runtime: Optional[Dict[str, Any]] = None,
                   material_kind: str = "",
                   page_stats: Optional[Dict[str, Any]] = None,
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

    out = autofill_detail(html, structured, step=step, material_kind=material_kind)
    rows: List[Dict[str, Any]] = out["fields"]
    if fields is not None:
        wanted = {str(f) for f in fields}
        rows = [r for r in rows if r.get("field") in wanted]
        out["fields"] = rows
    budget = {"model_calls": 0, "rule_tries": 0, "model_limit_reached": False,
              "try_limit_reached": False, "model_stop": ""}
    # 快验预算按**要提议的字段数**给：整段补全一次问好几个字段，固定 5 次会在最后一个
    # 字段上耗尽，把「没来得及验」写成「模型提议的都没选中」（判据在
    # `core.agent_actions.rule_try_budget`）。
    try_budget = agent_actions.rule_try_budget(len(rows))



    async def _try(rule: str, field: str = "") -> Optional[Dict[str, Any]]:
        """rule_try 一次（预算内），返回结果或 None（预算尽）。

        放线程池：`rule_try` 是同步的（最坏回落 `subprocess.run` 十分钟），而任务体跑在
        唯一的事件循环里，直调会把所有界面请求一起冻住。闸都在 `core.rule_try`。
        """
        from core.rule_try import fast_verify, refusal_code, refusal_result

        src = _container_source(step, rows, source)
        why = refusal_code(step, field, material_kind, rule, runtime, src)
        if why:
            # 不是失败，是**验不了**：规则照落、标 pending_engine，验收交重调本步
            return refusal_result(why)
        if budget["rule_tries"] >= try_budget:
            budget["try_limit_reached"] = True
            return None
        budget["rule_tries"] += 1
        return await asyncio.to_thread(fast_verify, src, step, field, html, base_url,
                                       rule, material_kind, runtime)

    def _single_item(checked: Dict[str, Any], field: str) -> bool:
        """这条候选是**列表容器且只选中 1 条**吗（判据在 `core.rule_try`）。

        容器该是「一个条目一条」：只选中 1 条说明规则写成了**整块容器**，引擎会把整块
        当一个条目、把几百章挤进一条（实测 2026-10-26 发目目录页：整块 count=1，
        按条目 count=233）。这种候选**不收**——收下比空着更坏：它会让后面「重调验收」
        看起来通过，而目录只有一章。
        """
        from core.autofill import field_kind

        return (field_kind(step, field) == "list"
                and int(checked.get("count") or 0) == 1)

    def _usable(checked: Optional[Dict[str, Any]], field: str = "") -> bool:
        """这条候选能不能收下。

        **快验答不忠实（`unfaithful`）时也收**：它只有否决权——收下但不升格
        （`_accept` 里标 `pending_engine`，验收仍看重调本步）。
        例外是列表容器只选中 1 条（`:func:`_single_item`）：那不是「验不了」，
        是**验出来了、结果是错的形态**，收下会让验收看起来通过。
        """
        if not checked:
            return False
        if field and _single_item(checked, field):
            return False
        if checked.get("unfaithful"):
            return True
        return bool(checked.get("ok") and checked.get("count"))

    def _accept(row: Dict[str, Any], rule: str, why: str,
                checked: Optional[Dict[str, Any]]) -> None:
        """收下提议：rule 落位、ref_only/need_ai 撤掉。

        前端的写入路径只认 ``row.rule``——在数据上完成落地，比让前端为「AI 行」
        另开一条渲染路径简单且不会漏。``ai`` 留着记来源，``try_result`` 带自查结论。

        **快验答不忠实时只落地、不升格**（`pending_engine`）：前端的升格路径只认
        ``try_result.ok``，这里不给它——「快验通过」不等于「可用」（D1），验收仍走
        重调本步。
        """
        row["rule"] = rule
        row.pop("ref_only", None)
        row.pop("need_ai", None)
        row["ai"] = {"rule": rule, "why": why}
        row["try_result"] = checked
        if (checked or {}).get("unfaithful"):
            row["pending_engine"] = True

    # ---- 提议段（只对免费段没把握的行花钱；按 kind 分组，一族一次调用）----
    groups: Dict[str, List[Dict[str, Any]]] = {}
    # 材料本身就不够（`core.material.blocked_next` 的码）：这时候让模型猜，只会花掉一次调用
    # 换来一串取不到的选择器。**先给一步能做的事**，提议留给下一次（材料够了之后）。
    # 目的按**取内容**算：补全要写的是这一行规则，不是去读结构——`webjs_output` 在
    # 「取内容」下够用（内容已经在脚本产出里），只有「看结构」才要先清脚本。
    # 判据在常量表里，这里只取码（AGENTS #24④：文案不进后端）。
    from core.material import INTENT_CONTENT, blocked_next

    next_step = blocked_next(material_kind, INTENT_CONTENT)
    for row in rows:
        if row.get("rule"):
            continue  # 免费段拍板的行：跳过提议，下面自查
        if next_step:
            row["needs_material"] = next_step
            row["fail_code"] = "need_material"
            row["reason"] = "材料还不够，先做下一步（%s）" % next_step
            _emit("ai_blocked", field=row["field"], reason_code=next_step)
            continue
        if budget["model_stop"]:
            continue  # 整段 AI 已停：原因在 budget.model_stop
        kind = field_kind(step, row["field"])
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
        proposals, stop = await _propose_group(
            _proposal_env(html, step, runtime, source, material_kind, page_stats, rows),
            structured, kind, group, on_llm=on_llm)
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
                checked = await _try(rule, field)
                _emit("ai_try", field=field, rule=rule, ok=bool(checked and checked.get("ok")),
                      count=int((checked or {}).get("count") or 0),
                      error=str((checked or {}).get("error") or "")[:200])
                if _usable(checked, field):
                    # 落的是**实际验过的那条**（`_try` 可能剥掉了重复的容器前缀）
                    _accept(row, str(checked.get("rule") or rule), why, checked)
                    accepted = True
                    break
            if accepted:
                _emit("ai_accepted", field=field, rule=str(row.get("rule") or ""),
                      count=int((row.get("try_result") or {}).get("count") or 0),
                      pending_engine=bool(row.get("pending_engine")),
                      reason_code=str((row.get("try_result") or {}).get("reason_code") or ""))
                continue
            # 模型的都不中：换候选头名（免费，不另花模型调用）
            tried = {str(c.get("rule") or "") for c in cands}
            alt = next((c for c in _candidates_for_ai(html, kind)
                        if str(c.get("rule") or "") and str(c["rule"]) not in tried), None)
            if alt:
                checked2 = await _try(str(alt["rule"]), field)
                _emit("ai_try", field=field, rule=str(alt["rule"]),
                      ok=bool(checked2 and checked2.get("ok")),
                      count=int((checked2 or {}).get("count") or 0),
                      error=str((checked2 or {}).get("error") or "")[:200])
                if _usable(checked2, field):
                    _accept(row, str(checked2.get("rule") or alt["rule"]),
                            "模型提议未选中，改用候选头名（轻量验证取到值）", checked2)
                    _emit("ai_accepted", field=field, rule=str(row.get("rule") or ""),
                          count=int((row.get("try_result") or {}).get("count") or 0),
                          pending_engine=bool(row.get("pending_engine")),
                          reason_code=str((row.get("try_result") or {}).get("reason_code") or ""))
                    continue
            # 都取不到：**不出规则文本**（宁空着），只留原因——落在输入框旁边的
            # 已知不可用规则正是「不如不填」的那个噪音。页面脚本那一路现在**真验**
            # （`fast_verify` 把 `page_vars` 合成 `params` 注入规则环境），所以「验不了」
            # 不再走这里：它由 `refusal_code` 给出码，候选照落、标 `pending_engine`。
            # 原因是**码**不是句子：消费侧要按它决定「让你改材料 / 改规则 / 就是模型没答上」
            # （同一套规矩见 `core.material` 的下一步码；文案在消费侧取，AGENTS #24④）。
            row.pop("ai", None)
            row["ai_failed"] = True
            row["fail_code"] = "no_candidates" if cands else "model_empty"
            row["reason"] = ("模型提议了 %d 条，轻量验证都没选中" % len(cands)
                             if cands else "模型没有给出该字段的候选")
            row["try_result"] = None
            _emit("ai_failed", field=field, reason=str(row["reason"])[:200],
                  fail_code=row["fail_code"])

    # ---- 免费段有把握的行：程序分数 + 引擎真跑双重确认（成本低、价值高）----
    for row in rows:
        if row.get("rule") and "try_result" not in row:
            row["try_result"] = await _try(str(row["rule"]), str(row["field"]))

    out["budget"] = budget
    # 段结论：整段一起补时，调用方要知道「这一段还差什么」——逐行状态散在
    # `fields[]` 里，而界面/任务讲的是段。三种去向互斥，顺序即优先级。
    out["segment"] = {
        "field_count": len(rows),
        "ready": [r["field"] for r in rows if r.get("rule")],
        "need_engine": [r["field"] for r in rows if r.get("pending_engine")],
        "need_material": [r["field"] for r in rows if r.get("needs_material")],
        "failed": [r["field"] for r in rows if r.get("ai_failed")],
    }
    return out


def run_loop_sync(html: str, structured: Dict[str, Any], source: Dict[str, Any],
                  base_url: str, step: str = "bookUrl",
                  fields: Optional[List[str]] = None,
                  runtime: Optional[Dict[str, Any]] = None,
                  material_kind: str = "",
                  page_stats: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """:func:`run_loop` 的同步版：端点（``/agent-loop`` 走线程池，避免
    rule_try 的同步调用卡住事件循环）与测试共用这一份判据。"""
    return asyncio.run(run_loop(html, structured, source, base_url, step, fields,
                                runtime=runtime, material_kind=material_kind,
                                page_stats=page_stats))


def _accepted_container(step: str, rows: List[Dict[str, Any]]) -> Tuple[str, str]:
    """本轮**已接受**的列表容器：`(源里的路径, 规则值)`；还没定就是两个空串。

    两处都要它：提示词要把容器当事实交给模型（`proposal_ctx.known_container`），
    快验要把容器并进源（`container_rule` 靠它做两段式）。
    """
    from core.autofill import _CONTAINER_PATHS, container_field

    name = container_field(step)
    group, rule_name = _CONTAINER_PATHS.get(name, ("", ""))
    if not (group and rule_name):
        return "", ""
    row = next((r for r in rows if r.get("field") == name and r.get("rule")), None)
    return ("%s.%s" % (group, rule_name), str(row["rule"])) if row else ("", "")


def _container_source(step: str, rows: List[Dict[str, Any]],
                      source: Dict[str, Any]) -> Dict[str, Any]:
    """把本轮已接受的容器并进源——`fast_verify` 靠它做两段式。

    补全整段时容器是这一轮才生成的，`source`（调用方给的）里还是空的。不并进去，
    验条目内规则时 `container_rule` 读到空 → 走单条路径 → 验的是「整页第一个匹配」，
    与引擎的「逐条目取」不是一回事（实测 2026-10-26：目录页 `chapterUrl` 就这样在
    整页上「通过」，落源后 233 条全部回落成目录页地址）。
    """
    path, value = _accepted_container(step, rows)
    if not path:
        return source
    group, rule_name = path.split(".", 1)
    return {**source, group: {**(source.get(group) or {}), rule_name: value}}


def _proposal_env(html: str, step: str, runtime: Optional[Dict[str, Any]],
                  source: Optional[Dict[str, Any]], material_kind: str,
                  page_stats: Optional[Dict[str, Any]],
                  rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """一次提议的**基础上下文**：与具体是哪一族字段无关的那些键。

    原先这些值作为 7 个参数交给 `_propose_group`、它再逐个挑进 ctx，同一次运行里
    每个字段族都重复挑一遍。收在这里之后 `_propose_group` 只补「本族是哪几个字段」。
    """
    env: Dict[str, Any] = {"html": html, "step": step}
    # 页面里的候选数据源（渲染后枚举的全局对象/数组，只有形状）。正文的地址常常不在
    # DOM 里而在某个全局变量上，而**变量名不能猜**——所以把清单原样交给模型判。
    globals_ = (runtime or {}).get("globals")
    if isinstance(globals_, list) and globals_:
        # **降权不删**：按「像不像数据」排序取前 N，并把「还有几条没列」带出去——只列
        # 前 N 条不说剩下的，模型会把「没列出来」读成「不存在」（AGENTS #12）。
        from core.app_debug import rank_globals

        kept, dropped = rank_globals(globals_, RUNTIME_PROMPT_ITEMS)
        if kept:
            env["runtimes"] = kept
            total_dropped = dropped + int(((runtime or {}).get("globals_dropped") or 0))
            if total_dropped > 0:
                env["runtime_dropped"] = total_dropped
    # 页面变量各键装的是什么（类型 / 条数 / 首项样例）：光有键名时所有键看起来一样，
    # 模型只能盲选；而选错了事后也验不出来（都能「跑出内容」）。
    page_vars = (runtime or {}).get("page_vars")
    if isinstance(page_vars, dict) and page_vars:
        env["page_vars"] = page_vars
    # 源里已有的 webJs：比枚举全局更准的「数据在哪」线索（变量名是源作者选的）。
    webjs = str(((source or {}).get("ruleContent") or {}).get("webJs") or "").strip()
    if webjs:
        env["known_webjs"] = webjs
    # 本段已定的容器：模型看不到源里刚生成的那一格，不给它就只能从大纲猜
    # 「条目内规则从哪起」，猜偏时带上容器祖先（引擎取空）。
    path, value = _accepted_container(step, rows)
    if path:
        env["known_container"] = {path: value}
    # 材料种类与页面统计：**判据在 `core.repair.suggest.outline_plan`**，这里只转交事实。
    # 少了这两项，材料不是 DOM 时也会硬压大纲（喂一堆 URL 给模型当结构）。
    if material_kind:
        env["material_kind"] = material_kind
    if isinstance(page_stats, dict):
        env["page_stats"] = page_stats
    return env


async def _propose_group(env: Dict[str, Any], structured: Dict[str, Any],
                         kind: str, group: List[Dict[str, Any]],
                         on_llm: Optional[Callable[[Dict[str, Any]], None]] = None,
                         ) -> Tuple[Optional[Dict[str, List[Dict[str, str]]]], str]:
    """一族字段（同取样类型）的模型提议。返回 ``({field: [candidates]}, "")`` 或
    ``(None, 终止原因)``。

    ``env`` 是 :func:`_proposal_env` 组好的基础上下文（与哪一族无关的部分）；
    这里只补「本族是哪几个字段」。

    ``FIELD_LABELS`` 在这里是**给人看的标签**（提示词里的「要的是哪个字段」），
    权威词表在前端 RULE_FIELD_DEFS / Kotlin BOOKINFO_FIELD_LABELS；取样类型
    取自 FILL_SPECS（:func:`core.autofill.field_kind`），不另存一份。

    ``on_llm`` 收这一次调用的**输入输出原文**（提示词 / 模型原样输出 / 解析结论 /
    usage）——调用方负责落账本；抛异常不拦这次运行。

    失败原因**原样带出**（suggest 的 ``error`` 本来就是写给用户看的：没配模型 /
    调用失败 / 输出不是 JSON）——上层不再把它压成「次数已用完」（AGENTS #4）。
    """
    from core.repair.suggest import suggest_many

    step = str(env.get("step") or "")
    proposal_ctx: Dict[str, Any] = {
        **env,
        "step_label": "、".join(FIELD_LABELS.get(r["field"], r["field"]) for r in group),
        "fields": [
            {"field": r["field"],
             "want_label": FIELD_LABELS.get(r["field"], r["field"]),
             # 取样类型也要进提示词：`script` 那一族要写成页面脚本（判据在 FILL_SPECS），
             # 只说字段名的话模型会给出取值规则
             "kind": field_kind(step, r["field"]),
             "app_values": _engine_values(structured, r["field"], step=step)}
            for r in group
        ],
    }
    try:
        proposals = await suggest_many(proposal_ctx)
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
