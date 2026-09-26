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
import weakref
from contextlib import asynccontextmanager
from typing import Any, Awaitable, Callable, Dict, Optional

from core.store import Store, now

#: 正在运行的任务 {job_id: asyncio.Task}
TASKS: Dict[str, asyncio.Task] = {}

# 任务过期清理的巡检间隔。TTL 本身由 core.store.JOBS_TTL_DAYS 定义，这里只负责
# 让清理不依赖“恰好又有人提交了新任务”。
JOB_SWEEP_INTERVAL_SECONDS = 3600
_JOB_SWEEPER: Optional[asyncio.Task] = None

#: 任务类型 -> 执行函数。函数签名 (job_id, store, payload) -> result dict
HANDLERS: Dict[str, Callable[..., Awaitable[Dict[str, Any]]]] = {}

#: 进程内的有序执行 lane。每个事件循环各有一份，避免测试用多个
#: ``asyncio.run`` 时复用已绑定到旧 loop 的 asyncio.Lock。
_LANES: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, Dict[str, asyncio.Lock]]" = weakref.WeakKeyDictionary()


def _lane_lock(name: str) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lanes = _LANES.setdefault(loop, {})
    return lanes.setdefault(name, asyncio.Lock())


@asynccontextmanager
async def acquire_lane(name: str):
    """按提交顺序串行执行一个共享资源 lane。"""
    lock = _lane_lock(name)
    await lock.acquire()
    try:
        yield
    finally:
        lock.release()


def register(kind: str):
    def deco(fn):
        HANDLERS[kind] = fn
        return fn
    return deco


def submit(kind: str, payload: Optional[Dict[str, Any]] = None,
           lane: Optional[str] = None, retry_of: str = "") -> str:
    # 创建任务记录并交给事件循环执行。必须在运行中的 loop 里调用。
    if kind not in HANDLERS:
        raise ValueError("未知任务类型: %s（可用: %s）" % (kind, ", ".join(sorted(HANDLERS))))
    job_id = uuid.uuid4().hex[:12]
    payload = payload or {}
    st = Store()
    try:
        st.create_job(job_id, kind, total=int(payload.get("total", 0) or 0),
                      payload=payload, retry_of=retry_of)
    finally:
        st.close()
    TASKS[job_id] = asyncio.create_task(_run(job_id, kind, payload, lane))
    return job_id


def update_phase(job_id: str, phase: str) -> None:
    """把阶段写入任务表。

    JVM 的真正执行段在工作线程里，不能复用 async handler 持有的 Store 连接；
    这里短开独立连接，保证阶段在阻塞执行期间也能被 SSE 读到。
    """
    st = Store()
    try:
        st.update_job(job_id, phase=phase)
    finally:
        st.close()


async def _run(job_id: str, kind: str, payload: Dict[str, Any],
               lane: Optional[str] = None) -> None:
    st = Store()
    lock = _lane_lock(lane) if lane else None
    acquired = False
    try:
        if kind == "jvm_run":
            update_phase(job_id, "waiting_readiness")
        if lock is not None:
            await lock.acquire()
            acquired = True
        if kind == "jvm_run":
            update_phase(job_id, "starting_worker" if payload.get("single")
                         else "starting_gradle")
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
        if acquired:
            lock.release()
        st.close()
        TASKS.pop(job_id, None)


def recover_orphans() -> int:
    """把上次进程留下的孤儿任务收尾（标成 failed），返回收了几条。

    **必须在服务进程启动时调一次**：任务活在进程内的 asyncio task 里（``TASKS``），
    进程一死它们就没了，而库里那行还写着 running/pending。调用点的选择见
    ``backend/app.py`` 的 lifespan——**别挪到模块级 import**（测试和 CLI 顺手 import
    一下就会写库），**也别挪进 ``__main__.py``**（--reload 下那里跑的是监督进程，
    热重载重启子进程时不会执行，恰好漏掉最常发生的那种情况）。

    **不静默**：收掉几条要说出来，否则「重启后任务列表里那条变红了」在日志里
    没有任何痕迹。判据与假定见 ``Store.fail_orphan_jobs``。
    """
    st = Store()
    try:
        n = st.fail_orphan_jobs()
    finally:
        st.close()
    if n:
        print("警告: 上次进程结束时 %d 个任务没写终态，已标为 failed" % n, flush=True)
    return n


def sweep_expired() -> int:
    """清理已过期的终态任务，供启动和后台巡检共用。"""
    st = Store()
    try:
        return st.sweep_jobs()
    finally:
        st.close()


async def start_job_sweeper() -> None:
    """启动任务 TTL 巡检；只应由服务 lifespan 调用。"""
    global _JOB_SWEEPER
    if _JOB_SWEEPER is not None and not _JOB_SWEEPER.done():
        return

    async def _loop() -> None:
        while True:
            await asyncio.sleep(JOB_SWEEP_INTERVAL_SECONDS)
            try:
                sweep_expired()
            except Exception as exc:
                # 清理失败不能影响任务执行；下一轮继续重试，并把原因留在日志里。
                print("警告：任务过期清理失败：%s" % exc, flush=True)

    _JOB_SWEEPER = asyncio.create_task(_loop())


async def stop_job_sweeper() -> None:
    """停止服务关闭时的任务 TTL 巡检。"""
    global _JOB_SWEEPER
    task = _JOB_SWEEPER
    _JOB_SWEEPER = None
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def cancel(job_id: str) -> bool:
    t = TASKS.get(job_id)
    if not t:
        return False
    st = Store()
    try:
        job = st.get_job(job_id)
        if not job or job.get("status") in ("done", "failed", "cancelled"):
            return False
        st.update_job(job_id, status="cancel_requested", phase="cancel_requested")
    finally:
        st.close()
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
