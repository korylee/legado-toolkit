# -*- coding: utf-8 -*-
"""JVM 校验的 HTTP 编排层（S2）。

四个端点：
  GET  /api/jvm/readiness  环境就绪检查（只读，不下载、不构建）
  POST /api/jvm/refresh    后台刷新 runtime snapshot
  POST /api/jvm/run        提交批量校验（进入 JVM lane 后由任务体后台执行）
  GET  /api/jvm/results    最近一批结论（从 meta 读，供列表合并展示）

执行引擎（manifest 写盘、参数文件、Gradle/daemon 调用、结果读回与落库）在
`backend/jobs/jvm_exec.py`，由它注册进 runner 的 "jvm_run" 任务；**测试的
patch 面在那里**（_run_gradle / _write_meta / _AGSVC / data_dir / Store）。
批量校验走 subprocess + 属性文件，不 import JVM 侧任何东西——两条进程的生命
周期完全独立，Robolectric 的堆/退出码都不影响后端进程。
"""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Optional
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool

from backend.jobs import jvm_exec, runner
from backend.jobs.jvm_exec import (_build_jvm_manifest, _cleanup_run_dir,
                                   _export_sources_file, _write_args)
from backend.schemas import JvmRunRequest
from core import settings_store
from core.checker import static_rule_complete
from core.jvm_direct import execution_readiness
from core.jvm_env import readiness
from core.loader import _normalize_url
from core.paths import data_dir
from core.store import ACTIVE_JOB_STATUSES, Store

router = APIRouter()

#: 结论 state → 展示口径（来源阶梯：JVM < 真机）
STATE_LABEL = {
    "ok": "可用（JVM·App 引擎）",
    "no_result": "搜索无结果（JVM）",
    "empty_js_shell": "无法验证·需浏览器（JVM）",
    #: A3：搜索为空 + 搜索页出现登录提示 → 需登录。**不是源坏了**，是这次没带登录态
    #: （结论里的 `cookie_len` 会说明带没带；`reason` 里有命中的那个词）
    "login_wall": "需登录（JVM）",
    "timeout": "超时（JVM）",
    "error": "执行失败（JVM）",
    "invalid": "源 JSON 无效（JVM）",
}


#: 允许**本次覆盖**的参数（白名单）。`app_repo` 是环境配置，不该被一次批量校验改掉；
#: 其余键走 `settings_store.coerce` 收敛（未知键丢弃、越界收到区间内）。
JVM_RUN_PARAMS = ("keyword", "timeout", "concurrency", "depth")


