# -*- coding: utf-8 -*-
"""JVM 校验服务的后端接口（S2）。

三个端点：
  GET  /api/jvm/selftest   环境自检（只读：推导 JDK/SDK/gradle-home，不装东西）
  POST /api/jvm/run        提交跑批（subprocess 调启动器；进入 JVM lane 后后台执行）
  GET  /api/jvm/results    最近一批结论（从 meta 读，供列表合并展示）

设计要点：
- 跑批走 **subprocess + 属性文件**，不 import JVM 侧任何东西——两条进程的生命
  周期完全独立，Robolectric 的堆/退出码都不影响后端进程。
- 结论读回（meta 表 jvm_check:<batch>:<url>）由 jvm_readback 逻辑内联在 run 里，
  跑完立即落库，失败也要留下已完成的部分。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool

from backend.api.check_summary import (ITEMS_LIMIT, check_items_from_checks,
                                       summarize_transitions)
from backend.jobs import runner
from backend.schemas import JvmRunRequest
from core.paths import data_dir
from core.store import Store
from core import settings_store
from core.jvm_env import selftest
from core.loader import _normalize_url
from core.paths import ARGS_PARTS

router = APIRouter()

_ROOT = Path(__file__).resolve().parents[2]
_AGSVC = _ROOT / "appservice"

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


def _launcher() -> Path:
    p = _AGSVC / "legado-gradle.bat"
    if not p.exists():
        raise HTTPException(500, "找不到启动器 appservice/legado-gradle.bat")
    return p


def _args_file() -> Path:
    """启动器的参数文件（data/ 下，零件见 `core.paths.ARGS_PARTS`）。

    **在调用时算**：测试会换 `data_dir`，import 时算死的常量会把它钉在真目录上。
    """
    return Path(data_dir()).joinpath(*ARGS_PARTS)


def _cleanup_run_dir(run_dir: Optional[Path]) -> None:
    """只清理本模块创建的单次运行目录，缺失目录时绝不把当前目录当目标。"""
    if run_dir is None:
        return
    root = (data_dir() / "app_probe" / "runs").resolve()
    try:
        target = run_dir.resolve()
    except OSError:
        return
    if target.parent != root or target == root:
        return
    shutil.rmtree(target, ignore_errors=True)


def _write_args(keyword: str, timeout: int, concurrency: int, limit: int,
                out_path: Path, source_file: Path, depth: str = "search",
                args_path: Optional[Path] = None) -> None:
    """把跑批参数写进启动器的参数文件（Launcher 的唯一参数入口）。"""
    from core.jvm_env import selftest  # 局部导入避免循环
    repo = settings_store.load().get("jvm", {}).get("app_repo", "").strip()
    st_conf = settings_store.load().get("jvm", {})
    if not repo:
        raise HTTPException(400, "JVM 校验未配置：设置里缺少 App 源码目录")
    lines = [
        "# 由后端 /api/jvm/run 生成（手工跑批时也可自己改）",
        "file=%s" % source_file.as_posix(),
        "keyword=%s" % (keyword or st_conf.get("keyword", "我")),
        "out=%s" % out_path.as_posix(),
        "timeout=%d" % (timeout or st_conf.get("timeout", 25)),
        "concurrency=%d" % (concurrency or st_conf.get("concurrency", 8)),
        # 深度也进参数文件（Launcher 读它转发给 --depth）。**不硬编码 search**：
        # 设置里选了什么就跑什么，跑批结论里的 stage 才与实际一致
        "depth=%s" % (depth or st_conf.get("depth", "search")),
    ]
    if limit:
        lines.append("limit=%d" % limit)
    # newline="\n" 是必须的：这是 **git 跟踪的文件**，而 write_text 在 Windows 上
    # 把 \n 翻成 \r\n——跑一次批工作区就脏一次（内容与 HEAD 逐字节相同，只差行尾，
    # git diff 连内容都不显示，只在 git add 时冒一句 warning）。同 agent-write-safety §三。
    target = args_path or _args_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def _export_sources_file(st, urls: Optional[List[str]] = None,
                         filt: Optional[Dict[str, Any]] = None,
                         dest_path: Optional[Path] = None) -> Path:
    """把管理库在用源导出成 JVM 侧可吃的 JSON 文件（与 S1 手工导出同一形状）。

    **三种范围，顺序即优先级**：

    1. `urls` 非空 —— 只导出这几条（列表页勾选的源）。**两边都先归一再比**（AGENTS #5）：
       前端给的是库里归一化过的 `source_url`，而这里读的是源 JSON 里 `bookSourceUrl`
       的原文——不归一就会一条都匹配不上，而表现是「跑完了但列没变」，不报错。
    2. `filt` 非空 —— 当前筛选命中的全部源（复用 `Store.export_by_filter`，不受分页限制）。
       形状与 `POST /api/export` 的 `filter` 一致（同一个前端对象直接递过来）。
    3. 都没有 —— 全部在用源。
    """
    import io
    from core.jvm_debug import apply_proxy_header
    from core.settings_store import resolve_proxy

    proxy = resolve_proxy()
    want = {_normalize_url(u) for u in (urls or []) if str(u or "").strip()}
    if want:
        views = st.export_sources()
    elif filt:
        views = st.export_by_filter(
            source_type=(int(filt["type"]) if filt.get("type") not in (None, "") else None),
            group=str(filt.get("group") or ""),
            health=str(filt.get("health") or ""),
            q=str(filt.get("q") or ""),
            only_enabled=bool(filt.get("only_enabled")),
            user_tag=str(filt.get("tag") or ""),
        )
    else:
        views = st.export_sources()
    out = []
    for view in views:
        if not view.get("enabled", 1):
            continue
        # `export_sources()` 给的是**解析好的书源对象**（`Store._source_view` 的输出，
        # 已并入 group_name/user_tags），不是带 `raw_json` 的包装。
        # 原先是 `raw = view.get("raw_json")`（恒为 None）→ `d = raw` → `d.get(...)`：
        # **任何有在用源的库都会在这里抛异常**（实测：真库副本上 TypeError）。
        # 之前没暴露，是因为它的测试把这个函数整个打桩了、而历史上的批量是走 CLI + 导出
        # 文件那条路（库里那几千条 jvm_check 结论不是这个端点写的）。
        d = view
        if want and _normalize_url(str(d.get("bookSourceUrl") or "")) not in want:
            continue
        row = {k: d.get(k) for k in (
            "bookSourceName", "bookSourceUrl", "searchUrl", "exploreUrl",
            "ruleSearch", "ruleBookInfo", "ruleToc", "ruleContent",
            "header", "bookSourceType", "enabled", "bookSourceGroup") if k in d}
        # 代理走源的 header（App 唯一认的注入点，十-3）——跑批与调试因此走同一个出口
        out.append(apply_proxy_header(row, proxy))
    # **必须绝对路径**：这两个路径是写给**另一个进程**用的——启动器会 `pushd` 到
    # App 仓库根再跑 Gradle，测试 JVM 的 CWD 就是那里。相对路径于是解析到
    # `<App 仓库>/data/...`：轻则后端 `out_path.exists()` 找不到（报「启动器没有
    # 产出结果文件」），重则**往 App 仓库里写目录**——那是零入侵红线（AGENTS 的
    # 「App 仓库 git status 必须为空」）。`core.paths.data_dir()` 是仓库自己的解析口。
    probe_dir = data_dir() / "app_probe"
    path = dest_path or (probe_dir / "jvm_batch.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8", newline="\n")
    return path


def _run_gradle(timeout_min: int = 90, args_path: Optional[Path] = None) -> int:
    """调启动器跑批（阻塞直到 Gradle 退出）。返回退出码。

    **顺手刷新 dump**：与 `core.jvm_debug._run_launcher` 同一条不变式（机制在
    `core.jvm_direct.dump_is_stale` 与 `core.jvm_debug.default_launcher` 那两处，别在这抄）。
    """
    exe = _launcher()
    env = {**os.environ,
           "LEGADO_TEST_JVM_ENV_OUT": str(data_dir() / "app_probe" / "test_jvm_env.json"),
           # **必须显式告诉它参数文件在哪**：不给就退回「挨着启动器找」，那里没有，
           # 于是启动器打印一句「跳过」之后什么都不跑（一次看不出来的空跑）
           "LEGADO_APPSERVICE_ARGS": str(args_path or _args_file())}
    proc = subprocess.run(
        ["cmd", "/c", str(exe), ":app:testAppDebugUnitTest",
         "--tests", "io.legado.app.service.ValidateServiceLauncher", "--rerun"],
        cwd=str(_AGSVC), env=env, capture_output=True, text=True,
        timeout=timeout_min * 60, errors="replace",
    )
    return proc.returncode


def _read_results(path: Path) -> List[Dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    if text.lstrip().startswith("["):
        return json.loads(text)
    return [json.loads(l) for l in text.splitlines() if l.strip()]


def _write_meta(rows: List[Dict[str, Any]]) -> str:
    """结论写 meta（jvm_check:<batch>:<url>），幂等。返回 batch id。"""
    from core.store import Store
    batch = time.strftime("%Y%m%d_%H%M%S")
    st = Store()
    try:
        for r in rows:
            url = _normalize_url(str(r.get("url", "") or ""))
            if not url:
                continue
            st.conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
                ("jvm_check:%s:%s" % (batch, url), json.dumps(r, ensure_ascii=False)))
        st.conn.commit()
    finally:
        st.close()
    return batch


@router.get("/selftest")
def jvm_selftest():
    conf = settings_store.load().get("jvm", {})
    return selftest(conf.get("app_repo", ""))


@router.post("/run")
async def jvm_run(body: Optional[JvmRunRequest] = None):
    conf = settings_store.load().get("jvm", {})
    if not conf.get("app_repo"):
        raise HTTPException(400, "JVM 校验未配置：请先在设置里填 App 源码目录并自检")

    # 自检不过就不开跑——跑一半 OOM/路径错只会浪费时间
    st_conf = selftest(conf.get("app_repo", ""))
    if not st_conf["ok"]:
        return {"started": False, "selftest": st_conf}

    #: 只跑这几条源（列表页勾选的）。空 = 全部在用源
    want_urls = [u for u in ((body.urls if body else []) or []) if str(u or "").strip()]
    want_filter = dict((body.filter if body else None) or {})
    want_params = dict((body.params if body else None) or {})

    # 请求先快照输入，真正执行由 `runner` 的 JVM lane 按提交顺序排队。
    # 不能在这里抢 `RUN_LOCK`：否则多个请求仍会在 HTTP 层直接失败，而不是进入队列。
    st = Store()
    run_dir = data_dir() / "app_probe" / "runs" / ("batch-" + uuid4().hex)
    src_file = _export_sources_file(st, want_urls, want_filter,
                                    dest_path=run_dir / "sources.json")
    st.close()
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
    prep = {"started": True, "count": len(rows)}
    # `total` 进 payload：一次跑批没有逐条进度（一次 Gradle 调用跑一批），但**条数**
    # 要给前端算「N/N」——不给的话状态条永远停在 0
    job_id = runner.submit(
        "jvm_run",
        {"prep": prep, "total": len(rows), "run_dir": str(run_dir),
         "source_file": str(src_file), "args_file": str(args_path),
         "out_path": str(out_path)},
        lane="jvm",
    )
    return dict(prep, **{"job_id": job_id})


@runner.register("jvm_run")
async def run_jvm_job(job_id: str, st: Store, payload: Dict[str, Any]) -> Dict[str, Any]:
    """跑批任务体。`payload`：`out_path`（结果文件）+ `prep`（预检里算好的摘要，原样回给前端）。

    **跑批是分钟级的**（全量十几分钟），所以它是一条 job 而不是一个长 HTTP 请求：
    关掉页面、刷新、断网都不再让那十几分钟白等（结论本来就会落库，但界面从此知道
    它跑完了）。形状与本地校验那条 `check` 一样：POST /api/jobs → SSE 订阅。

    **锁在这个线程里拿、也在它里面放**：原来锁挂在 HTTP 处理函数上，客户端一断开
    Starlette 会取消那个协程，`finally` 就提前把锁放了，而 Gradle 还在跑——下一个请求
    立刻能进来踩同一份 `args.properties`（TODO 里记着的那处）。搬进任务体之后，取消
    最多把这条 job 记成 cancelled，锁仍跟着**执行它的线程**走（它跑完才放）。
    """
    from core.jvm_debug import BUSY_REASON, RUN_LOCK

    # 新任务从提交时生成的目录取输入/输出；旧的直接调用测试仍允许不带目录。
    run_dir_value = str(payload.get("run_dir") or "").strip()
    run_dir = Path(run_dir_value) if run_dir_value else None
    if run_dir is None:
        out_path = data_dir() / "app_probe" / "jvm_results.jsonl"
        args_path = None
    else:
        out_path = Path(str(payload.get("out_path") or run_dir / "results.jsonl"))
        args_path = Path(str(payload.get("args_file") or run_dir / "args.properties"))
    prep = dict(payload.get("prep") or {})

    def _work() -> Dict[str, Any]:
        if not RUN_LOCK.acquire(blocking=False):
            _cleanup_run_dir(run_dir)
            return dict(prep, **{"ok": False, "reason": BUSY_REASON})
        try:
            code = (_run_gradle(args_path=args_path) if args_path is not None
                    else _run_gradle())
            if not out_path.exists():
                return dict(prep, **{"ok": False, "exit": code,
                                    "reason": "启动器没有产出结果文件（看 Gradle 输出定位）"})
            rows = _read_results(out_path)
            batch = _write_meta(rows)
            # **上一版结论的快照必须在落库之前读**：跑完再读，每条源都是 old == new，
            # 「这次变了什么」会永远答「没变」——那正是这个字段要回答的问题（同一处
            # 理由在 `ops.run_check_job` 里写着，两边必须同规矩）
            prev_checks = st.checks_map()
            # 结论同时按 checks 的口径落库（六档 / 星级 / 深度）——列表与筛选读的是
            # checks，不落这一步的话健康列会在撤掉本地引擎之后断供（TODO §一点九）。
            # 映射与判据都在 core/jvm_health，**别在这里另写一份**。
            from core import jvm_health
            n_checks = jvm_health.store_checks(rows, batch=batch, store=st)
            # items 从**落库后的 checks** 取，而不是自己拿 rows 再算一遍六档/星级：
            # 前端列表读的就是那张表，这样两边天然一致（形状映射见 check_summary）
            names = {_normalize_url(str(r.get("url") or "")): str(r.get("name") or "")
                     for r in rows}
            fresh = st.checks_map()
            items = check_items_from_checks(
                {u: fresh[u] for u in names if u in fresh}, names=names)
            dist = Counter(r.get("state") for r in rows)
            return dict(prep, **{
                "ok": code == 0, "exit": code, "batch": batch,
                "count": len(rows), "dist": dict(dist), "checks": n_checks,
                # 结果体与**本地校验那条同形状**（前端单条校验两条路都读它）：
                # checked / items / transitions 是回填与摘要要的，cached 这条链
                # **没有页面缓存**（每次都真抓），报 0 是实话
                "checked": len(items), "cached": 0, "fetched": len(items),
                "transitions": summarize_transitions(prev_checks, items),
                "items": items[:ITEMS_LIMIT],
            })
        finally:
            RUN_LOCK.release()
            _cleanup_run_dir(run_dir)

    work = asyncio.create_task(run_in_threadpool(_work))
    try:
        return await asyncio.shield(work)
    except asyncio.CancelledError:
        # 取消 HTTP 请求不能提前释放 JVM lane；线程里的 Gradle 仍在跑，必须等它
        # 收尾后再让下一个任务进入。
        await asyncio.shield(work)
        raise


@router.get("/results")
def jvm_results():
    """每源**最深**的一条 JVM 结论，供列表/面板展示。

    取舍规则收在 ``Store.latest_jvm_conclusions``（深者胜、同深取新）——
    这里**不再自己实现一遍**：列表回填读的是同一张表，两处各写一次就会漂
    （lessons §二十三 的「同一件事两个实现」已经栽过两次）。
    """
    st = Store()
    try:
        latest = st.latest_jvm_conclusions()
    finally:
        st.close()
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
