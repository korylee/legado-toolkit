# -*- coding: utf-8 -*-
# 后台任务执行器。
#
# 设计：所有耗时操作都不在 HTTP 请求里同步跑，而是
#   POST /api/jobs -> 立刻返回 job_id
#   任务在 asyncio task 里跑，进度写进 SQLite 的 jobs 表
#   前端通过 SSE 订阅 /api/jobs/{id}/events
#
# 这样 check 3700 个源、AI 修复循环这类分钟级任务不会让前端超时。
import asyncio
import traceback
import uuid
from typing import Any, Awaitable, Callable, Dict, Optional

from core.store import Store, now

#: 正在运行的任务 {job_id: asyncio.Task}
TASKS: Dict[str, asyncio.Task] = {}

#: 任务类型 -> 执行函数。函数签名 (job_id, store, payload) -> result dict
HANDLERS: Dict[str, Callable[..., Awaitable[Dict[str, Any]]]] = {}


def register(kind: str):
    def deco(fn):
        HANDLERS[kind] = fn
        return fn
    return deco


def submit(kind: str, payload: Optional[Dict[str, Any]] = None) -> str:
    # 创建任务记录并交给事件循环执行。必须在运行中的 loop 里调用。
    if kind not in HANDLERS:
        raise ValueError("未知任务类型: %s（可用: %s）" % (kind, ", ".join(sorted(HANDLERS))))
    job_id = uuid.uuid4().hex[:12]
    payload = payload or {}
    st = Store()
    try:
        st.create_job(job_id, kind, total=int(payload.get("total", 0) or 0), payload=payload)
    finally:
        st.close()
    TASKS[job_id] = asyncio.create_task(_run(job_id, kind, payload))
    return job_id


async def _run(job_id: str, kind: str, payload: Dict[str, Any]) -> None:
    st = Store()
    try:
        st.update_job(job_id, status="running")
        result = await HANDLERS[kind](job_id, st, payload)
        st.update_job(job_id, status="done", result=result or {})
    except asyncio.CancelledError:
        st.update_job(job_id, status="cancelled")
        raise
    except Exception as exc:
        st.update_job(job_id, status="failed",
                      result={"error": "%s: %s" % (type(exc).__name__, exc),
                              "trace": traceback.format_exc()[-2000:]})
    finally:
        st.close()
        TASKS.pop(job_id, None)


def cancel(job_id: str) -> bool:
    t = TASKS.get(job_id)
    if not t:
        return False
    t.cancel()
    return True


def running() -> list:
    return sorted(TASKS.keys())


# ------------------------------------------------------------------ 内置任务

@register("ping")
async def _ping(job_id: str, st: Store, payload: Dict[str, Any]) -> Dict[str, Any]:
    # 冒烟用：把 payload["steps"] 拆成几步，每步更新一次进度
    from core.store import Store as _S
    steps = int(payload.get("steps", 3) or 3)
    for i in range(1, steps + 1):
        await asyncio.sleep(0.2)
        st.update_job(job_id, progress=i, total=steps)
    return {"steps": steps, "finished_at": now()}