def _classify_validate_operation(daemon: Dict[str, Any], active_jobs: list,
                                 owner_job: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """保守判断是否可安全停止；证据不完整或冲突时拒绝停止。"""
    operation = daemon.get("current_operation")
    if not operation:
        if active_jobs:
            return {"owner_inactive": False,
                    "reason": "存在活跃 JVM 任务但没有对应 daemon operation marker"}
        if daemon.get("process_alive") and not daemon.get("responsive"):
            return {"owner_inactive": False,
                    "reason": "daemon 进程存活但无响应，无法确认是否正在执行操作"}
        if daemon.get("process_alive") and daemon.get("responsive") is not True:
            return {"owner_inactive": False,
                    "reason": "daemon 响应状态未知，不能确认空闲"}
        if daemon.get("process_alive") and daemon.get("signature_matches") is not True:
            return {"owner_inactive": False,
                    "reason": "daemon 签名不匹配，不能确认当前实例空闲"}
        if daemon.get("process_alive") and daemon.get("identity_verified") is not True:
            return {"owner_inactive": False,
                    "reason": "daemon 进程身份未知，不能确认空闲"}
        return {"owner_inactive": True,
                "reason": "没有操作 marker，也没有活跃 JVM 任务"}

    operation_id = str(operation.get("operation_id") or "").strip()
    job_id = str(operation.get("job_id") or "").strip()
    owner_pid = operation.get("pid")
    owner_created = str(operation.get("process_created") or "").strip()
    if not operation_id or not job_id or not owner_pid or not owner_created:
        return {"owner_inactive": False,
                "reason": "daemon operation marker 缺少 operation_id、job_id 或 owner 实例身份"}
    if daemon.get("owner_process_alive") not in (True, False):
        return {"owner_inactive": False,
                "reason": "owner 进程状态未知"}
    if daemon.get("owner_process_alive") and daemon.get("owner_instance_matches") is not True:
        return {"owner_inactive": False,
                "reason": "owner PID 活着但进程实例身份不匹配"}

    matching_active = [job for job in active_jobs if str(job.get("id") or "") == job_id]
    if len(matching_active) > 1:
        return {"owner_inactive": False,
                "reason": "同一 job 出现多条活跃记录，owner 状态冲突"}
    if matching_active:
        job = matching_active[0]
        if job.get("kind") != "jvm_run":
            return {"owner_inactive": False,
                    "reason": "marker 对应的活跃 job 类型不是 jvm_run"}
        if owner_job is None or str(owner_job.get("id") or "") != job_id:
            return {"owner_inactive": False,
                    "reason": "活跃 job 与 owner job 读取结果不一致"}
        if daemon.get("owner_process_alive") is True:
            return {"owner_inactive": False,
                    "reason": "marker 对应的 job 与 owner 进程实例仍活跃"}
        if daemon.get("owner_process_alive") is False:
            return {"owner_inactive": False,
                    "reason": "job 仍活跃但 marker owner 进程已退出，状态冲突"}
        return {"owner_inactive": False,
                "reason": "owner 进程状态未知"}

    if owner_job is None:
        return {"owner_inactive": False,
                "reason": "marker 对应 job 不存在，不能证明 owner 已结束"}
    if str(owner_job.get("id") or "") != job_id:
        return {"owner_inactive": False,
                "reason": "查询到的 job ID 与 marker 不一致"}
    if owner_job.get("kind") != "jvm_run":
        return {"owner_inactive": False,
                "reason": "marker 对应的终态 job 类型不是 jvm_run"}
    if owner_job.get("status") not in ("done", "failed", "cancelled"):
        return {"owner_inactive": False,
                "reason": "owner job 状态缺失或不是已知终态"}
    if daemon.get("owner_process_alive") is not False:
        return {"owner_inactive": False,
                "reason": "owner 后端进程尚存活或进程身份状态未知"}
    if daemon.get("owner_identity_verified") is not True:
        return {"owner_inactive": False,
                "reason": "owner 实例身份无法验证"}
    if not operation.get("instance_id"):
        return {"owner_inactive": False,
                "reason": "operation marker 缺少 owner instance_id"}
    if daemon.get("owner_instance_exited") is not True:
        return {"owner_inactive": False,
                "reason": "owner 后端进程实例身份无法验证，不能排除 PID 复用"}
    return {"owner_inactive": True,
            "reason": "owner 后端进程实例已退出且对应 job 已终态"}


def _daemon_status_with_jobs() -> Dict[str, Any]:
    from core import jvm_validate_daemon

    daemon = jvm_validate_daemon.status()
    with Store() as st:
        active = [j for j in st.list_jobs(limit=10000, statuses=ACTIVE_JOB_STATUSES)
                  if j.get("kind") in ("jvm_run", "jvm_debug")]
        active_jobs = [{k: j.get(k) for k in
                        ("id", "kind", "status", "phase", "progress", "total")}
                       for j in active]
        operation = daemon.get("current_operation") or {}
        owner_job_id = str(operation.get("job_id") or "")
        owner_job = st.get_job_summary(owner_job_id) if owner_job_id else None

    classified = _classify_validate_operation(daemon, active_jobs, owner_job)
    process_alive = bool(daemon.get("process_alive"))
    identity_ready = bool(daemon.get("responsive") and daemon.get("identity_verified"))
    has_daemon_record = bool(daemon.get("pid"))
    stop_allowed = (has_daemon_record and classified["owner_inactive"]
                    and (not process_alive or identity_ready))
    if not classified["owner_inactive"]:
        stop_reason = classified["reason"]
    elif process_alive and not identity_ready:
        stop_reason = "进程仍存活但 daemon 未响应或身份无法验证，不停止 daemon"
    elif not has_daemon_record:
        stop_reason = "没有需要停止的 Validate daemon"
    else:
        stop_reason = "没有活跃 owner，且 daemon 身份可确认，可以安全停止"
    return {
        **daemon,
        "active_jobs": active_jobs,
        "owner_inactive": classified["owner_inactive"],
        "stop_allowed": stop_allowed,
        "stop_reason": stop_reason,
    }


@router.get("/daemon/status")
def jvm_daemon_status():
    snapshot = _daemon_status_with_jobs()
    snapshot.pop("owner_inactive", None)
    return snapshot


@router.post("/daemon/stop")
async def jvm_daemon_stop():
    lane = runner.lane("jvm")
    lane_acquired = False
    try:
        await asyncio.wait_for(lane.acquire("debug", job_id="daemon-stop"), timeout=0.25)
        lane_acquired = True
    except asyncio.TimeoutError:
        raise HTTPException(409, "校验引擎正在被任务使用，暂不停止")
    try:
        from core.jvm_debug import RUN_LOCK
        if not RUN_LOCK.acquire(blocking=False):
            raise HTTPException(409, "校验引擎正在处理请求，暂不停止")
        try:
            snapshot = await run_in_threadpool(_daemon_status_with_jobs)
            if not snapshot.get("stop_allowed"):
                raise HTTPException(409, str(snapshot.get("stop_reason") or "无法安全停止 daemon"))
            from core import jvm_validate_daemon
            result = await run_in_threadpool(
                jvm_validate_daemon.stop_idle,
                owner_inactive=bool(snapshot.get("owner_inactive")),
                expected_operation_id=(snapshot.get("current_operation") or {}).get("operation_id"))
            if not result.get("stopped"):
                raise HTTPException(409, str(result.get("reason") or "daemon 未确认停止"))
            return result
        finally:
            RUN_LOCK.release()
    finally:
        if lane_acquired:
            lane.release()


@router.get("/readiness")
def jvm_readiness():
    conf = settings_store.load().get("jvm", {})
    return readiness(conf.get("app_repo", ""), conf.get("android_sdk_dir", ""))


@router.post("/refresh", status_code=202)
async def jvm_refresh():
    job_id = runner.submit("jvm_refresh", {}, lane="jvm", lane_kind="batch")
    return {"job_id": job_id, "kind": "jvm_refresh",
            "events": "/api/jobs/%s/stream" % job_id}


def _pick_directory(title: str):
    """打开本机原生目录选择器；浏览器 file input 无法提供可用的本机路径。"""
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        try:
            selected = filedialog.askdirectory(title=title, mustexist=True)
        finally:
            root.destroy()
    except Exception as e:
        raise HTTPException(503, "无法打开本机目录选择器：%s；也可以直接输入目录路径" % e)
    return {"path": str(Path(selected).resolve()) if selected else "",
            "cancelled": not bool(selected)}


@router.post("/pick-app-repo")
def pick_app_repo():
    return _pick_directory("选择 Legado App 源码仓库目录")


@router.post("/pick-android-sdk")
def pick_android_sdk():
    return _pick_directory("选择 Android SDK 根目录")


@router.post("/run")
async def jvm_run(body: Optional[JvmRunRequest] = None):
    conf = settings_store.load().get("jvm", {})
    if not conf.get("app_repo"):
        raise HTTPException(400, "JVM 校验未配置：请先在设置里填 App 源码目录并自检")

    #: 只跑这几条源（列表页勾选的）。空 = 全部在用源
    want_urls = [u for u in ((body.urls if body else []) or []) if str(u or "").strip()]
    want_filter = dict((body.filter if body else None) or {})
    want_params = dict((body.params if body else None) or {})

    # 请求先快照输入，真正执行由 `runner` 的 JVM lane 按提交顺序排队。
    # 不能在这里抢 `RUN_LOCK`：否则多个请求仍会在 HTTP 层直接失败，而不是进入队列。
    with Store() as st:
        run_dir = data_dir() / "app_probe" / "runs" / ("batch-" + uuid4().hex)
        src_file = _export_sources_file(
            st, want_urls, want_filter, dest_path=run_dir / "sources.json")
    rows = json.loads(src_file.read_text(encoding="utf-8"))
    match_report = {}
    if want_urls:
        matched_match = {
            _normalize_url(str(row.get("bookSourceUrl") or "")) for row in rows
        }
        match_report = {
            "requested": want_urls,
            "matched": [url for url in want_urls if _normalize_url(url) in matched_match],
            "missing": [url for url in want_urls if _normalize_url(url) not in matched_match],
        }
    if want_urls and not rows:
        _cleanup_run_dir(run_dir)
        missing = match_report["missing"]
        return {"started": False,
                "requested_urls": match_report["requested"],
                "matched_urls": match_report["matched"],
                "missing_urls": missing,
                "reason": "选中的 %d 条源一条都没匹配上：%s"
                          "（库中可能已删除、已停用，或 URL 与所选不同）"
                          % (len(want_urls), "、".join(missing))}
    if want_filter and not want_urls and not rows:
        _cleanup_run_dir(run_dir)
        return {"started": False, "reason": "当前筛选没有命中任何源，不用跑"}

    skipped_rule_incomplete = sum(
        1 for row in rows if static_rule_complete(row) is False)
    if skipped_rule_incomplete:
        rows = [row for row in rows if static_rule_complete(row) is not False]
        src_file.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8", newline="\n")
        if not rows:
            _cleanup_run_dir(run_dir)
            return {
                "started": False,
                "skipped_rule_incomplete": skipped_rule_incomplete,
                "reason": ("跳过 %d 条规则不完整的源；没有可校验的书源"
                           % skipped_rule_incomplete),
            }

    # **本次覆盖**：逐键过 coerce（未知键丢弃、越界收敛到区间内），只作用于
    # 这一次批量校验，不写回设置（理由见 schemas）。白名单见 JVM_RUN_PARAMS
    eff = dict(conf)
    for key in JVM_RUN_PARAMS:
        if key in want_params:
            got = settings_store.coerce("jvm", key, want_params[key])
            if got is not None:
                eff[key] = got
    params = {key: eff[key] for key in JVM_RUN_PARAMS}
    chunk_size = settings_store.coerce("jvm", "chunk_size",
                                          eff["chunk_size"])

    # 完整 readiness 是 Gradle/SDK 的准备态；单条请求可以只依赖已有的
    # runtime snapshot + Validate daemon
    st_conf = readiness(conf.get("app_repo", ""), conf.get("android_sdk_dir", ""))
    single = len(rows) == 1
    exec_conf: Dict[str, Any] = {}
    preparation_ready = bool(st_conf.get("ok"))
    allow_gradle_fallback = preparation_ready
    if single:
        # 单条任务无论是否允许 Gradle fallback，都必须在提交时冻结一份
        # runtime snapshot 的执行态检查；否则「准备态完整」会把已失效的
        # classpath / Java / 源码快照带进 daemon，直到 daemon 自己失败才暴露。
        exec_conf = execution_readiness()
        if not preparation_ready and not exec_conf.get("ok"):
            _cleanup_run_dir(run_dir)
            return {
                "started": False,
                "reason": exec_conf.get("reason") or "单条 JVM 执行态未就绪",
                "readiness": st_conf,
                "execution_readiness": exec_conf,
                "skipped_rule_incomplete": skipped_rule_incomplete,
            }
    elif not preparation_ready:
        _cleanup_run_dir(run_dir)
        return {"started": False, "readiness": st_conf,
                "skipped_rule_incomplete": skipped_rule_incomplete}
    # 绝对路径的理由同 _export_sources_file：这个路径是给**另一个进程**
    # （CWD = App 仓库根）用的，相对路径会落到 App 仓库里去
    out_path = run_dir / "results.jsonl"
    args_path = run_dir / "args.properties"
    try:
        _write_args(params["keyword"], params["timeout"],
                    params["concurrency"], out_path, src_file,
                    params["depth"], args_path=args_path)
    except Exception:
        _cleanup_run_dir(run_dir)
        raise

    # 交给任务跑（分钟级）：预检里已经算好的那几项回给前端做进度条，
    # 也让 job 结果的形状与旧版一致（`count` / `dist` / `checks` 都是任务体填的）
    prep = {
        "started": True,
        "count": len(rows),
        "skipped_rule_incomplete": skipped_rule_incomplete,
        "execution_plan": "validate_daemon",
        **({"requested_urls": match_report["requested"],
           "matched_urls": match_report["matched"],
           "missing_urls": match_report["missing"]} if want_urls else {}),
    }
    execution_plan = "validate_daemon"
    # 分块大小（提交即冻结）：settings 的 jvm.chunk_size，越界值已被 coerce 收敛
    cs = chunk_size
    n = len(rows)
    chunk_sizes = [cs] * (n // cs) + ([n % cs] if n % cs else []) or [n]
    manifest = _build_jvm_manifest(
        run_dir=run_dir, source_file=src_file, args_file=args_path,
        out_path=out_path, single=single, execution_plan=execution_plan,
        allow_gradle_fallback=(allow_gradle_fallback if single else False),
        runtime=dict(st_conf.get("runtime") or {}),
        readiness=st_conf,
        execution_readiness=exec_conf,
        readiness_fingerprint=st_conf.get("fingerprint", ""),
        readiness_checked_at=st_conf.get("checked_at", ""),
        source_count=len(rows),
        urls=[_normalize_url(str(r.get("bookSourceUrl") or "")) for r in rows],
        params=params,
        chunks=chunk_sizes,
    )
    # `total` 进 payload：一次批量校验没有逐条进度（一次 Gradle 调用跑一批），但**条数**
    # 要给前端算「N/N」——不给的话状态条永远停在 0
    job_id = runner.submit(
        "jvm_run",
        {"prep": prep, "total": len(rows), "manifest": manifest,
         "run_dir": str(run_dir),
          "source_file": str(src_file), "args_file": str(args_path),
          "out_path": str(out_path), "runtime": dict(st_conf.get("runtime") or {}),
          "single": single,
          "allow_gradle_fallback": allow_gradle_fallback,
          "execution_readiness": exec_conf,
          "readiness": st_conf,
          "readiness_fingerprint": st_conf.get("fingerprint", ""),
          "readiness_checked_at": st_conf.get("checked_at", "")},

        lane="jvm",
    )
    return dict(prep, **{"job_id": job_id})


@router.get("/results")
def jvm_results():
    """每源**最深**的一条 JVM 结论，供列表/面板展示。

    取舍规则收在 ``Store.latest_jvm_conclusions``（深者胜、同深取新）——
    这里**不再自己实现一遍**：列表回填读的是同一张表，两处各写一次就会漂
    （lessons §二十三 的「同一件事两个实现」已经栽过两次）。
    """
    with Store() as st:
        latest = st.latest_jvm_conclusions()
    dist = Counter(d.get("state") for d in latest.values())
    stage_dist = Counter(d.get("stage") for d in latest.values())
    batches = sorted({str(d.get("_batch") or "") for d in latest.values()}, reverse=True)
    return {
        "count": len(latest),
        "batches": [b for b in batches if b][:5],
        "dist": dict(dist),
        "stage_dist": dict(stage_dist),
        "label": STATE_LABEL,
        "items": [{"url": u, **d} for u, d in latest.items()],
    }
