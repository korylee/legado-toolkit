# -*- coding: utf-8 -*-
"""运行观测的**唯一一条读法**（jvm-batch-timeline）：状态 + 事件增量 + 失败增量。

帧由 :func:`run_frame` 生产，两种传输渲染同一份：轮询
``GET /api/jobs/{id}/timeline``（前端兜底）与 SSE ``GET /api/jobs/{id}/stream``
（前端主路）。**逻辑只在这里一份**——两处各写一遍时，"推的"和"拉的"必然漂成
两种状态，而读者无从判断该信哪一份。

事件与失败都来自**运行目录里的两本账**（生产者见 ``backend/jobs/run_events``）：
行号即游标，成功清场也保留它们，所以运行态与终态是**同一条读法**——不再有
「跑的时候读目录、跑完读 result_json 尾巴（上限 500 条）」那两套。

旧任务的事件只存在 ``result_json`` 里（清场就删目录那版实现的遗留）：这里留一条
**读旧数据**的兜底，别在它上面加新逻辑，新的运行一律走文件。
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import time
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from backend.api.jobs import _job_row
from backend.deps import get_store
from backend.jobs import run_events, runner
from core.store import Store

router = APIRouter()

#: 单次响应的事件条数上限：增量轮询按游标续传不会丢行，首次打开大文件时分页补齐。
_EVENT_PAGE_CAP = 500
#: 失败源明细条数上限；超出只给总数（「还有 N 条未列出」），不没收计数。
_FAILURES_CAP = 500
#: 单条失败原因的展示上限（原文在失败清单里仍有全量）。
_REASON_CAP = 300
#: 流的检查间隔：进度是秒级，0.5 秒够快，也不给库添没必要的读。
_STREAM_POLL_SEC = 0.5
#: 心跳间隔：没有变化也要发一帧。缺了它，「卡住」与「服务没了」在界面上长得
#: 一样——而"卡住"正是用户盯着看的时刻。
_STREAM_BEAT_SEC = 5.0

_TERMINAL = ("done", "failed", "cancelled")


# ------------------------------------------------------------------ 两本账的读法


def _lines_after(path: pathlib.Path, after: int,
                 cap: int) -> Tuple[List[Tuple[int, str]], int]:
    """按**行号游标**读增量：返回 ``[(行号, 原文)]`` 与最后扫到的行号。

    三条规矩都是踩出来的：

    - **坏行也占行号**（跳过不解析）：否则游标会在坏行处打转，客户端反复重取同一段；
    - 文件末尾的**半行留到下一拍**（边写边读必然遇到），能解析才收；
    - 超过 ``cap`` 只扫一页，游标停在**已扫描的最后一行的上一行**，下一拍继续。
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return [], max(int(after or 0), 0)
    lines = raw.splitlines(keepends=True)
    out: List[Tuple[int, str]] = []
    cursor = max(int(after or 0), 0)
    for idx, line in enumerate(lines, start=1):
        if idx <= cursor:
            continue
        text = line.strip()
        if idx == len(lines) and line and not line.endswith(("\n", "\r")):
            if text:
                try:
                    json.loads(text)
                except ValueError:
                    break                      # 半行：下一拍再看
        cursor = idx
        if not text:
            continue
        if len(out) >= cap:
            cursor = idx - 1
            break
        out.append((idx, text))
    return out, cursor


def _events_after(run_dir: pathlib.Path, after: int) -> Tuple[List[Dict[str, Any]], int]:
    """事件增量：行号即 seq（读到的第几行就是第几号）。"""
    got, cursor = _lines_after(run_dir / run_events.EVENTS_NAME, after, _EVENT_PAGE_CAP)
    events: List[Dict[str, Any]] = []
    for seq, text in got:
        try:
            ev = json.loads(text)
        except ValueError:
            continue
        if isinstance(ev, dict):
            ev["seq"] = seq
            events.append(ev)
    return events, cursor


def _failure_item(rec: Dict[str, Any], seq: int = 0) -> Dict[str, Any]:
    """失败清单行：``seq`` 是它在 ``failures.jsonl`` 里的**行号**——客户端按它去重。

    少了它，SSE 重连（服务端从原游标重发）会把同一条失败渲染两遍，而时间线上看起来
    就像真跑了两遍。
    """
    return {
        "seq": int(seq or 0),
        "url": str(rec.get("url") or ""),
        "name": str(rec.get("name") or ""),
        "state": str(rec.get("state") or ""),
        "reason": str(rec.get("reason") or "")[:_REASON_CAP],
        "stage": str(rec.get("stage") or ""),
        "chunk": str(rec.get("chunk") or ""),
    }


