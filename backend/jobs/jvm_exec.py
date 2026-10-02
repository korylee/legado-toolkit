# -*- coding: utf-8 -*-
"""JVM 跑批的执行引擎（自 backend/api/jvm.py 原样搬出）。

manifest 写盘、参数文件、Gradle/daemon 调用、结果读回与落库都在这里，经
`runner.register("jvm_run")` 注册进 JVM lane；backend/api/jvm.py 只留 HTTP 编排。
**测试的 patch 面在这个模块**（_run_gradle / _write_meta / _AGSVC / data_dir /
Store / subprocess）——改函数名或搬动前，先扫 tests/test_jvm_run_*.py 与
tests/test_proxy.py 的 patch.object / 源码文本钉子。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import HTTPException
from fastapi.concurrency import run_in_threadpool

from backend.api.check_summary import (ITEMS_LIMIT, check_items_from_checks,
                                       summarize_transitions)
from backend.jobs import runner
from core import settings_store
from core.jvm_direct import execution_readiness
from core.jvm_env import process_environment
from core.loader import _normalize_url
from core.paths import ARGS_PARTS, data_dir
from core.store import Store
_ROOT = Path(__file__).resolve().parents[2]
_AGSVC = _ROOT / "appservice"

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
                        urls: List[str], params: Dict[str, Any],
                        chunks: Optional[List[int]] = None) -> Dict[str, Any]:
    """生成一次提交即固定的 JVM 任务输入快照。

    ``payload`` 会落库，但 worker 仍不应依赖多处散落字段重新推导路径和执行计划。
    manifest 的哈希只覆盖实际字段；worker 读到后先验哈希，再消费这份快照。
    ``urls`` 是归一化后的范围（AGENTS #5），``params`` 是本次生效的校验参数——
    两者都是「提交即冻结」的输入，落进运行目录的 manifest.json 后不可再改。
    ``chunks`` 是批量分块的大小序列（按 urls 顺序切分；单条=[1]），同样冻结。
    """
    manifest = {
        "schema": 3,
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
        "chunks": [int(n) for n in (chunks or [])],
    }
    encoded = json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"))
    manifest["sha256"] = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return manifest


def _job_retry_of(job_id: str) -> str:
    """读任务的 retry_of（jobs 表事实源）；manifest 信封只做记录。"""
    with Store() as st:
        row = st.get_job(job_id) or {}
    return str(row.get("retry_of") or "")


def _write_run_manifest(run_dir: Path, manifest: Dict[str, Any], job_id: str,
                        chunk: str = "") -> None:
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
        "chunk": chunk,
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
        "chunks",
    }
    missing = sorted(required - set(manifest))
    if missing or manifest.get("schema") != 3:
        detail = "缺少字段：%s" % ", ".join(missing) if missing else "schema 不是 3"
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
    st_conf = settings_store.load().get("jvm", {})
    repo = st_conf.get("app_repo", "").strip()
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


_EVENT_LOCK = threading.Lock()
_EVENT_TAIL_CAP = 500


def _append_event(run_dir: Optional[Path], kind: str, **fields: Any) -> None:
    """骨架事件追加到运行目录 ``events.jsonl``（jvm-batch-timeline）。

    **行号即游标**（消费端按行号增量读，见 backend/api/job_timeline），所以
    一行一条、坏行也占号；模块锁防块线程与任务协程交错写。
    事件是辅助证据：写不进去（OSError）不拦校验本身，也不算源失败。
    """
    if run_dir is None:
        return
    record = {"ts": round(time.time(), 3), "kind": kind, **fields}
    with _EVENT_LOCK:
        try:
            with open(run_dir / "events.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass


def _read_events_tail(run_dir: Optional[Path]) -> List[Dict[str, Any]]:
    """终态合并用：读事件尾部（cap 后仍按原始行号编 seq），进 result 长存——
    成功会清运行目录，时间线在合并后就靠 result_json。"""
    if run_dir is None:
        return []
    try:
        lines = (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    lines = [l for l in lines if l.strip()]
    out: List[Dict[str, Any]] = []
    start = max(len(lines) - _EVENT_TAIL_CAP, 0)
    for idx, line in enumerate(lines[start:], start=start + 1):
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict):
            out.append(ev)
    return out


def _write_meta(rows: List[Dict[str, Any]]) -> str:
    """结论写 meta（jvm_check:<batch>:<url>），幂等。返回 batch id。"""
    batch = time.strftime("%Y%m%d_%H%M%S")
    with Store() as st:
        for r in rows:
            url = _normalize_url(str(r.get("url", "") or ""))
            if not url:
                continue
            st.conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
                ("jvm_check:%s:%s" % (batch, url), json.dumps(r, ensure_ascii=False)))
        st.conn.commit()
    return batch


#: 进度轮询间隔（秒）。测试会把它调小来驱动轮询。
_PROGRESS_POLL_INTERVAL = 1.0

#: 块级 daemon 请求的 socket 等待上限（秒）。对齐 Gradle 路径的 90 分钟硬上限：
#: Kotlin 侧对单次 op 时长没有上限，客户端等待是唯一护栏，没有它会挂死连接
_DAEMON_SOCKET_TIMEOUT_CAP = 5400


def _tail_progress(job_id: str, out_path: Path, stop: threading.Event,
                   interval: float, cap: int = 0, base: int = 0) -> None:
    """跑批期间把 results.jsonl 已落盘的完整行数当进度上报。

    Kotlin 侧每写完一条源就 flush（`ValidateService.kt` 的 writer 回调），数 ``\\n``
    就是「实际完成几条」，不是估的——这与「不编假进度」不冲突，编的是没有依据的数。
    文件还没出现（JVM 尚未写出第一条）时保持安静，不把 0 反复写库。
    分块执行时 ``base`` 是前面各块已完成条数（跨块累计）。
    """
    while not stop.wait(interval):
        try:
            with open(out_path, "rb") as fh:
                done = base + fh.read().count(b"\n")
        except OSError:
            continue
        if cap and done > cap:
            done = cap
        if done:
            runner.update_progress(job_id, done)


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
    source_file_value = str((manifest.get("source_file") if has_manifest
                             else payload.get("source_file")) or "").strip()

    if run_dir is not None:
        # 重试的运行目录可能已被清理（成功/单条取消）：就地重建，旧块（若有）
        # 的 DONE/results 不受影响——那是重试恢复要扫的
        run_dir.mkdir(parents=True, exist_ok=True)
        if has_manifest and single and args_path is not None:
            params = (manifest.get("params") or {})
            _write_args(str(params.get("keyword") or "我"),
                        int(params.get("timeout") or 25),
                        int(params.get("concurrency") or 8), 0,
                        out_path, Path(source_file_value or (run_dir / "sources.json")),
                        str(params.get("depth") or "search"), args_path=args_path)

    cancelled = threading.Event()
    # 灰度开关（jvm-batch-daemon）：每个 job 读一次，块级线程里不再碰设置；
    # 块级只**只读探测** daemon（不杀不启），失败回落 Gradle 并带原因
    from core import settings_store
    use_daemon = settings_store.batch_via_daemon()

    def _single_work() -> Dict[str, Any]:
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
            _append_event(run_dir, "single_started")
            result = _execute_single(tail_stop, tail)
            # 终态事件在读尾合并之前落盘，才能进 result["events"]
            _append_event(run_dir, "done" if result.get("ok") else "failed",
                          reason=str(result.get("reason") or "")[:300])
            result["events"] = _read_events_tail(run_dir)
            return result
        finally:
            # 早退路径（如 daemon 失败不回退）也从这里停轮询；set/join 可重入，
            # 成功路径上已经停过一次
            tail_stop.set()
            tail.join(5)
            RUN_LOCK.release()

    def _execute_single(tail_stop: threading.Event, tail: threading.Thread) -> Dict[str, Any]:
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
        # 结论同时按 checks 的口径落库（五档 / 星级 / 深度）——列表与筛选读的是
        # checks，不落这一步的话健康列会在撤掉本地引擎之后断供。
        # 映射与判据都在 core/jvm_health，**别在这里另写一份**。
        job_runner.update_phase(job_id, "saving_results")
        from core import jvm_health
        n_checks = jvm_health.store_checks(rows, batch=batch, store=st)
        # items 从**落库后的 checks** 取，而不是自己拿 rows 再算一遍五档/星级：
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

    def _chunk_work(idx: int, total: int, chunk_dir: Optional[Path],
                    chunk_args: Optional[Path], chunk_out: Path,
                    base_done: int, batch: str,
                    tail_stop: threading.Event, tail: threading.Thread,
                    chunk_sources: int = 0,
                    daemon_allowed: bool = True,
                    daemon_reason: str = "") -> Dict[str, Any]:
        from backend.jobs import runner as job_runner

        RUN_LOCK.acquire()
        # 块墙钟（jvm-batch-daemon 批次 2 的基线数据）：没有每块耗时，
        # 「daemon 省了多少启动」就只能靠猜（lessons §四十七：性能结论必须实测）
        started = time.monotonic()
        try:
            if has_manifest and chunk_dir is not None:
                _write_run_manifest(chunk_dir, manifest, job_id,
                                    chunk="%d/%d" % (idx + 1, total))
            # 块级 daemon 路径（jvm-batch-daemon，灰度开关）：与单条同形——省掉
            # 每块一次 Gradle+JVM 冷启动。与单条的差别在探测：**只读探测，绝不
            # 杀进程**（probe 见 core.jvm_validate_daemon）——探测方不持有互斥，
            # daemon 忙着别人的请求时 ping 不通不等于死，杀了会伤及在跑的 op；
            # 探测不过/执行失败一律回落 Gradle，并把原因带到块报告里（AGENTS #4：
            # 回落要说出来，不能装成源失败）。
            daemon_failure = (daemon_reason if use_daemon and not daemon_allowed else "")
            daemon_response: Dict[str, Any] = {}
            gradle: Dict[str, Any] = {}
            daemon_mode = False
            if daemon_allowed and use_daemon and chunk_args is not None:
                try:
                    from core import jvm_validate_daemon, jvm_direct

                    dump = jvm_direct.load_dump(warn_stale=False)
                    info = jvm_validate_daemon.probe(dump)
                    if info is None:
                        raise jvm_validate_daemon.ValidateDaemonError(
                            "daemon 探测未通过（未启动/忙/版本不符）")
                    job_runner.update_phase(job_id, "running_validate")
                    # socket 等待 = 每源预算 × 块源数 + 余量：Kotlin 侧对单次 op
                    # 的时长没有上限，这个客户端等待是唯一护栏；硬上限对齐 Gradle
                    # 路径的 90 分钟。超时抛 ValidateDaemonError → 回落，不是源失败
                    per_source = int(jvm_validate_daemon.params_from_args(
                        chunk_args)["timeout"])
                    wait = min(per_source * max(chunk_sources, 1) + 60,
                               _DAEMON_SOCKET_TIMEOUT_CAP)
                    daemon_response = jvm_validate_daemon.run(
                        dump, str(chunk_args), socket_timeout=wait)
                    daemon_code = daemon_response.get("code")
                    if daemon_code != 0:
                        raise jvm_validate_daemon.ValidateDaemonError(
                            "daemon 返回 code=%s%s" %
                            (daemon_code,
                             ("：" + str(daemon_response.get("error"))
                              if daemon_response.get("error") else "")))
                    if not chunk_out.exists():
                        raise jvm_validate_daemon.ValidateDaemonError(
                            "daemon 返回成功但没有产出结果文件")
                    daemon_mode = True
                except Exception as exc:
                    daemon_failure = "常驻 Validate JVM 未完成：%s" % exc
                    # 半成品结果不能留给读结果阶段当真结论
                    try:
                        chunk_out.unlink()
                    except OSError:
                        pass
            if daemon_mode:
                code = daemon_response.get("code")
            else:
                job_runner.update_phase(job_id, "starting_gradle")
                gradle = _normalize_gradle_result(
                    _run_gradle(args_path=chunk_args, runtime=runtime))
                code = gradle.get("exit")
            snapshot_reason = _runtime_snapshot_failure_reason(gradle)
            if snapshot_reason:
                return {"index": idx, "ok": False, "exit": code,
                        "reason": snapshot_reason, "gradle": gradle,
                        "execution_mode": ("validate_daemon" if daemon_mode
                                           else "gradle"),
                        "daemon_failure": daemon_failure,
                        "cost_sec": round(time.monotonic() - started, 1)}
            if code != 0 or not chunk_out.exists():
                return {"index": idx, "ok": False, "exit": code,
                        "reason": (_gradle_failure_reason(gradle)
                                   if gradle and not daemon_mode
                                   else "没有产出结果文件"),
                        "gradle": gradle,
                        "execution_mode": ("validate_daemon" if daemon_mode
                                           else "gradle"),
                        "daemon_failure": daemon_failure,
                        "cost_sec": round(time.monotonic() - started, 1)}
            # 读结果前先停本块轮询：终值以解析出的 rows 为准（理由同单条）
            tail_stop.set()
            tail.join(5)
            job_runner.update_phase(job_id, "reading_results")
            rows = _read_results(chunk_out)
            job_runner.update_progress(job_id, base_done + len(rows))
            job_runner.update_phase(job_id, "saving_results")
            from core import jvm_health
            jvm_health.store_checks(rows, batch=batch, store=st)
            # 块完成标记（文件即状态）：重试恢复据此跳过已完成块
            if chunk_dir is not None:
                (chunk_dir / "DONE").write_text("", encoding="utf-8")
            return {"index": idx, "ok": True, "count": len(rows),
                    "execution_mode": ("validate_daemon" if daemon_mode
                                       else "gradle"),
                    "daemon_failure": daemon_failure,
                    "cost_sec": round(time.monotonic() - started, 1)}
        finally:
            tail_stop.set()
            tail.join(5)
            RUN_LOCK.release()

    def _chunk_completed(chunk_dir: Path) -> bool:
        """块完成判据（文件即状态）：DONE 标记在，且 results.jsonl 可整读。"""
        if not (chunk_dir / "DONE").is_file():
            return False
        try:
            _read_results(chunk_dir / "results.jsonl")
            return True
        except (OSError, ValueError):
            return False

    def _prepare_daemon() -> Dict[str, Any]:
        """批量开跑前的 daemon 准备（jvm-batch-daemon-cold-start，每批至多一次）。

        分类与「不误杀」边界都在 core.jvm_validate_daemon.prepare 里收口；
        这里只兜底：准备动作炸了也不能带走整批，转成 failed 带原因，
        块级按现状回落 Gradle。
        """
        try:
            from core import jvm_direct, jvm_validate_daemon

            return jvm_validate_daemon.prepare(
                jvm_direct.load_dump(warn_stale=False))
        except Exception as exc:
            return {"outcome": "failed", "reason": str(exc)}

    async def _run_batch() -> Dict[str, Any]:
        from backend.jobs import runner as job_runner

        rows_all = (json.loads(Path(source_file_value).read_text(encoding="utf-8"))
                    if source_file_value else [])
        sizes = [int(n) for n in (manifest.get("chunks") or [])] if has_manifest else []
        if not sizes or sum(sizes) != len(rows_all):
            sizes = [len(rows_all)]      # legacy / 清单不可信：整批一块，行为同旧版
        chunks: List[List[dict]] = []
        pos = 0
        for n in sizes:
            chunks.append(rows_all[pos:pos + n])
            pos += n

        if run_dir is not None and has_manifest:
            _write_run_manifest(run_dir, manifest, job_id)
            _persist_runtime_snapshot(run_dir)
        batch_t0 = time.monotonic()
        _append_event(run_dir, "batch_started", chunks=len(chunks),
                      sources=len(rows_all))
        job_runner.update_phase(job_id, "starting_gradle")
        # **上一版结论的快照必须在落库之前读**：跑完再读，每条源都是 old == new，
        # 「这次变了什么」会永远答「没变」（与单条同规矩，理由见 ops.run_check_job）
        prev_checks = st.checks_map()
        batch = _write_meta([])
        chunk_reports: List[Dict[str, Any]] = []
        all_rows: List[dict] = []
        base_done = 0
        abort_reason = ""
        daemon_prepare: Dict[str, Any] = {}
        daemon_retry_waiting = False
        daemon_retry_available = False
        daemon_recovered = False
        daemon_retry_reason = ""
        for idx, chunk_rows in enumerate(chunks):
            if idx > 0:
                # 块间交还 lane：排队者（调试等）按优先级插队，本批随后重新取许可。
                # 这是「批量不饿死调试、调试不饿死批量」的机制本体。
                lane.release()
                await lane.acquire("batch", job_id)
            if cancelled.is_set():
                abort_reason = abort_reason or "用户取消，剩余块未启动"
                break
            chunk_dir = (run_dir / ("chunk-%02d" % (idx + 1))
                         if (run_dir is not None and has_manifest) else None)
            if chunk_dir is not None and _chunk_completed(chunk_dir):
                # 重试恢复：DONE 标记在 → 该块结论已在库，只汇总不重跑
                rows = _read_results(chunk_dir / "results.jsonl")
                all_rows.extend(rows)
                base_done += len(rows)
                job_runner.update_progress(job_id, base_done)
                _append_event(run_dir, "resumed", index=idx, count=len(rows))
                chunk_reports.append({"index": idx, "ok": True,
                                      "count": len(rows), "resumed": True})
                continue
            if chunk_dir is not None:
                chunk_dir.mkdir(parents=True, exist_ok=True)
                chunk_src = chunk_dir / "sources.json"
                chunk_out = chunk_dir / "results.jsonl"
                chunk_args = chunk_dir / "args.properties"
                chunk_src.write_text(json.dumps(chunk_rows, ensure_ascii=False),
                                     encoding="utf-8", newline="\n")
                params = (manifest.get("params") or {})
                _write_args(str(params.get("keyword") or "我"),
                            int(params.get("timeout") or 25),
                            int(params.get("concurrency") or 8), 0,
                            chunk_out, chunk_src,
                            str(params.get("depth") or "search"),
                            args_path=chunk_args)
            else:
                chunk_out, chunk_args = out_path, args_path
            # 冷启动准备（jvm-batch-daemon-cold-start）：每批至多一次，放在第一个
            # 待跑块前——全部块 DONE 的重试批一次都不准备。块级仍只读探测（上面
            # _chunk_work 里的 probe 原样保留，是 daemon 中途死掉/变忙的安全网）；
            # 「不误杀」的边界在 core.prepare 收口：确认没在跑才允许 ensure/start，
            # 忙（端口有人听、ping 没应答）一律不杀。
            if use_daemon and chunk_args is not None and not daemon_prepare:
                job_runner.update_phase(job_id, "starting_daemon")
                _prepare_t0 = time.monotonic()
                daemon_prepare = await run_in_threadpool(_prepare_daemon)
                _append_event(run_dir, "prepare",
                              outcome=str(daemon_prepare.get("outcome") or ""),
                              reason=str(daemon_prepare.get("reason") or ""),
                              cost_sec=round(time.monotonic() - _prepare_t0, 1))
                if daemon_prepare.get("outcome") in ("busy", "failed"):
                    daemon_retry_waiting = True
                    daemon_retry_reason = str(daemon_prepare.get("reason") or
                                              "daemon 批次准备未就绪")
            tail_stop = threading.Event()
            tail = threading.Thread(
                target=_tail_progress, name="jvm-progress-tail",
                args=(job_id, chunk_out, tail_stop, _PROGRESS_POLL_INTERVAL,
                      len(chunk_rows), base_done), daemon=True)
            tail.start()
            retry_this_chunk = daemon_retry_available
            daemon_allowed = (use_daemon and
                              (daemon_recovered or
                               daemon_prepare.get("outcome") in ("ready", "started") or
                               retry_this_chunk))
            daemon_reason = (daemon_retry_reason
                             if use_daemon and not daemon_allowed else "")
            if retry_this_chunk:
                daemon_retry_available = False
            _append_event(run_dir, "chunk_started", index=idx,
                          total=len(chunks), count=len(chunk_rows))
            work = asyncio.create_task(run_in_threadpool(
                _chunk_work, idx, len(chunks), chunk_dir, chunk_args,
                chunk_out, base_done, batch, tail_stop, tail,
                len(chunk_rows), daemon_allowed, daemon_reason))
            try:
                report = await asyncio.shield(work)
            except asyncio.CancelledError:
                # 取消不能绕过仍在跑的块线程：等它收尾再传播（与单条同规矩）
                cancelled.set()
                report = await asyncio.shield(work)
                chunk_reports.append(report)
                abort_reason = "用户取消，剩余块未启动"
                break
            chunk_reports.append(report)
            _append_event(run_dir,
                          "chunk_done" if report.get("ok") else "chunk_failed",
                          index=idx, mode=str(report.get("execution_mode") or ""),
                          count=int(report.get("count") or 0),
                          cost_sec=report.get("cost_sec"),
                          reason=(str(report.get("reason") or "")[:300]
                                  if not report.get("ok") else ""))
            if daemon_retry_waiting:
                # 忙/失败后只在下一块重试一次，**有意不逐块重试**：每次 probe 都要
                # 付满 1 秒超时，忙着的 daemon 不会因为多探几次就空闲
                daemon_retry_waiting = False
                daemon_retry_available = True
            if retry_this_chunk and report.get("execution_mode") == "validate_daemon":
                daemon_recovered = True
                _append_event(run_dir, "recovered", index=idx)
            if cancelled.is_set():
                abort_reason = abort_reason or "用户取消，剩余块未启动"
                break
            if not report.get("ok"):
                # 环境级失败（Gradle/快照漂移）：余下块不再启动——不是源的问题，
                # 别把它们也记成失败；已完成块的结论已在库
                abort_reason = ("第 %d/%d 块失败：%s"
                                % (idx + 1, len(chunks), report.get("reason") or ""))
                break
            all_rows.extend(_read_results(chunk_out))
            base_done += int(report.get("count") or 0)

        if cancelled.is_set():
            # 批量取消**保留**运行目录：已完成块的 DONE/results 是重试恢复的依据
            _append_event(run_dir, "cancelled")
            from core.jvm_debug import prune_stale_run_dirs
            prune_stale_run_dirs(root=data_dir() / "app_probe" / "runs")
            raise asyncio.CancelledError()

        # 终态事件必须在读尾合并**之前**落盘，才能进 result["events"]
        if abort_reason:
            _append_event(run_dir, "failed", reason=abort_reason)
        else:
            _append_event(run_dir, "done", count=len(all_rows),
                          cost_sec=round(time.monotonic() - batch_t0, 1))

        names = {_normalize_url(str(r.get("url") or "")): str(r.get("name") or "")
                 for r in all_rows}
        fresh = st.checks_map()
        items = check_items_from_checks(
            {u: fresh[u] for u in names if u in fresh}, names=names)
        dist = Counter(r.get("state") for r in all_rows)
        # 块级执行方式统计（jvm-batch-daemon 灰度观察用）。汇总的 execution_mode
        # 只有在**全部**块都走了 daemon 时才改标——混合时保留历史值，差额写进
        # execution_note（前端词表不扩，信息也不静默）
        daemon_blocks = sum(1 for r in chunk_reports
                            if r.get("execution_mode") == "validate_daemon")
        gradle_blocks = sum(1 for r in chunk_reports
                            if r.get("execution_mode") == "gradle")
        all_daemon = daemon_blocks > 0 and gradle_blocks == 0
        result = dict(prep, **{
            "ok": not abort_reason, "exit": None if abort_reason else 0,
            "batch": batch, "count": len(all_rows), "dist": dict(dist),
            "checks": sum(r.get("count", 0) for r in chunk_reports if r.get("ok")),
            "execution_mode": "validate_daemon" if all_daemon else "gradle_fallback",
            "execution_note": ("批量按块执行：%d 块走常驻 daemon、%d 块走 Gradle"
                               % (daemon_blocks, gradle_blocks)
                               if (daemon_blocks or gradle_blocks)
                               else "批量按块执行 Gradle，块间交还调度权"),
            "checked": len(items), "cached": 0, "fetched": len(items),
            "transitions": summarize_transitions(prev_checks, items),
            "items": items[:ITEMS_LIMIT],
            "chunk_reports": chunk_reports,
            "daemon_chunks": daemon_blocks, "gradle_chunks": gradle_blocks,
        })
        if daemon_prepare:
            # 只投影有界两键：pid/port 这类易变值不进结果（呈现归
            # jvm-batch-observability）；开关关 / 全部块恢复时本键缺席
            result["daemon_prepare"] = {
                "outcome": str(daemon_prepare.get("outcome") or "failed"),
                "reason": str(daemon_prepare.get("reason") or ""),
            }
        result["events"] = _read_events_tail(run_dir)
        if abort_reason:
            result["reason"] = abort_reason
        # 成功清现场；失败/取消保留——已完成块的 DONE/results 是重试恢复的依据
        if result["ok"]:
            _cleanup_run_dir(run_dir)
        else:
            from core.jvm_debug import prune_stale_run_dirs
            prune_stale_run_dirs(root=data_dir() / "app_probe" / "runs")
        return result

    # jvm_run 自管 lane（runner._run 不再代持）：批量按块交还许可重排队
    lane = runner.lane("jvm")
    await lane.acquire("batch", job_id)
    try:
        if single:
            work = asyncio.create_task(run_in_threadpool(_single_work))
            try:
                result = await asyncio.shield(work)
            except asyncio.CancelledError:
                # 单条取消沿用「不遗留」：结论没入库，现场没有复查价值
                cancelled.set()
                await asyncio.shield(work)
                _cleanup_run_dir(run_dir)
                raise
            if result.get("ok"):
                _cleanup_run_dir(run_dir)
            else:
                from core.jvm_debug import prune_stale_run_dirs
                prune_stale_run_dirs(root=data_dir() / "app_probe" / "runs")
            return result
        try:
            return await _run_batch()
        except asyncio.CancelledError:
            # 批量取消**保留**运行目录：已完成块的 DONE/results 是重试恢复的依据
            cancelled.set()
            from core.jvm_debug import prune_stale_run_dirs
            prune_stale_run_dirs(root=data_dir() / "app_probe" / "runs")
            raise
    finally:
        lane.release()
