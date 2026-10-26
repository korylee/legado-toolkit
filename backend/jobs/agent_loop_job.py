# -*- coding: utf-8 -*-
"""AI 补全也是一次任务（``kind="agent_loop"``）。

**为什么让它进 jobs 表**：一轮补全 = 若干次模型调用 + 若干次 rule_try，几十秒到
几分钟。同步请求的等待期界面上没有任何产物——「进度」只能在响应回来之后才更新，
那是假的；刷新一次进度就没了（进程外没有任何痕迹）。进表之后：

  - **逐字段事件**写进事件账本（``events.jsonl``），前端吃现成的 SSE
    （``/api/jobs/{id}/stream``）——正在提议哪个字段、哪条规则验过、取到几条，
    全是真事实；
  - **模型调用的输入输出**落 ``llm.jsonl``（提示词原文 / 模型原样输出 / 解析结果 /
    token 用量），行号即游标，与事件账本同一套读法（``/api/jobs/{id}/llm-calls``）；
  - 取消、重挂、TTL 清场、orphan 收尾全部沿用既有那套。

**不占 JVM lane**（``lane_kind`` 只影响排队档位）：rule_try 走常驻 daemon 那条通道
（`core.rule_try`），占 lane 会把「改一个字段快验一下」排在几分钟的调试后面。

失败原因纪律（AGENTS #4）：模型没配 / 调用失败 / 输出不是 JSON 的原因**原样**进
事件与结果体，不冒充「次数已用完」。
"""

from __future__ import annotations

import json
import pathlib
import time
from typing import Any, Dict, List, Optional

from backend.jobs import run_events, runner
from core.paths import data_dir

_RUNS_DIR = "runs"


def run_dir_of(job_id: str) -> pathlib.Path:
    """这个任务的运行目录（**约定**：``runs/agent-<job_id>``，同调试那套按任务号现算）。"""
    return pathlib.Path(data_dir()) / "app_probe" / _RUNS_DIR / ("agent-" + str(job_id))