def _failures_after(run_dir: pathlib.Path, after: int) -> Tuple[List[Dict[str, str]], int, int]:
    """失败增量：``failures.jsonl`` 的行号就是它的游标。

    ``total`` 是**全量条数**（与 ``after`` 无关）：清单被 cap 截断时，界面靠它说
    「还有 N 条未列出」，不能因为增量就把总数说小。
    """
    cursor = max(int(after or 0), 0)
    try:
        lines = (run_dir / run_events.FAILURES_NAME).read_text(
            encoding="utf-8").splitlines()
    except OSError:
        return [], cursor, 0
    items: List[Dict[str, Any]] = []
    total = 0
    for idx, line in enumerate(lines, start=1):
        text = line.strip()
        rec: Any = None
        if text:
            try:
                rec = json.loads(text)
            except ValueError:
                rec = None
        if not isinstance(rec, dict):
            if idx > cursor:
                cursor = idx
            continue
        total += 1
        if idx <= cursor:
            continue
        cursor = idx
        if len(items) < _FAILURES_CAP:
            items.append(_failure_item(rec, idx))
    return items, cursor, total


# ------------------------------------------------- 旧数据：事件只存在 result_json


def _legacy_result(st: Store, job_id: str) -> Dict[str, Any]:
    """旧任务行里的完整结果（解析不出来给空对象）。只在"运行目录里没有账本"时读。"""
    job = st.get_job(job_id)
    try:
        parsed = json.loads((job or {}).get("result_json") or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _events_from_result(parsed: Dict[str, Any], after: int) -> Tuple[List[Dict[str, Any]], int]:
    events = parsed.get("events") if isinstance(parsed.get("events"), list) else []
    page: List[Dict[str, Any]] = []
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


def _failures_from_result(parsed: Dict[str, Any]) -> Dict[str, Any]:
    """终态旧行的失败清单：计数用 ``dist``（引擎原值 state 的精确分布），
    清单从 ``items`` 里筛——items 只带五档 health 键且本身有截断上限，
    所以 total 以 dist 为准，清单不足时如实报 truncated。"""
    dist = parsed.get("dist") if isinstance(parsed.get("dist"), dict) else {}
    total = sum(int(v or 0) for k, v in dist.items() if k != "ok")
    items: List[Dict[str, Any]] = []
    raw_items = parsed.get("items") if isinstance(parsed.get("items"), list) else []
    for idx, it in enumerate(raw_items, start=1):
        if not isinstance(it, dict) or it.get("health") == "ok":
            continue
        if len(items) >= _FAILURES_CAP:
            break
        items.append(_failure_item({"url": it.get("url"), "name": it.get("name"),
                                    "state": it.get("health"),
                                    "reason": it.get("error")}, idx))
    return {"total": total, "truncated": total > len(items), "cursor": 0, "items": items}


# ------------------------------------------------------------------ 帧的组装


def _run_dir_of(row: Dict[str, Any]) -> pathlib.Path:
    """运行目录。

    跑批的目录在**提交时就冻结进了 payload**（导出清单那一步决定的），所以从轻量行的
    ``result_run_dir`` 取（SQL 侧已抽好，见 ``core.store._JOB_LIGHT_COLUMNS``——不为一个
    路径去解析上百 KB 的 result_json）。

    调试的目录是**按任务号现算**的（提交时还不知道 job_id，见
    ``backend/jobs/jvm_debug_job.run_dir_of``）：约定 ``runs/debug-<job_id>``。
    取不到给一个不存在的路径，读文件自然返回空。
    """
    value = str(row.get("result_run_dir") or "")
    if value:
        return pathlib.Path(value)
    job_id = str(row.get("id") or "")
    if job_id and str(row.get("kind") or "") == "jvm_debug":
        from backend.jobs.jvm_debug_job import run_dir_of
        return run_dir_of(job_id)
    return pathlib.Path("<none>")


def _elapsed_ms(stamp: Any) -> int:
    """行里的**本地时间字符串** → 距今毫秒。解析不了给 0（宁可不显示，也不编一个数）。"""
    try:
        at = time.mktime(time.strptime(str(stamp or ""), "%Y-%m-%d %H:%M:%S"))
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, int((time.time() - at) * 1000))


def lane_state() -> Dict[str, Any]:
    """此刻占着 JVM lane 的是谁、后面排了几个。

    与旧的 `debug-status` 读的是**同一份**事实（`runner.lane_snapshot`）。同步端点
    跑在线程池里、没有事件循环，所以那一步失败时退到能跨线程读的那条路；两条都读不到
    就留空——「读不到」不等于「没人占用」，更不能把一次观测失败变成 500（AGENTS #12）。
    文案由前端按码取词。
    """
    try:
        snap = runner.lane_snapshot("jvm")
    except RuntimeError:
        snap = runner.lane_snapshot_threadsafe("jvm")
    return {"lane_holder": str(snap.get("held") or ""),
            "lane_waiting": len(snap.get("waiting") or [])}


