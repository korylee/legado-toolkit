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
_LANES: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, Dict[str, _Lane]]" = weakref.WeakKeyDictionary()

#: lane 等待者的基础优先级：数值小者先获得许可。调试是交互请求（0）；
#: 批量校验与生成后验证同档（10）——它们都由用户显式提交、非交互敏感。
LANE_PRIORITY = {"debug": 0, "batch": 10, "unknown": 10}

#: 批量老化：等待超过 _AGING_DELAY 秒后每 _AGING_STEP 秒优先级 -1，下限
#: _AGING_FLOOR（仍高于调试的 0——调试在 unit 边界总是优先，但第二个批量
#: 任务最终也能跑，不会被持续的调试流饿死）。
_AGING_DELAY = 120.0
_AGING_STEP = 60.0
_AGING_FLOOR = 2


class _Waiter:
    __slots__ = ("kind", "since", "granted", "event")

    def __init__(self, kind: str, since: float) -> None:
        self.kind = kind
        self.since = since
        self.granted = False
        self.event = asyncio.Event()


class _Lane:
    """单资源 lane：许可按等待者的**有效优先级**发放，而不是先到先得。

    有效优先级 = 基础优先级 − 老化折扣（批量等得越久越接近调试档）。许可
    只在 unit 边界（release / 取消转交）重新裁定——unit 内部不可抢占，那是
    chunk（块间让位）的职责，不是调度器的。
    """

    def __init__(self) -> None:
        self._held: Optional[_Waiter] = None
        self._waiters: list[_Waiter] = []

    def _effective_priority(self, w: _Waiter, now: float) -> float:
        base = LANE_PRIORITY.get(w.kind, LANE_PRIORITY["unknown"])
        if base <= LANE_PRIORITY["debug"]:
            return base                # 调试档不老化：交互优先是恒定的
        waited = now - w.since
        if waited > _AGING_DELAY:
            return max(base - int((waited - _AGING_DELAY) // _AGING_STEP), _AGING_FLOOR)
        return base

    def _winner(self) -> Optional[_Waiter]:
        if not self._waiters:
            return None
        now = asyncio.get_running_loop().time()
        return min(self._waiters,
                   key=lambda w: (self._effective_priority(w, now), w.since))

    def _grant(self) -> None:
        nxt = self._winner()
        if nxt is not None:
            nxt.granted = True
            nxt.event.set()

    async def acquire(self, kind: str, job_id: Optional[str] = None) -> None:
        w = _Waiter(kind, asyncio.get_running_loop().time())
        self._waiters.append(w)
        try:
            while True:
                if self._held is None and not w.granted:
                    self._grant()
                if w.granted:
                    self._waiters.remove(w)
                    self._held = w
                    return
                await w.event.wait()
        except asyncio.CancelledError:
            if w in self._waiters:
                self._waiters.remove(w)
            if w.granted:
                # 许可发给了被取消的等待者：立刻转交下一个赢家，别把 lane 带死
                self._held = None
                self._grant()
            raise

    def release(self) -> None:
        self._held = None
        self._grant()

    def snapshot(self) -> Dict[str, Any]:
        """排队现状（可观测）：持有者与各等待者的 kind/已等秒数/有效优先级。"""
        now = asyncio.get_running_loop().time()
        waiting = sorted(
            ({"kind": w.kind, "waiting_seconds": round(now - w.since, 1),
              "priority": self._effective_priority(w, now)}
             for w in self._waiters),
            key=lambda item: item["priority"])
        return {"held": self._held.kind if self._held else None,
                "waiting": waiting}


def _lane(name: str) -> _Lane:
    loop = asyncio.get_running_loop()
    return _LANES.setdefault(loop, {}).setdefault(name, _Lane())


def lane(name: str) -> _Lane:
    """取一条 lane（公开口）：给要**自管 lane 生命周期**的任务体用——
    jvm_run 的批量按块交还许可重排队，持有者不能是 runner._run。"""
    return _lane(name)


def lane_snapshot(name: str) -> Dict[str, Any]:
    return _lane(name).snapshot()


@asynccontextmanager
async def acquire_lane(name: str, kind: str = "batch",
                       job_id: Optional[str] = None):
    """按有效优先级串行执行一个共享资源 lane。"""
    lane = _lane(name)
    await lane.acquire(kind, job_id)
    try:
        yield
    finally:
        lane.release()


async def run_in_lane(name: str, kind: str, fn: Callable, *args, **kwargs):
    """在 lane 内跑一个同步阻塞函数，返回其结果。

    客户端断开（刷新/关页）会取消 HTTP 协程；这里用 shield 顶住第一次取消、
    把工作**等完**才放 lane——引擎调用不许被半路掐死（没有可恢复的中间态），
    lane 也只能跟着真正跑完的那次调用走。调试入口共用这一段（原在
    ``api/rules.py`` 两处与 ``api/ops.py`` 一处各抄一遍）。
    """
    async with acquire_lane(name, kind=kind):
        work = asyncio.create_task(asyncio.to_thread(fn, *args, **kwargs))
        try:
            return await asyncio.shield(work)
        except asyncio.CancelledError:
            await asyncio.shield(work)
            raise


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
    with Store() as st:
        st.create_job(job_id, kind, total=int(payload.get("total", 0) or 0),
                      payload=payload, retry_of=retry_of)
    TASKS[job_id] = asyncio.create_task(_run(job_id, kind, payload, lane))
    return job_id


def update_phase(job_id: str, phase: str) -> None:
    """把阶段写入任务表。

    JVM 的真正执行段在工作线程里，不能复用 async handler 持有的 Store 连接；
    这里短开独立连接，保证阶段在阻塞执行期间也能被 SSE 读到。
    """
    with Store() as st:
        st.update_job(job_id, phase=phase)


def update_progress(job_id: str, progress: int) -> None:
    """把进度写入任务表；短连接的理由同 update_phase。"""
    with Store() as st:
        st.update_job(job_id, progress=progress)


async def _run(job_id: str, kind: str, payload: Dict[str, Any],
               lane: Optional[str] = None) -> None:
    # jvm_run 的 lane 由任务体自己持有（run_jvm_job）：批量按块交还许可重排队，
    # 块边界在 handler 内部，runner 在外层持锁会让「块间让位」失效。
    lane_obj = _lane(lane) if (lane and kind != "jvm_run") else None
    acquired = False
    with Store() as st:
        try:
            if kind == "jvm_run":
                update_phase(job_id, "waiting_readiness")
            if lane_obj is not None:
                await lane_obj.acquire("batch", job_id)
                acquired = True
            if kind == "jvm_run":
                manifest = payload.get("manifest") or {}
                single = (manifest.get("single") if isinstance(manifest, dict)
                          and "single" in manifest else payload.get("single"))
                update_phase(job_id, "starting_worker" if single
                             else "preparing_engine")
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
                lane_obj.release()
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
    with Store() as st:
        n = st.fail_orphan_jobs()
    if n:
        print("警告: 上次进程结束时 %d 个任务没写终态，已标为 failed" % n, flush=True)
    return n


def sweep_expired() -> int:
    """清理已过期的终态任务，供启动和后台巡检共用。"""
    with Store() as st:
        return st.sweep_jobs()


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
    with Store() as st:
        job = st.get_job(job_id)
        if not job or job.get("status") in ("done", "failed", "cancelled"):
            return False
        st.update_job(job_id, status="cancel_requested", phase="cancel_requested")
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
