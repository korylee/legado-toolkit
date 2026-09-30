# -*- coding: utf-8 -*-
"""JVM 校验的 HTTP 编排层（S2）。

三个端点：
  GET  /api/jvm/readiness  环境就绪检查（只读，不下载、不构建）
  POST /api/jvm/run        提交跑批（进入 JVM lane 后由任务体后台执行）
  GET  /api/jvm/results    最近一批结论（从 meta 读，供列表合并展示）

执行引擎（manifest 写盘、参数文件、Gradle/daemon 调用、结果读回与落库）在
`backend/jobs/jvm_exec.py`，由它注册进 runner 的 "jvm_run" 任务；**测试的
patch 面在那里**（_run_gradle / _write_meta / _AGSVC / data_dir / Store）。
跑批走 subprocess + 属性文件，不 import JVM 侧任何东西——两条进程的生命
周期完全独立，Robolectric 的堆/退出码都不影响后端进程。
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Optional
from uuid import uuid4

from fastapi import APIRouter, HTTPException

from backend.jobs import jvm_exec, runner
from backend.jobs.jvm_exec import (_build_jvm_manifest, _cleanup_run_dir,
                                   _export_sources_file, _write_args)
from backend.schemas import JvmRunRequest
from core import settings_store
from core.jvm_direct import execution_readiness
from core.jvm_env import readiness
from core.loader import _normalize_url
from core.paths import data_dir
from core.store import Store

router = APIRouter()

#: 结论 state → 展示口径（来源阶梯：本地回放 < JVM < 真机）
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


#: 允许**本次覆盖**的参数（白名单）。`app_repo` 是环境配置，不该被一次跑批改掉；
#: 其余键走 `settings_store.coerce` 收敛（未知键丢弃、越界收到区间内）。
#: `limit` 在里面：范围（勾选 / 筛选）优先，而"只跑前 N 条"只有它能表达——
#: 设置页放开它的时候，界面上写着「全部在用源」而实际被截成 N 条，看不出来。
JVM_RUN_PARAMS = ("keyword", "timeout", "concurrency", "depth", "limit")


@router.get("/readiness")
def jvm_readiness():
    conf = settings_store.load().get("jvm", {})
    return readiness(conf.get("app_repo", ""), conf.get("android_sdk_dir", ""))


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
        src_file = _export_sources_file(st, want_urls, want_filter,
                                        dest_path=run_dir / "sources.json")
    rows = json.loads(src_file.read_text(encoding="utf-8"))
    if want_urls and not rows:
        _cleanup_run_dir(run_dir)
        # 一条都没匹配上：**说清楚**，别开一次空跑（那会让用户以为「跑过了、源没问题」）
        return {"started": False,
                "reason": "选中的 %d 条源一条都没匹配上（可能已被删除，或 URL 改过）"
                          % len(want_urls)}
    if want_filter and not want_urls and not rows:
        _cleanup_run_dir(run_dir)
        return {"started": False, "reason": "当前筛选没有命中任何源，不用跑"}
    # **本次覆盖**：逐键过 coerce（未知键丢弃、越界收敛到区间内），只作用于
    # 这一次跑批，不写回设置（理由见 schemas）。白名单见 JVM_RUN_PARAMS
    eff = dict(conf)
    for key in JVM_RUN_PARAMS:
        if key in want_params:
            got = settings_store.coerce("jvm", key, want_params[key])
            if got is not None:
                eff[key] = got

    limit = int(eff.get("limit", 0) or 0)
    # **给了范围就不再看条数上限**：范围由选中/筛选决定，否则会出现
    # 「选了 20 条只跑了 3 条」这种看不出来的截断
    if limit > 0 and not want_urls and not want_filter:
        rows = rows[:limit]
        src_file.write_text(json.dumps(rows, ensure_ascii=False),
                            encoding="utf-8", newline="\n")

    # 完整 readiness 是 Gradle/SDK 的准备态；单条请求可以只依赖已有的
    # runtime snapshot + Validate daemon。必须在最终截断后再判断 single，
    # 否则「全量 + limit=1」会误走批量规则。
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
            }
    elif not preparation_ready:
        _cleanup_run_dir(run_dir)
        return {"started": False, "readiness": st_conf}
    # 绝对路径的理由同 _export_sources_file：这个路径是给**另一个进程**
    # （CWD = App 仓库根）用的，相对路径会落到 App 仓库里去
    out_path = run_dir / "results.jsonl"
    args_path = run_dir / "args.properties"
    try:
        _write_args(eff.get("keyword", "我"), int(eff.get("timeout", 25)),
                    int(eff.get("concurrency", 8)), 0, out_path, src_file,
                    str(eff.get("depth", "search")), args_path=args_path)
    except Exception:
        _cleanup_run_dir(run_dir)
        raise

    # 交给任务跑（分钟级）：预检里已经算好的那几项回给前端做进度条，
    # 也让 job 结果的形状与旧版一致（`count` / `dist` / `checks` 都是任务体填的）
    prep = {
        "started": True,
        "count": len(rows),
        "execution_plan": "validate_daemon" if single else "gradle",
        "preparation_ready": preparation_ready,
    }
    execution_plan = "validate_daemon" if single else "gradle"
    # 分块大小（提交即冻结）：settings 的 jvm.chunk_size，越界值已被 coerce 收敛
    cs = int(eff.get("chunk_size") or 0) or 25
    n = len(rows)
    chunk_sizes = [cs] * (n // cs) + ([n % cs] if n % cs else []) or [n]
    manifest = _build_jvm_manifest(
        run_dir=run_dir, source_file=src_file, args_file=args_path,
        out_path=out_path, single=single, execution_plan=execution_plan,
        allow_gradle_fallback=allow_gradle_fallback,
        runtime=dict(st_conf.get("runtime") or {}),
        readiness=st_conf,
        execution_readiness=exec_conf,
        readiness_fingerprint=st_conf.get("fingerprint", ""),
        readiness_checked_at=st_conf.get("checked_at", ""),
        source_count=len(rows),
        urls=[_normalize_url(str(r.get("bookSourceUrl") or "")) for r in rows],
        params={k: eff.get(k) for k in JVM_RUN_PARAMS},
        chunks=chunk_sizes,
    )
    # `total` 进 payload：一次跑批没有逐条进度（一次 Gradle 调用跑一批），但**条数**
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