@runner.register("agent_loop")
async def run_agent_loop_job(job_id: str, st: Any, payload: Dict[str, Any],
                             ) -> Dict[str, Any]:
    """跑一轮 AI 补全：材料从 debug 任务行取，逐字段写事件，调用原文写账本。"""
    from backend.jobs.debug_materials import debug_step_material, job_source
    from core.agent_loop import run_loop

    run_dir = run_dir_of(job_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    debug_job_id = str(payload.get("debug_job_id") or "")
    step = str(payload.get("step") or "bookUrl")
    wants: Optional[List[str]] = list(payload.get("fields") or []) or None

    debug_job = st.get_job(debug_job_id)
    # 材料取法与自报原因都在共用件里（没有任务 / 没有这一步 / 没有页面各说各的）
    html, base_url, result, material_kind, structured, runtime = (
        debug_step_material(debug_job, debug_job_id, step))
    source = job_source(debug_job)
    # 这一页的**层与统计**：判「要不要给 DOM 大纲」要用它（图片多而带地址的少 = 地址不在
    # DOM 属性里）。判据在 `core.repair.suggest.outline_plan`，这里只把页面找出来。
    step_row = next((s for s in ((result or {}).get("steps") or [])
                     if isinstance(s, dict) and s.get("name") == step), {})
    page = next((p for p in ((result or {}).get("pages") or [])
                 if isinstance(p, dict) and p.get("id") == step_row.get("page_id")), {})
    page_stats = (page.get("page_layer") or {}).get("stats")
    page_stats = page_stats if isinstance(page_stats, dict) else None
    if not wants:
        # 整段补全：**按材料事实裁字段**，不把用不上的字段也列给模型。
        # 正文段最典型：DOM 里已有图时 `content` 一条就够，把 `webJs` 也列上
        # 会让模型多写一条用不上的脚本（判据在 `core.autofill.propose_fields`）。
        from core.autofill import propose_fields

        wants = propose_fields(step, material_kind, page_stats) or None

    run_events.append_event(run_dir, "ai_started", step=step,
                            debug_job_id=debug_job_id,
                            fields=list(wants or []),
                            # 这一轮是在哪种材料上提议的（rendered_dom / webjs_output /
                            # static_html）：排障时要能回答「它看的是 DOM 还是一串 URL」
                            material_kind=material_kind)
    runner.update_phase(job_id, "starting")

    def on_event(event: str, /, **payload: Any) -> None:
        """逐字段的过程事实：写不进去不拦这次运行（同事件账本的纪律）。

        事件名位置专用：载荷里就有 ``kind`` / ``fields`` 这类键，形参可被关键字
        命中时撞名会静默丢行（``run_events.append_event`` 的注释里记着这次踩坑）。
        """
        run_events.append_event(run_dir, event, **payload)

    def on_llm(record: Dict[str, Any]) -> None:
        """一次模型调用的账：**输入输出原文**（排查「它为什么这么说」只能看这个）。"""
        run_events.append_llm_call(run_dir, {
            "job_id": job_id,
            "debug_job_id": debug_job_id,
            "step": step,
            "kind": str(record.get("kind") or ""),
            "fields": list(record.get("fields") or []),
            "llm": str(record.get("llm") or ""),
            "model": str(record.get("model") or ""),
            "usage": record.get("usage") or {},
            "prompt_chars": len(str(record.get("prompt") or "")),
            "prompt": str(record.get("prompt") or ""),
            "response": str(record.get("response") or ""),
            "error": str(record.get("error") or "")[:300],
        })

    started = time.monotonic()
    try:
        out = await run_loop(html, structured, source, base_url, step, wants,
                             on_event=on_event, on_llm=on_llm, runtime=runtime,
                             material_kind=material_kind, page_stats=page_stats)
    except Exception as e:
        # 失败原因要能走到界面（AGENTS #4）：事件 + 任务行都留一份
        reason = "AI 补全没跑成：%s: %s" % (type(e).__name__, str(e)[:200])
        run_events.append_event(run_dir, "ai_failed", reason=reason)
        raise ValueError(reason) from e
    budget = out.get("budget") or {}
    written = sum(1 for r in (out.get("fields") or []) if r.get("rule"))
    cost = round(time.monotonic() - started, 1)
    run_events.append_event(run_dir, "ai_done", written=written, cost_sec=cost,
                            model_calls=int(budget.get("model_calls") or 0),
                            rule_tries=int(budget.get("rule_tries") or 0),
                            stop=str(budget.get("model_stop") or "")[:200])
    return out


def llm_calls(run_dir: pathlib.Path, after: int = 0,
              limit: int = 100) -> Dict[str, Any]:
    """读 ``llm.jsonl`` 的**摘要**列表（不含提示词与输出原文）。

    列表里放全文等于把几百 KB 的提示词塞进每次轮询的响应；原文按 ``seq`` 单取
    （见 :func:`llm_call`）。行号即游标，坏行也占号。
    """
    path = pathlib.Path(run_dir) / run_events.LLM_NAME
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {"items": [], "truncated": False, "cursor": 0}
    items: List[Dict[str, Any]] = []
    for index, raw in enumerate(lines):
        seq = index + 1
        if seq <= int(after or 0):
            continue
        try:
            rec = json.loads(raw)
        except ValueError:
            rec = None
        if not isinstance(rec, dict):
            items.append({"seq": seq, "error": "这一行读不出来"})
            continue
        items.append({
            "seq": seq,
            "ts": rec.get("ts"),
            "step": rec.get("step"),
            "kind": rec.get("kind"),
            "fields": rec.get("fields") or [],
            "llm": rec.get("llm"),
            "model": rec.get("model"),
            "usage": rec.get("usage") or {},
            "prompt_chars": rec.get("prompt_chars") or 0,
            "response_head": str(rec.get("response") or "")[:200],
            "error": rec.get("error") or "",
        })
    truncated = len(items) > limit
    return {"items": items[:limit], "truncated": truncated,
            "cursor": len(lines)}


def llm_call(run_dir: pathlib.Path, seq: int) -> Optional[Dict[str, Any]]:
    """按行号取**一条调用的全文**（system 不在记录里：它在代码里只有一份）。"""
    path = pathlib.Path(run_dir) / run_events.LLM_NAME
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    index = int(seq or 0) - 1
    if index < 0 or index >= len(lines):
        return None
    try:
        rec = json.loads(lines[index])
    except ValueError:
        return None
    return rec if isinstance(rec, dict) else None
