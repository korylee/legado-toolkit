# -*- coding: utf-8 -*-
"""JVM 校验服务的后端接口（S2）。

三个端点：
  GET  /api/jvm/readiness  环境就绪检查（只读，不下载、不构建）
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
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
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
from core.jvm_env import process_environment, readiness
from core.jvm_direct import execution_readiness
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


def _build_jvm_manifest(*, run_dir: Path, source_file: Path, args_file: Path,
                        out_path: Path, single: bool, execution_plan: str,
                        allow_gradle_fallback: bool, runtime: Dict[str, Any],
                        readiness: Dict[str, Any],
                        execution_readiness: Dict[str, Any],
                        readiness_fingerprint: str,
                        readiness_checked_at: str, source_count: int,
                        urls: List[str], params: Dict[str, Any]) -> Dict[str, Any]:
    """生成一次提交即固定的 JVM 任务输入快照。

    ``payload`` 会落库，但 worker 仍不应依赖多处散落字段重新推导路径和执行计划。
    manifest 的哈希只覆盖实际字段；worker 读到后先验哈希，再消费这份快照。
    ``urls`` 是归一化后的范围（AGENTS #5），``params`` 是本次生效的校验参数——
    两者都是「提交即冻结」的输入，落进运行目录的 manifest.json 后不可再改。
    """
    manifest = {
        "schema": 2,
        "run_dir": str(run_dir),
        "source_file": str(source_file),
        "args_file": str(args_file),
        "out_path": str(out_path),
        "source_count": int(source_count),
        "single": bool(single),
        "execution_plan": str(execution_plan),
        "allow_gradle_fallback": bool(allow_gradle_fallback),
        "runtime": dict(runtime or {}),
        "readiness": dict(readiness or {}),
        "execution_readiness": dict(execution_readiness or {}),
        "readiness_fingerprint": str(readiness_fingerprint or ""),
        "readiness_checked_at": str(readiness_checked_at or ""),
        "urls": [str(u) for u in urls],
        "params": dict(params or {}),
    }
    encoded = json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"))
    manifest["sha256"] = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return manifest


def _job_retry_of(job_id: str) -> str:
    """读任务的 retry_of（jobs 表事实源）；manifest 信封只做记录。"""
    st = Store()
    try:
        row = st.get_job(job_id) or {}
    finally:
        st.close()
    return str(row.get("retry_of") or "")


def _write_run_manifest(run_dir: Path, manifest: Dict[str, Any], job_id: str) -> None:
    """把本次任务的输入信封写进运行目录（不可变的复查交付物）。

    SQLite 仍是任务管理事实源；这个文件让「重启/异常退出后这次任务用了什么
    输入、写到哪」不用翻库也能逐项对出。``inputs`` 是提交时冻结的 manifest
    （带 sha256）；job/owner/chunk/generation 是执行身份，不属于冻结范围。
    重试天然生成新的运行目录（uuid 命名），旧产物不会被覆盖；retry_of 记录
    它替代的是哪一次。
    """
    envelope = {
        "schema": 1,
        "job_id": job_id,
        "owner_pid": os.getpid(),
        "chunk": "",
        "generation": 1,
        "retry_of": _job_retry_of(job_id),
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "inputs": manifest,
    }
    try:
        (run_dir / "manifest.json").write_text(
            json.dumps(envelope, ensure_ascii=False, indent=1),
            encoding="utf-8", newline="\n")
    except OSError:
        pass


def _persist_runtime_snapshot(run_dir: Path) -> None:
    """把本次执行依赖的 runtime dump 复制一份进运行目录。

    缺失不拦：执行态检查会对「没有 dump」给出明确原因，这里只负责留存证据。
    """
    src = data_dir() / "app_probe" / "test_jvm_env.json"
    try:
        shutil.copyfile(src, run_dir / "runtime-snapshot.json")
    except OSError:
        pass


def _write_run_logs(args_path: Optional[Path], stdout: Any, stderr: Any) -> None:
    """Gradle 全量输出落进运行目录；结果体里只留尾部，完整版在这里。"""
    if args_path is None:
        return
    base = Path(args_path).parent
    try:
        base.mkdir(parents=True, exist_ok=True)
        (base / "stdout.log").write_text(str(stdout or ""), encoding="utf-8", newline="\n")
        (base / "stderr.log").write_text(str(stderr or ""), encoding="utf-8", newline="\n")
    except OSError:
        pass


def _manifest_error(manifest: Any) -> str:
    if not isinstance(manifest, dict):
        return "JVM 任务缺少不可变 manifest"
    required = {
        "schema", "run_dir", "source_file", "args_file", "out_path",
        "source_count", "single", "execution_plan", "allow_gradle_fallback",
        "runtime", "readiness", "execution_readiness",
        "readiness_fingerprint", "readiness_checked_at", "urls", "params",
    }
    missing = sorted(required - set(manifest))
    if missing or manifest.get("schema") != 2:
        detail = "缺少字段：%s" % ", ".join(missing) if missing else "schema 不是 2"
        return "JVM 任务 manifest 结构不受支持：%s" % detail
    if not isinstance(manifest.get("runtime"), dict) or not isinstance(manifest.get("readiness"), dict):
        return "JVM 任务 manifest 结构不受支持：runtime/readiness 不是对象"
    if not isinstance(manifest.get("execution_readiness"), dict):
        return "JVM 任务 manifest 结构不受支持：execution_readiness 不是对象"
    if not isinstance(manifest.get("urls"), list) or not isinstance(manifest.get("params"), dict):
        return "JVM 任务 manifest 结构不受支持：urls/params 形状不对"
    if manifest.get("execution_plan") not in ("validate_daemon", "gradle"):
        return "JVM 任务 manifest 结构不受支持：execution_plan 无效"
    if bool(manifest.get("single")) != (manifest.get("execution_plan") == "validate_daemon"):
        return "JVM 任务 manifest 结构不受支持：执行计划与 single 不一致"
    expected = str(manifest.get("sha256") or "")
    if not expected:
        return "JVM 任务 manifest 缺少 sha256"
    body = dict(manifest)
    body.pop("sha256", None)
    encoded = json.dumps(body, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"))
    actual = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    if actual != expected:
        return "JVM 任务 manifest 校验失败：提交后的执行输入被修改"
    return ""


def _write_args(keyword: str, timeout: int, concurrency: int, limit: int,
                out_path: Path, source_file: Path, depth: str = "search",
                args_path: Optional[Path] = None) -> None:
    """把跑批参数写进启动器的参数文件（Launcher 的唯一参数入口）。"""
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


def _tail_process_output(value: Any, limit: int = 4000) -> str:
    """把子进程输出收敛成可放进任务结果的文本尾部。"""
    if value is None:
        return ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return str(value).strip()[-limit:]


def _run_gradle(timeout_min: int = 90, args_path: Optional[Path] = None,
                runtime: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """调启动器跑批（阻塞直到 Gradle 退出）。保留退出码和输出尾部。

    以前这里只返回整数。Gradle 在测试 JVM 启动前失败时，调用方只能知道结果文件
    不存在，真正的 wrapper/native-platform 错误被丢掉。

    **顺手刷新 dump**：与 `core.jvm_debug._run_launcher` 同一条不变式（机制在
    `core.jvm_direct.dump_is_stale` 与 `core.jvm_debug.default_launcher` 那两处，别在这抄）。
    """
    exe = _launcher()
    if not runtime:
        raise ValueError("JVM runtime 快照缺失，拒绝使用后端进程环境启动")
    env = process_environment({"ok": True, "runtime": runtime})
    snapshot_dump = data_dir() / "app_probe" / "test_jvm_env.json"
    snapshot_actual = Path(str(snapshot_dump) + ".actual.gradle.validate.json")
    snapshot_report = snapshot_actual.with_suffix(".comparison.json")
    snapshot_dump.parent.mkdir(parents=True, exist_ok=True)
    for stale in (snapshot_dump, snapshot_actual, snapshot_report):
        try:
            stale.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass

    def with_runtime_snapshot(result: Dict[str, Any]) -> Dict[str, Any]:
        from core.jvm_runtime_snapshot import compare_runtime_snapshot
        result["runtime_snapshot"] = compare_runtime_snapshot(
            "gradle", "validate", path=snapshot_actual, runtime=runtime)
        return result
    env.update({
           "LEGADO_TEST_JVM_ENV_OUT": str(snapshot_dump),
           "LEGADO_TEST_JVM_LAUNCH_MODE": "gradle",
           # **必须显式告诉它参数文件在哪**：不给就退回「挨着启动器找」，那里没有，
           # 于是启动器打印一句「跳过」之后什么都不跑（一次看不出来的空跑）
           "LEGADO_APPSERVICE_ARGS": str(args_path or _args_file())})
    try:
        proc = subprocess.run(
            ["cmd", "/c", str(exe), ":app:testAppDebugUnitTest",
             "--tests", "io.legado.app.service.ValidateServiceLauncher", "--rerun"],
            cwd=str(_AGSVC), env=env, capture_output=True, text=True,
            timeout=timeout_min * 60, errors="replace",
        )
        _write_run_logs(args_path, proc.stdout, proc.stderr)
        return with_runtime_snapshot({
            "exit": proc.returncode,
            "stdout": _tail_process_output(proc.stdout),
            "stderr": _tail_process_output(proc.stderr),
        })
    except subprocess.TimeoutExpired as exc:
        _write_run_logs(args_path, exc.stdout, exc.stderr)
        return with_runtime_snapshot({
            "exit": None,
            "stdout": _tail_process_output(exc.stdout),
            "stderr": _tail_process_output(exc.stderr),
            "error": "Gradle 运行超过 %d 分钟" % timeout_min,
        })


def _normalize_gradle_result(raw: Any) -> Dict[str, Any]:
    """兼容旧调用方/测试桩返回整数，同时统一真实执行结果形状。"""
    if isinstance(raw, dict):
        result = dict(raw)
        result["stdout"] = _tail_process_output(result.get("stdout"))
        result["stderr"] = _tail_process_output(result.get("stderr"))
        return result
    return {"exit": raw, "stdout": "", "stderr": ""}


def _gradle_failure_reason(result: Dict[str, Any]) -> str:
    """生成能直接指导排查的 Gradle 失败原因。"""
    code = result.get("exit")
    pieces = ["启动器没有产出结果文件"]
    if code is not None:
        pieces.append("Gradle 退出码 %s" % code)
    if result.get("error"):
        pieces.append(str(result["error"]))
    for label, key in (("stderr", "stderr"), ("stdout", "stdout")):
        text = str(result.get(key) or "").strip()
        if text:
            pieces.append("Gradle %s 尾部：\n%s" % (label, text))
    pieces.append("请先看上面的 Gradle 输出，再处理启动环境")
    return "\n".join(pieces)



def _runtime_snapshot_failure_reason(result: Dict[str, Any]) -> str:
    """环境就绪快照漂移时阻断入库，其他快照问题只保留为诊断。"""
    snapshot = result.get("runtime_snapshot")
    if not isinstance(snapshot, dict):
        return ""
    differences = snapshot.get("differences")
    if not isinstance(differences, dict):
        return ""
    environment = differences.get("readinessEnvironment")
    if not isinstance(environment, dict) or not environment:
        return ""
    details = []
    for name, values in environment.items():
        if isinstance(values, dict):
            details.append("%s（提交时=%s，实际=%s）" %
                           (name, values.get("declared"), values.get("actual")))
        else:
            details.append("%s（%s）" % (name, values))
    return "JVM 实际启动环境与提交时的环境就绪快照不一致：" + "；".join(details)


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


#: 进度轮询间隔（秒）。测试会把它调小来驱动轮询。
_PROGRESS_POLL_INTERVAL = 1.0


def _tail_progress(job_id: str, out_path: Path, stop: threading.Event,
                   interval: float, cap: int = 0) -> None:
    """跑批期间把 results.jsonl 已落盘的完整行数当进度上报。

    Kotlin 侧每写完一条源就 flush（`ValidateService.kt` 的 writer 回调），数 ``\\n``
    就是「实际完成几条」，不是估的——这与「不编假进度」不冲突，编的是没有依据的数。
    文件还没出现（JVM 尚未写出第一条）时保持安静，不把 0 反复写库。
    """
    while not stop.wait(interval):
        try:
            with open(out_path, "rb") as fh:
                done = fh.read().count(b"\n")
        except OSError:
            continue
        if cap and done > cap:
            done = cap
        if done:
            runner.update_progress(job_id, done)


@router.get("/readiness")
def jvm_readiness():
    conf = settings_store.load().get("jvm", {})
    return readiness(conf.get("app_repo", ""), conf.get("android_sdk_dir", ""))


@router.post("/pick-app-repo")
def pick_app_repo():
    """打开本机原生目录选择器；浏览器 file input 无法提供可用的本机路径。"""
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        try:
            selected = filedialog.askdirectory(
                title="选择 Legado App 源码仓库目录", mustexist=True)
        finally:
            root.destroy()
    except Exception as e:
        raise HTTPException(503, "无法打开本机目录选择器：%s；也可以直接输入目录路径" % e)
    return {"path": str(Path(selected).resolve()) if selected else "",
            "cancelled": not bool(selected)}


@router.post("/pick-android-sdk")
def pick_android_sdk():
    """打开本机原生目录选择器，选择 Android SDK 根目录。"""
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        try:
            selected = filedialog.askdirectory(
                title="选择 Android SDK 根目录", mustexist=True)
        finally:
            root.destroy()
    except Exception as e:
        raise HTTPException(503, "无法打开本机目录选择器：%s；也可以直接输入目录路径" % e)
    return {"path": str(Path(selected).resolve()) if selected else "",
            "cancelled": not bool(selected)}


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
    from core.jvm_debug import RUN_LOCK

    # 新任务只从提交时生成的 manifest 取输入/输出；旧的直接调用测试仍允许不带 manifest。
    manifest_value = payload.get("manifest")
    manifest_reason = _manifest_error(manifest_value) if manifest_value is not None else ""
    has_manifest = isinstance(manifest_value, dict)
    manifest = manifest_value if has_manifest else {}
    run_dir_value = str(manifest.get("run_dir") if has_manifest
                        else payload.get("run_dir") or "").strip()
    run_dir = Path(run_dir_value) if run_dir_value else None
    if run_dir is None:
        out_path = data_dir() / "app_probe" / "jvm_results.jsonl"
        args_path = None
    else:
        out_path = Path(str(manifest.get("out_path") if has_manifest
                            else payload.get("out_path") or run_dir / "results.jsonl"))
        args_path = Path(str(manifest.get("args_file") if has_manifest
                            else payload.get("args_file") or run_dir / "args.properties"))
    prep = dict(payload.get("prep") or {})
    if manifest_reason:
        _cleanup_run_dir(run_dir)
        return dict(prep, **{"ok": False, "reason": manifest_reason,
                             "execution_mode": "unknown"})

    single = bool(manifest.get("single") if has_manifest else payload.get("single"))
    allow_gradle_fallback = bool(
        manifest.get("allow_gradle_fallback") if has_manifest
        else payload.get("allow_gradle_fallback", True))
    runtime = (dict(manifest.get("runtime") or {}) if has_manifest
               else payload.get("runtime"))
    execution_readiness_snapshot = (
        manifest.get("execution_readiness") if has_manifest
        else payload.get("execution_readiness"))
    readiness_snapshot = (manifest.get("readiness") if has_manifest
                          else payload.get("readiness"))

    def _work() -> Dict[str, Any]:
        # The async lane queues JVM jobs; acquire here in the worker thread as well.
        # This also waits for any non-lane holder instead of failing the submitted job.
        RUN_LOCK.acquire()
        tail_stop = threading.Event()
        tail = threading.Thread(
            target=_tail_progress, name="jvm-progress-tail",
            args=(job_id, out_path, tail_stop, _PROGRESS_POLL_INTERVAL,
                  int(manifest.get("source_count") or payload.get("total") or 0)),
            daemon=True)
        try:
            tail.start()
            # 任务信封 + runtime 快照：执行一开始就落盘。inputs 是提交时冻结的
            # manifest（带 sha256），信封上的 job/owner/chunk 是执行身份——崩溃后
            # 凭这个目录就能逐项对出「这次用了什么输入、依赖哪份快照、写到哪」。
            if run_dir is not None and has_manifest:
                _write_run_manifest(run_dir, manifest, job_id)
                _persist_runtime_snapshot(run_dir)
            result = _execute(tail_stop, tail)
            # 成功或用户取消：运行目录是过程产物，结论已入库，清掉；失败保留——
            # manifest/日志/结果文件是「这次为什么失败」的唯一现场，堆积由
            # prune_stale_run_dirs 的上限兜底。
            if cancelled.is_set() or result.get("ok"):
                _cleanup_run_dir(run_dir)
            else:
                from core.jvm_debug import prune_stale_run_dirs
                prune_stale_run_dirs(root=data_dir() / "app_probe" / "runs")
            return result
        finally:
            # 早退路径（如 daemon 失败不回退）也从这里停轮询；set/join 可重入，
            # 成功路径上已经停过一次
            tail_stop.set()
            tail.join(5)
            RUN_LOCK.release()

    def _execute(tail_stop: threading.Event, tail: threading.Thread) -> Dict[str, Any]:
        from backend.jobs import runner as job_runner

        execution_mode = "gradle_fallback"
        execution_note = "批量任务直接使用 Gradle"
        daemon_failure = ""
        gradle: Dict[str, Any] = {}
        daemon_response: Dict[str, Any] = {}

        # 单条校验优先复用常驻 Validate JVM。daemon 只负责执行，结果仍从同一个
        # results.jsonl 读回；准备态不完整时，daemon 失败不能偷偷再开 Gradle。
        if single and args_path is not None:
            try:
                from core import jvm_validate_daemon, jvm_direct

                job_runner.update_phase(job_id, "configuring")
                dump = jvm_direct.load_dump(warn_stale=False)
                if execution_readiness_snapshot:
                    current_exec = execution_readiness(dump)
                    if not current_exec.get("ok"):
                        raise jvm_validate_daemon.ValidateDaemonError(
                            current_exec.get("reason") or "单条 JVM 执行态在排队期间失效")
                job_runner.update_phase(job_id, "running_validate")
                daemon_response = jvm_validate_daemon.run(dump, str(args_path))
                daemon_code = daemon_response.get("code")
                if daemon_code != 0:
                    raise jvm_validate_daemon.ValidateDaemonError(
                        "daemon 返回 code=%s%s" %
                        (daemon_code,
                         ("：" + str(daemon_response.get("error"))
                          if daemon_response.get("error") else "")))
                if not out_path.exists():
                    raise jvm_validate_daemon.ValidateDaemonError(
                        "daemon 返回成功但没有产出结果文件")
                execution_mode = "validate_daemon"
                execution_note = "单条校验复用常驻 Validate JVM"
            except Exception as exc:
                daemon_failure = "常驻 Validate JVM 未完成：%s" % exc
                try:
                    out_path.unlink()
                except OSError:
                    pass

                if not allow_gradle_fallback:
                    return dict(prep, **{
                        "ok": False,
                        "exit": None,
                        "reason": (daemon_failure + "；完整 Gradle 准备态未通过，"
                                   "本次不回退 Gradle，避免再次触发冷启动/缺 SDK"),
                        "execution_mode": "validate_daemon",
                        "execution_note": "单条执行态失败，未使用 Gradle fallback",
                        "readiness": readiness_snapshot or {},
                        "execution_readiness": execution_readiness_snapshot or {},
                    })

        if execution_mode != "validate_daemon":
            job_runner.update_phase(job_id, "starting_gradle")
            gradle = _normalize_gradle_result(
                _run_gradle(args_path=args_path, runtime=runtime)
                if args_path is not None else _run_gradle(runtime=runtime))
            code = gradle.get("exit")
            execution_note = (daemon_failure + "；已使用 Gradle 完成本次校验"
                              if daemon_failure else execution_note)
        else:
            code = daemon_response.get("code")

        snapshot_reason = _runtime_snapshot_failure_reason(gradle)
        if snapshot_reason:
            return dict(prep, **{"ok": False, "exit": code,
                                "reason": snapshot_reason,
                                "execution_mode": execution_mode,
                                "execution_note": execution_note,
                                "gradle": gradle})
        if not out_path.exists():
            return dict(prep, **{"ok": False, "exit": code,
                                "reason": (_gradle_failure_reason(gradle)
                                           if gradle else
                                           "常驻 Validate JVM 没有产出结果文件"),
                                "execution_mode": execution_mode,
                                "execution_note": execution_note,
                                "gradle": gradle})
        # 执行结束：先停轮询再读结果。终值以解析出的 rows 为准——轮询数的是物理
        # 行，有残缺行被 _read_results 跳过时它比 rows 大，不能后写顶掉终值
        tail_stop.set()
        tail.join(5)
        job_runner.update_phase(job_id, "reading_results")
        rows = _read_results(out_path)
        job_runner.update_progress(job_id, len(rows))
        batch = _write_meta(rows)
        # **上一版结论的快照必须在落库之前读**：跑完再读，每条源都是 old == new，
        # 「这次变了什么」会永远答「没变」——那正是这个字段要回答的问题（同一处
        # 理由在 `ops.run_check_job` 里写着，两边必须同规矩）
        prev_checks = st.checks_map()
        # 结论同时按 checks 的口径落库（六档 / 星级 / 深度）——列表与筛选读的是
        # checks，不落这一步的话健康列会在撤掉本地引擎之后断供（TODO §一点九）。
        # 映射与判据都在 core/jvm_health，**别在这里另写一份**。
        job_runner.update_phase(job_id, "saving_results")
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
        result = dict(prep, **{
            "ok": code == 0, "exit": code, "batch": batch,
            "count": len(rows), "dist": dict(dist), "checks": n_checks,
            "execution_mode": execution_mode,
            "execution_note": execution_note,
            # 结果体与**本地校验那条同形状**（前端单条校验两条路都读它）：
            # checked / items / transitions 是回填与摘要要的，cached 这条链
            # **没有页面缓存**（每次都真抓），报 0 是实话
            "checked": len(items), "cached": 0, "fetched": len(items),
            "transitions": summarize_transitions(prev_checks, items),
            "items": items[:ITEMS_LIMIT],
        })
        if code != 0:
            result["gradle"] = gradle
        if daemon_failure:
            result["daemon_fallback_reason"] = daemon_failure
        snapshot = gradle.get("runtime_snapshot")
        if (isinstance(snapshot, dict) and
                (snapshot.get("error") or snapshot.get("differences"))):
            result["runtime_snapshot"] = snapshot
        return result

    cancelled = threading.Event()
    work = asyncio.create_task(run_in_threadpool(_work))
    try:
        return await asyncio.shield(work)
    except asyncio.CancelledError:
        # 取消 HTTP 请求不能提前释放 JVM lane；线程里的 Gradle 仍在跑，必须等它
        # 收尾后再让下一个任务进入。标记取消：运行目录照常清理（结论没入库，
        # 现场也没有复查价值——「取消不遗留」是单条快速路径验收钉过的行为）。
        cancelled.set()
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
