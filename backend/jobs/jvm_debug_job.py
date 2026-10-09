# -*- coding: utf-8 -*-
"""调试也是任务（``kind="jvm_debug"``）：把一次本机引擎调试交给 jobs 那条链。

**为什么让调试变成任务**：调试的执行体（一次几分钟的 JVM 跑测）本来就和跑批同形——
同一个 lane、同一种"边跑边 flush 的事件文件"（引擎逐条写 ``debug.ndjson``）、同样需要
"跑到哪一步了 / 谁占着引擎"。之前它是一条同步长轮询：不跑完不返回，于是等待期界面上
一个可读产物都没有，只剩一个前端本地秒表（刷新还归零），``--reload`` 一来状态直接消失
（``_ACTIVE_RUNS`` 是进程内的）。进了 jobs 表之后：运行目录/事件账本/取消/重试/孤儿
收尾/TTL 全部沿用既有那套，观测口也只剩一条（``backend/api/job_timeline``）。

运行目录按**任务号**命名（``runs/debug-<job_id>``）：提交时还不知道 job_id，而
"目录属于哪个任务"既是过期清理的依据、也是观测端的读法（见 ``_run_dir_of``）。
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import threading
import time
from typing import Any, Dict, Optional

from fastapi.concurrency import run_in_threadpool

from backend.jobs import run_events, runner
from core.paths import data_dir

#: 事件 tail 的采样间隔：引擎逐条 flush，0.3 秒足够快，又不至于把盘读穿。
_TAIL_INTERVAL_SEC = 0.3

_RUNS_DIR = "runs"


def run_dir_of(job_id: str) -> pathlib.Path:
    """这个任务的运行目录（**约定**：``runs/debug-<job_id>``）。

    与跑批不同：跑批的运行目录在提交时就冻结进了 payload（导出清单那一步决定的），
    调试是按任务号现算——两处都只回答"这次运行的材料在哪"，没有第三份。
    """
    return pathlib.Path(data_dir()) / "app_probe" / _RUNS_DIR / ("debug-" + str(job_id))


def _event_line(raw: str) -> Optional[Dict[str, Any]]:
    """引擎事件行 → 时间线事件字段。

    形状与返回体里的 ``events``（``[{t, text}]``）同一个来源：``t`` 是引擎给的
    相对秒，``text`` 是原文。解析不了给 ``None``（坏行照样占行号，消费端跳过）。
    """
    try:
        event = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(event, dict):
        return None
    return {"t": round((event.get("elapsed_ms") or 0) / 1000.0, 3),
            "text": str(event.get("text") or "")}


class _EventTail:
    """把引擎正在写的 ``debug.ndjson`` 逐行搬进事件账本。

    引擎那边是**逐条 flush** 的（``DebugService.kt``：崩了/超时也要留下已收到的部分），
    所以这里读文件就能拿到"正在跑哪一步"——这正是同步长轮询时代缺的那个产物。
    行号由账本自己发（``seq``），这里只管把新行按顺序追加。
    """

    def __init__(self, ndjson: pathlib.Path, run_dir: pathlib.Path) -> None:
        self.ndjson = ndjson
        self.run_dir = run_dir
        self.stop = threading.Event()
        self._seen = 0
        self._thread = threading.Thread(target=self._loop, name="debug-event-tail",
                                        daemon=True)

    def start(self) -> None:
        self._thread.start()

    def join(self, timeout: float = 2.0) -> None:
        self.stop.set()
        self._thread.join(timeout)

    def _loop(self) -> None:
        while not self.stop.wait(_TAIL_INTERVAL_SEC):
            self.drain()

    def drain(self) -> None:
        try:
            lines = self.ndjson.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        for raw in lines[self._seen:]:
            self._seen += 1
            if not raw.strip():
                continue
            fields = _event_line(raw)
            if fields is None:
                continue
            run_events.append_event(self.run_dir, "debug", **fields)


@runner.register("jvm_debug")
async def run_jvm_debug_job(job_id: str, st: Any, payload: Dict[str, Any]) -> Dict[str, Any]:
    """跑一次调试。lane 由 ``runner._run`` 按 ``debug`` 档持有（调试优先于批量）。"""
    from core import jvm_debug

    run_dir = run_dir_of(job_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    # 拿到 lane 之后的第一件事：相位从 queued（等引擎）推进到 starting（已交给引擎线程）——
    # 运行条据此换一次词，与旧 `_ACTIVE_RUNS` 登记的两个相位同义
    runner.update_phase(job_id, "starting")
    run_events.append_event(run_dir, "debug_started",
                            key=str(payload.get("key") or ""),
                            source_url=str((payload.get("source") or {}).get("bookSourceUrl") or ""))
    tail = _EventTail(run_dir / "debug.ndjson", run_dir)
    tail.start()
    started = time.monotonic()
    try:
        # 执行体是阻塞的（Gradle/常驻 daemon），交给线程；`shield` 沿用既有规矩：
        # 客户端断开不许把已经在引擎里的那一次掐掉（调试没有可恢复的中间态）
        result = await asyncio.shield(run_in_threadpool(
            jvm_debug.run_jvm_debug,
            payload.get("source") or {},
            payload.get("key") or "",
            int(payload.get("timeout") or 0),
            str(payload.get("cookie") or ""),
            str(payload.get("cache") or "auto"),
            str(payload.get("proxy") or ""),
            run_dir=run_dir,
            keep_run_dir=True,
            job_id=job_id,
            readiness_result=payload.get("readiness"),
        ))
    except asyncio.CancelledError:
        # 取消也要留一行：账本是过程的事实源，缺了它时间线上看不出这次是被取消的
        # （批次那条链同样写 `cancelled`）
        tail.join()
        tail.drain()
        run_events.append_event(run_dir, "cancelled")
        raise
    finally:
        tail.join()
        tail.drain()
    cost = round(time.monotonic() - started, 1)
    ok = not str(result.get("error") or "")
    # 终态事件只落盘（读它的地方是 job_timeline）：返回体是结果，账本是过程
    run_events.append_event(run_dir, "done" if ok else "failed",
                            code=result.get("code"),
                            cost_sec=cost,
                            reason=str(result.get("error") or "")[:300])
    return result


def debug_payload(body_source: Dict[str, Any], *, key: str, timeout: int, cookie: str = "",
                  cache: str = "auto", proxy: str = "",
                  readiness: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """提交时的 payload（**唯一一份**）：URL 校验与 readiness 都在路由里做过了。"""
    return {"source": body_source, "key": key, "timeout": int(timeout),
            "cookie": cookie, "cache": cache, "proxy": proxy,
            "readiness": readiness or {}}
