# -*- coding: utf-8 -*-
"""任务执行时间线（jvm-batch-timeline）：骨架事件 + 失败源的只读增量端点。

事件由执行线程追加到运行目录 ``events.jsonl``（生产者见
``backend/jobs/jvm_exec``；**行号即游标**，端点不给事件编 seq，读到第几行
就报第几行）。本端点只读：运行中从运行目录取事件增量与失败源（扫各块
``results.jsonl``），终态从 ``result_json`` 取——成功会清运行目录，时间线
在终态合并时就已并入结果。
"""

from __future__ import annotations

import json
import pathlib
from typing import Any, Dict, List, Tuple

from fastapi import APIRouter, Depends, HTTPException

from backend.deps import get_store
from core.store import Store

router = APIRouter()

#: 单次响应的事件条数上限：增量轮询按游标续传不会丢行，首次打开大文件时分页补齐。
_EVENT_PAGE_CAP = 500
#: 失败源明细条数上限；超出只给总数（「还有 N 条未列出」），不没收计数。
_FAILURES_CAP = 500
#: 单条失败原因的展示上限（原文在结果文件里仍有全量）。
_REASON_CAP = 300

_TERMINAL = ("done", "failed", "cancelled")


def _iter_json_lines(path: pathlib.Path) -> List[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    return [line for line in text.splitlines() if line.strip()]


def _events_after(path: pathlib.Path, after: int) -> Tuple[List[Dict[str, Any]], int]:
    """读 ``events.jsonl`` 第 after 行之后的事件。返回 (事件, 总行数)。

    行号即游标：**坏行也占一个行号**（跳过不解析），否则游标会在坏行处
    打转、客户端反复重取同一段。首次打开大文件时按 ``_EVENT_PAGE_CAP``
    分页，游标只前进已返回的行数，不丢中间行。
    """
    lines = _iter_json_lines(path)
    events: List[Dict[str, Any]] = []
    for idx, line in enumerate(lines, start=1):
        if idx <= after:
            continue
        if len(events) >= _EVENT_PAGE_CAP:
            break
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict):
            ev["seq"] = idx
            events.append(ev)
    return events, len(lines)


def _failure_item(row: Dict[str, Any], state: str, reason: str,
                  stage: str, chunk: str) -> Dict[str, str]:
    return {
        "url": str(row.get("url") or ""),
        "name": str(row.get("name") or ""),
        "state": state,
        "reason": reason[:_REASON_CAP],
        "stage": stage,
        "chunk": chunk,
    }


def _failures_from_files(run_dir: pathlib.Path) -> Dict[str, Any]:
    """运行中的失败源：扫各块 results.jsonl（单条的 results.jsonl 也在其中）。

    「通过」= ``state == "ok"``（唯一映射到 Health.OK，见 core/jvm_health）；
    其余原值（no_result / timeout / error / login_wall / empty_js_shell /
    invalid）全算非通过。文件可能正被引擎追加，逐行容错：残缺行跳过，
    读到哪算哪——进度轮询对同一批文件就是这么读的。
    """
    items: List[Dict[str, str]] = []
    total = 0
    paths = sorted(run_dir.glob("chunk-*/results.jsonl"))
    single = run_dir / "results.jsonl"
    if single.is_file():
        paths.append(single)
    for path in paths:
        chunk = path.parent.name if path.parent.name.startswith("chunk-") else ""
        for line in _iter_json_lines(path):
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict) or row.get("state") == "ok":
                continue
            total += 1
            if len(items) >= _FAILURES_CAP:
                continue
            items.append(_failure_item(row, str(row.get("state") or ""),
                                       str(row.get("reason") or ""),
                                       str(row.get("stage") or ""), chunk))
    return {"total": total, "truncated": total > len(items), "items": items}


def _failures_from_result(parsed: Dict[str, Any]) -> Dict[str, Any]:
    """终态的失败源：计数用 ``dist``（引擎原值 state 的精确分布），
    清单从 ``items`` 里筛——items 只带五档 health 键且本身有截断上限，
    所以 total 以 dist 为准，清单不足时如实报 truncated。"""
    dist = parsed.get("dist") if isinstance(parsed.get("dist"), dict) else {}
    total = sum(int(v or 0) for k, v in dist.items() if k != "ok")
    items: List[Dict[str, str]] = []
    raw_items = parsed.get("items") if isinstance(parsed.get("items"), list) else []
    for it in raw_items:
        if not isinstance(it, dict) or it.get("health") == "ok":
            continue
        if len(items) >= _FAILURES_CAP:
            break
        items.append(_failure_item(it, str(it.get("health") or ""),
                                   str(it.get("error") or ""), "", ""))
    return {"total": total, "truncated": total > len(items), "items": items}


def _events_from_result(parsed: Dict[str, Any], after: int) -> Tuple[
        List[Dict[str, Any]], int]:
    events = parsed.get("events") if isinstance(parsed.get("events"), list) else []
    page = []
    for idx, ev in enumerate(events, start=1):
        if idx <= after:
            continue
        if len(page) >= _EVENT_PAGE_CAP:
            break
        if isinstance(ev, dict):
            ev = dict(ev)
            ev["seq"] = idx
            page.append(ev)
    return page, len(events)


def _run_dir_of(job: Dict[str, Any]) -> pathlib.Path:
    """从任务的 payload（提交时冻结的 manifest）取运行目录；取不到给不存在
    的路径，读文件自然返回空——legacy 无 manifest 的任务本来就没有这些文件。"""
    try:
        payload = json.loads(job.get("payload") or "{}")
        run_dir = str((payload.get("manifest") or {}).get("run_dir") or "")
    except (TypeError, ValueError):
        return pathlib.Path("<none>")
    return pathlib.Path(run_dir) if run_dir else pathlib.Path("<none>")


@router.get("/{job_id}/timeline")
def job_timeline(job_id: str, after: int = 0, st: Store = Depends(get_store)):
    """时间线增量：``after`` 之前的事件不重发（行号游标）。

    终态任务的事件在执行结束时就并进了 ``result_json``（上限 500 条），
    这里不再碰磁盘；运行中才读运行目录。``/events`` 已被任务 SSE 占用，
    时间线用 ``/timeline``。
    """
    job = st.get_job(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    kind = str(job.get("kind") or "")
    status = str(job.get("status") or "")
    done = status in _TERMINAL
    out: Dict[str, Any] = {
        "job_id": job_id, "kind": kind, "status": status, "done": done,
        "cursor": max(int(after or 0), 0),
        "events": [],
        "failures": {"total": 0, "truncated": False, "items": []},
    }
    after = max(int(after or 0), 0)
    if kind != "jvm_run":
        # 只有 JVM 校验有执行时间线；其余 kind 前端按 kind 就不渲染面板
        out["done"] = True
        return out
    if done:
        try:
            parsed = json.loads(job.get("result_json") or "{}")
        except (TypeError, ValueError):
            parsed = {}
        if not isinstance(parsed, dict):
            parsed = {}
        events, cursor = _events_from_result(parsed, after)
        out["events"] = events
        out["cursor"] = cursor
        out["failures"] = _failures_from_result(parsed)
        return out
    run_dir = _run_dir_of(job)
    events, cursor = _events_after(run_dir / "events.jsonl", after)
    out["events"] = events
    out["cursor"] = cursor
    if run_dir.is_dir():
        out["failures"] = _failures_from_files(run_dir)
    return out