def run_frame(st: Store, job_id: str, after: int = 0,
              after_failures: int = 0) -> Dict[str, Any]:
    """一次读取：状态 + 事件增量 + 失败增量。**两种传输都调它**。

    帧的形状：轻量任务行（``_job_row``，含结论摘要）+ 以下几键——

    - ``events`` / ``cursor``：事件增量与它的行号游标；
    - ``failures`` / 内层 ``cursor``：失败增量、精确总数与它自己的游标；
    - ``done``：终态（客户端据此关流；非 JVM kind 也照实报，不再一律当作已完成）；
    - ``lane_holder`` / ``lane_waiting`` / ``elapsed_ms`` / ``phase_ms``：
      调试侧原本靠 ``/rules/debug-status`` 拿的两条本机事实，现在两边同源
      （``utils/debugRun.js`` 的中文取词因此批量与调试共用一份）。
    """
    row = st.get_job_summary(job_id)
    if not row:
        raise HTTPException(404, "任务不存在")
    after = max(int(after or 0), 0)
    after_failures = max(int(after_failures or 0), 0)
    status = str(row.get("status") or "")
    done = status in _TERMINAL
    run_dir = _run_dir_of(row)
    has_events = (run_dir / run_events.EVENTS_NAME).is_file()
    has_failures = (run_dir / run_events.FAILURES_NAME).is_file()

    legacy: Dict[str, Any] = {}
    if done and not (has_events and has_failures):
        # 旧任务：事件/失败只在 result_json 里。**只在缺账本时读**，别让新运行也付这份钱
        legacy = _legacy_result(st, job_id)

    if has_events:
        events, cursor = _events_after(run_dir, after)
    elif done:
        events, cursor = _events_from_result(legacy, after)
    else:
        events, cursor = [], after

    if has_failures:
        items, f_cursor, total = _failures_after(run_dir, after_failures)
        failures: Dict[str, Any] = {"total": total, "truncated": total > len(items),
                                    "cursor": f_cursor, "items": items}
    elif done:
        failures = _failures_from_result(legacy)
    else:
        failures = {"total": 0, "truncated": False, "cursor": after_failures, "items": []}

    frame = _job_row(row)
    frame.update(lane_state())
    frame.update({
        "cursor": cursor,
        "events": events,
        "failures": failures,
        "done": done,
        "elapsed_ms": _elapsed_ms(row.get("created_at")),
        "phase_ms": _elapsed_ms(row.get("updated_at")),
    })
    return frame


@router.get("/{job_id}/timeline")
def job_timeline(job_id: str, after: int = 0, after_failures: int = 0,
                 st: Store = Depends(get_store)):
    """帧的**拉**形态：``after`` / ``after_failures`` 之前的不重发（行号游标）。"""
    return run_frame(st, job_id, after, after_failures)


@router.get("/{job_id}/stream")
async def job_stream(job_id: str, after: int = 0, after_failures: int = 0):
    """帧的**推**形态（SSE）：与 ``/timeline`` 同一个生产者，只是我们替你定时。

    - 只在**有变化**（状态/进度/新事件/新失败）或心跳到点时发一帧；
    - 终态发完即关流；重连回来自带游标，而第一帧必然是**全量快照**
      （``--reload`` 会掐断所有流，这条是自愈的根据）；
    - Store **建一次**就用整条流：放进循环里的话，每 0.5 秒要重跑一遍
      ``Store.__init__``（建连接 + PRAGMA + 建库），为一个 SELECT 付一次建库的价。
    """

    def frame(data: Dict[str, Any]) -> str:
        return "data: " + json.dumps(data, ensure_ascii=False) + "\n\n"

    async def gen():
        last_key: Any = None
        beat_at = 0.0
        cursor, f_cursor = max(int(after or 0), 0), max(int(after_failures or 0), 0)
        with Store(readonly=True) as st:
            while True:
                try:
                    body = run_frame(st, job_id, cursor, f_cursor)
                except HTTPException:
                    yield frame({"error": "任务不存在"})
                    return
                now = time.monotonic()
                key = (body["status"], body["phase"], body["progress"],
                       body["total"], body["updated_at"])
                changed = (bool(body["events"]) or bool(body["failures"]["items"])
                           or key != last_key)
                if changed or now - beat_at >= _STREAM_BEAT_SEC:
                    yield frame(body)
                    last_key = key
                    beat_at = now
                cursor, f_cursor = body["cursor"], body["failures"]["cursor"]
                if body["done"]:
                    return
                await asyncio.sleep(_STREAM_POLL_SEC)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})
