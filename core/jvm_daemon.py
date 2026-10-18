# -*- coding: utf-8 -*-
"""常驻引擎 daemon 的客户端（**唯一一份**，2026-10-18 起调试与校验共用）。

**它解决什么**：单次调试/校验的**入场费**（JVM + Robolectric + App 初始化）比站点请求
贵一个量级，常驻把「起一次」摊到多次请求上。
**实测数字别在这里抄**（会过期）：见 lessons §六十五 / §六十六 / §六十七。

## 与一次性那条**同形**（关键设计，别改成另一套）

daemon 只换「怎么拉起」：请求走一行 JSON 过 socket（`op=debug` / `op=validate`，见
`appservice` 的 `AppServiceDaemon`），产物（NDJSON + 侧车 / results.jsonl）**还是落到
原来的路径**，于是 `core.jvm_debug.run_jvm_debug` 与批量那条链的解析一行不改——它靠
`launcher=` 缝接进来。两条拉起方式（直起 / 常驻）的差别**只有拉起**。

## 生命周期语义（2026-10-18 合并时取 validate 侧的强语义为准）

- **probe 只读**：绝不杀进程、绝不重启——ping 不通可能只是 daemon 正串行忙别人的
  op（对已关闭端口的 connect 抛的是超时不是拒绝连接，端口行为分不出死活；
  判「死」的唯一依据是 info 里的 pid 已不在，`_pid_alive`）。
- **prepare 先分类再行动**：ready / started / busy / failed 四档，永不抛；busy 是
  「进程活着但没应答」，有界等待，到点不杀不启，原因留给调用方。
- **resolve 抛异常版**：单条路径拿不到可用 daemon 就带原因抛，进程一个都不动。
- **stop_idle 保守停**：身份核验（ping + 进程实例身份）+ PID 复用防护 + 优雅退出
  确认，绝不强杀。
- **降级路径（必须有）**：`launcher_from_args()` 的每一次调用都是「先试 daemon，
  任何失败就回落」——用户看到的顶多是「这次慢一点」，而不是「调试坏了」。

## 版本键

`source_sig()` = appservice 的 Kotlin 源码指纹 + runtime snapshot 内容指纹。
**改了 Kotlin 或运行环境不重启进程，进程里跑的还是旧类/旧环境**，而那看起来像
「规则改了没生效」——所以 sig 变了就换进程。daemon 自己不判版本（它不知道磁盘上
现在是什么），只在 `op=ping` 时把起它时收到的 sig 原样回吐，由客户端比。
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import signal
import socket
import subprocess
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
from uuid import uuid4

from core import jvm_direct
from core.paths import data_path

DAEMON_CLASS = "io.legado.app.service.AppServiceDaemonLauncher"
INFO_NAME = "jvm_daemon.json"
LOG_NAME = "jvm_daemon.log"

#: 默认空闲退出（秒）。**真正的计时在 daemon 里**，这里只是传给它；
#: 改值要两边一起想（Kotlin 侧 `AppServiceDaemon.DEFAULT_IDLE_SEC`）。
DEFAULT_IDLE_SEC = 1800
#: 首次拉起要等 Robolectric + App 起来（首次编译/冷缓存会更久）
DEFAULT_BOOT_TIMEOUT = 180

#: 忙等待期间每隔这么久回调一次 ``on_wait``（让调用方把等待写进时间线）。
#: 短等待（≤ 这个值）不上报，避免刷屏。
_BUSY_NOTICE_INTERVAL = 10.0

#: 守护启动/停止的互斥锁。**必须是可重入锁**：``ensure`` 内部会调 ``start``，
#: 两处都要持锁。没有它，两个调用方同时判定「没在跑」就会各起一个 daemon。
_LOCK = threading.RLock()
_PROC: Optional[subprocess.Popen] = None
_LOG = None


class DaemonError(RuntimeError):
    """daemon 不可用（起不来 / 不认路 / 半路死）。**调用方应回落，不该把它当结论**。"""


def info_path() -> pathlib.Path:
    return pathlib.Path(data_path("app_probe", INFO_NAME))


def log_path() -> pathlib.Path:
    return pathlib.Path(data_path("app_probe", LOG_NAME))


def current_operation_path() -> pathlib.Path:
    return pathlib.Path(data_path("app_probe", "daemon.current.json"))


def _runtime_sig() -> str:
    path = jvm_direct.dump_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        canonical = json.dumps(raw, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"))
    except (OSError, ValueError, TypeError):
        try:
            canonical = path.read_bytes().decode("utf-8", errors="replace")
        except OSError:
            canonical = ""
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def source_sig(dump: Optional[Dict[str, Any]] = None) -> str:
    """版本键：Kotlin 源码（数量+mtime+总大小）+ runtime snapshot 内容指纹。

    **不是密码学哈希**，职责是「改了代码或执行环境别用旧进程」。snapshot 使用稳定内容
    指纹而不是文件 mtime；Gradle 重写同一份环境描述不应无故让常驻 daemon 失效。
    **刻意不缓存**：源码与 snapshot 每次重新读取，必须逐次反映真实执行环境；加缓存省下
    的时间抵不上多出来的失效条件。"""
    files = list((jvm_direct.AGSVC / "test").rglob("*.kt"))
    mt = max((f.stat().st_mtime for f in files), default=0.0)
    size = sum(f.stat().st_size for f in files)
    runtime = _runtime_sig()

    return "kt%d-%d-%d-runtime%s" % (len(files), int(mt), size, runtime)


# ---------------------------------------------------------------- 进程身份


def process_created(pid: int) -> str:
    """返回 OS 可验证的进程创建时间，防止 PID 复用被当成旧 owner。"""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        handle = kernel32.OpenProcess(0x1000, False, int(pid))
        if not handle:
            raise OSError(ctypes.get_last_error(), "OpenProcess failed")
        try:
            created = wintypes.FILETIME()
            exited = wintypes.FILETIME()
            kernel = wintypes.FILETIME()
            user = wintypes.FILETIME()
            if not kernel32.GetProcessTimes(handle, ctypes.byref(created),
                                            ctypes.byref(exited),
                                            ctypes.byref(kernel),
                                            ctypes.byref(user)):
                raise OSError(ctypes.get_last_error(), "GetProcessTimes failed")
            return "%08x%08x" % (created.dwHighDateTime, created.dwLowDateTime)
        finally:
            kernel32.CloseHandle(handle)
    stat_path = "/proc/%d/stat" % int(pid)
    with open(stat_path, encoding="ascii") as stream:
        fields = stream.read().rsplit(")", 1)[1].split()
    try:
        return fields[19]
    except IndexError as exc:
        raise OSError("process stat 没有 starttime") from exc


def _pid_alive(pid: int) -> bool:
    """进程还活着吗。判「死」用 pid，不用端口行为——实测（Windows，2026-10-02）
    对已关闭端口的 connect 抛的是超时而不是拒绝连接，端口行为分不出死活。
    Windows 走 OpenProcess（只查状态、不杀）；POSIX 走 ``kill(pid, 0)``。
    """
    pid = int(pid)
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:  # PROCESS_QUERY_LIMITED_INFORMATION
            return False
        try:
            exit_code = wintypes.DWORD()
            ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            return bool(ok and exit_code.value == 259)  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 活着，只是没权限发信号
    return True


# ---------------------------------------------------------------- 进程间协议

def _read_line(sock: socket.socket) -> str:
    buf = bytearray()
    while True:
        ch = sock.recv(1)
        if not ch:
            break
        if ch == b"\n":
            break
        buf += ch
    return buf.decode("utf-8", errors="replace")


def _exchange(payload: Dict[str, Any], port: int, timeout: float) -> Dict[str, Any]:
    """一行请求 → 一行应答。任何 IO 异常都翻成 `DaemonError`（调用方据此回落）。"""
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=min(timeout, 10)) as s:
            s.settimeout(timeout)
            s.sendall((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
            line = _read_line(s)
    except OSError as e:
        raise DaemonError("连不上 daemon(port=%s): %s" % (port, e)) from e
    if not line.strip():
        raise DaemonError("daemon 没回应答（连接被关）")
    try:
        return json.loads(line)
    except Exception as e:
        raise DaemonError("daemon 的应答不是 JSON: %r" % line[:200]) from e


def ping(port: int, timeout: float = 5.0) -> Optional[Dict[str, Any]]:
    """探活。**不是我们的 daemon 就返回 None**（端口可能被别的程序占着）。"""
    try:
        r = _exchange({"id": 0, "op": "ping"}, port, timeout)
    except DaemonError:
        return None
    return r if "sig" in r and r.get("kind") == "engine" else None


def request(cfg: Dict[str, Any], port: int, timeout: float) -> Dict[str, Any]:
    """跑一次调试（op=debug；参数与 `DebugService.Config` 一一对应）。"""
    payload = {
        "id": int(time.time() * 1000) % 1_000_000,
        "op": "debug",
        "file": str(cfg.get("file") or ""),
        "key": str(cfg.get("key") or ""),
        "out": str(cfg.get("out") or ""),
        "timeout": int(cfg.get("timeout") or 60),
        "cookie": str(cfg.get("cookie") or ""),
    }
    r = _exchange(payload, port, timeout)
    if "code" not in r:
        raise DaemonError("daemon 的应答缺 code: %r" % r)
    return r


def params_from_args(args_file: Optional[str] = None) -> Dict[str, Any]:
    """把 `appservice/args.properties` 读成**调试**请求参数（op=debug 的形状）。

    **故意读那个文件**：一次性那条链的契约就是「参数在 args.properties 里，拉起方自己
    去读」（Kotlin 侧 `AppserviceEnv.loadArgs` 是同一个约定）。常驻这边读同一份，
    `run_jvm_debug` 写一次参数、两条拉起方式都认得，不会出现「命令行跑了另一条源」。
    """
    from core.jvm_debug import ARGS
    props: Dict[str, str] = {}
    p = pathlib.Path(str(args_file or ARGS))
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        props[k.strip()] = v.strip()
    return {"file": props.get("file", ""), "key": props.get("key", ""),
            "out": props.get("out", ""),
            "timeout": int(props.get("timeout") or 60), "cookie": props.get("cookie", "")}


def validate_params_from_args(args_file: Optional[str] = None) -> Dict[str, Any]:
    """把 `appservice/args.properties` 读成**校验**请求参数（op=validate 的形状）。"""
    from core.jvm_debug import ARGS

    path = pathlib.Path(str(args_file or ARGS))
    props: Dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        props[key.strip()] = value.strip()
    result: Dict[str, Any] = {
        "file": props.get("file", ""),
        "dir": props.get("dir", ""),
        "keyword": props["keyword"],
        "out": props.get("out", ""),
        "concurrency": int(props["concurrency"]),
        "timeout": int(props["timeout"]),
        "limit": int(props.get("limit") or 0),
        "depth": props["depth"],
        "cookie": props.get("cookie", ""),
        "no_strip_webview": props.get("noStripWebview") == "1",
    }
    if props.get("profile"):
        result["profile"] = props["profile"]
    return result


def _validate_request(cfg: Dict[str, Any], port: int, timeout: float) -> Dict[str, Any]:
    """向 daemon 发一次 op=validate（参数与 `ValidateService.main` 的 CLI 一一对应）。"""
    payload = {
        "id": int(time.time() * 1000) % 1_000_000,
        "op": "validate",
        "file": str(cfg.get("file") or ""),
        "dir": str(cfg.get("dir") or ""),
        "keyword": str(cfg["keyword"]),
        "out": str(cfg.get("out") or ""),
        "concurrency": int(cfg["concurrency"]),
        "timeout": int(cfg["timeout"]),
        "limit": int(cfg.get("limit") or 0),
        "depth": str(cfg["depth"]),
        "cookie": str(cfg.get("cookie") or ""),
        "no_strip_webview": bool(cfg.get("no_strip_webview")),
        "owner_job_id": str(cfg.get("owner_job_id") or ""),
        "owner_chunk": str(cfg.get("owner_chunk") or ""),
        "owner_source_name": str(cfg.get("owner_source_name") or ""),
    }
    if cfg.get("profile"):
        payload["profile"] = str(cfg["profile"])
    try:
        result = _exchange(payload, port, timeout)
    except DaemonError as exc:
        context = _request_failure_context(port)
        raise DaemonError("%s；诊断：%s" % (exc, context)) from exc
    if "code" not in result:
        raise DaemonError("daemon 的应答缺 code: %r" % result)
    return result


# ---------------------------------------------------------------- owner marker

def _read_current_operation() -> Dict[str, Any]:
    try:
        data = json.loads(current_operation_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _begin_current_operation(owner: Optional[Dict[str, Any]]) -> str:
    operation_id = uuid4().hex
    data = {str(k): v for k, v in (owner or {}).items()
            if k in {"job_id", "chunk", "source_count", "source_name"}}
    if "source_name" in data:
        data["source_name"] = str(data["source_name"]).replace("\r", " ").replace("\n", " ")[:120]
    try:
        process_identity = process_created(os.getpid())
    except (OSError, ValueError, IndexError) as exc:
        raise DaemonError("无法记录 operation owner 的进程实例身份：%s" % exc) from exc
    data.update({"operation_id": operation_id, "pid": os.getpid(),
                 "process_created": process_identity,
                 "started_at": time.time()})
    path = current_operation_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                    encoding="utf-8", newline="\n")
    return operation_id


def _end_current_operation(operation_id: str) -> None:
    path = current_operation_path()
    current = _read_current_operation()
    if current.get("operation_id") != operation_id:
        return
    try:
        path.unlink()
    except OSError:
        pass


# ---------------------------------------------------------------- 生命周期

def _read_info() -> Dict[str, Any]:
    p = info_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_info(info: Dict[str, Any]) -> None:
    p = info_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(info, ensure_ascii=False, indent=1),
                 encoding="utf-8", newline="\n")


def _log_tail(lines: int = 12) -> str:
    p = log_path()
    if not p.exists():
        return ""
    txt = p.read_text(encoding="utf-8", errors="replace").strip().splitlines()
    return " / ".join(txt[-lines:])


def _request_failure_context(port: int) -> str:
    """请求失败时的诊断上下文：pid/端口/死活、owner、日志尾巴。"""
    info = _read_info()
    try:
        pid = int(info.get("pid") or 0)
    except (TypeError, ValueError):
        pid = 0
    try:
        alive = _pid_alive(pid) if pid else False
    except OSError:
        alive = "unknown"
    parts = ["daemon pid=%s port=%s alive=%s" %
             (pid or "unknown", info.get("port") or port, alive)]
    owner = _read_current_operation()
    if owner:
        parts.append("owner job=%s chunk=%s pid=%s" % (
            owner.get("job_id") or "?", owner.get("chunk") or "?",
            owner.get("pid") or "?"))
    process = _PROC
    if process is not None and process.pid == pid:
        try:
            exit_code = process.poll()
        except OSError:
            exit_code = None
        if exit_code is not None:
            parts.append("daemon exit_code=%s" % exit_code)
    tail = _log_tail(8)
    if tail:
        parts.append("daemon log tail=" + tail[-1000:])
    return "; ".join(parts)


def pick_port() -> int:
    """让系统给一个空闲端口。**有极小的竞态窗口**（关掉到 daemon 绑定之间），
    真撞上就是启动失败 → 回落直起，不会错结论。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def is_running() -> bool:
    """info 记录的那个 daemon 还活着吗（**从端口问身份**，不去杀 pid）。"""
    info = _read_info()
    got = ping(int(info.get("port") or 0), timeout=1.0) if info.get("port") else None
    return bool(got and got.get("pid") == info.get("pid"))


def status() -> Dict[str, Any]:
    """daemon 的完整状态（后端 daemon 管理面与 CLI 共用）。"""
    info = _read_info()
    if not isinstance(info, dict):
        info = {}
    try:
        pid = int(info.get("pid") or 0)
        port = int(info.get("port") or 0)
    except (TypeError, ValueError):
        pid, port = 0, 0
    alive = _pid_alive(pid) if pid else False
    got = ping(port, timeout=1.0) if alive and port else None
    try:
        got_pid = int((got or {}).get("pid") or 0)
    except (TypeError, ValueError):
        got_pid = 0
    identity = bool(got and got.get("kind") == "engine" and got_pid == pid)
    try:
        sig_matches = bool(identity and got.get("sig") == source_sig())
    except (OSError, ValueError):
        sig_matches = False
    owner = _read_current_operation()
    try:
        owner_pid = int(owner.get("pid") or 0)
    except (TypeError, ValueError):
        owner_pid = 0
    owner_alive = _pid_alive(owner_pid) if owner_pid else None
    owner_identity_matches = False
    owner_instance_matches = False
    if owner_alive:
        try:
            owner_instance_matches = (
                bool(owner.get("process_created"))
                and process_created(owner_pid) == owner.get("process_created"))
            owner_identity_matches = bool(owner_instance_matches)
        except (OSError, ValueError, IndexError):
            owner_instance_matches = False
            owner_identity_matches = False
    try:
        owner_age = (max(0.0, time.time() - float(owner.get("started_at") or time.time()))
                     if owner else None)
    except (TypeError, ValueError):
        owner_age = None
    return {
        "state": ("missing" if not info else
                  "stopped" if not alive else
                  "idle" if identity else "unresponsive"),
        "pid": pid or None,
        "port": port or None,
        "started_at": info.get("started_at"),
        "process_alive": alive,
        "responsive": bool(got),
        "identity_verified": identity,
        "signature_matches": sig_matches,
        "current_operation": owner or None,
        "owner_process_alive": owner_alive if owner else None,
        "owner_identity_verified": bool(owner_identity_matches),
        "owner_instance_matches": bool(owner_instance_matches),
        "owner_instance_exited": bool(owner.get("process_created") and not owner_alive),
        "owner_age_seconds": round(owner_age, 1) if owner_age is not None else None,
        "log": str(log_path()),
    }


def start(dump: Dict[str, Any], idle_sec: int = DEFAULT_IDLE_SEC,
          boot_timeout: int = DEFAULT_BOOT_TIMEOUT) -> Dict[str, Any]:
    """拉起一个 daemon 并等它就绪。失败抛 `DaemonError`（**带日志尾巴**，别让人对着空手猜）。"""
    with _LOCK:
        return _start_locked(dump, idle_sec=idle_sec, boot_timeout=boot_timeout)


def _start_locked(dump: Dict[str, Any], idle_sec: int = DEFAULT_IDLE_SEC,
                  boot_timeout: int = DEFAULT_BOOT_TIMEOUT) -> Dict[str, Any]:
    global _PROC, _LOG
    jvm_direct.validate_dump(dump)
    port = pick_port()
    sig = source_sig(dump)
    cmd = jvm_direct.java_command(dump, DAEMON_CLASS)
    env = {**jvm_direct.java_env(dump),
           "LEGADO_DAEMON_PORT": str(port),
           "LEGADO_DAEMON_IDLE_SEC": str(int(idle_sec)),
           "LEGADO_DAEMON_SIG": sig,
           "LEGADO_TEST_JVM_ENV_OUT": str(jvm_direct.dump_path()),
           "LEGADO_TEST_JVM_LAUNCH_MODE": "daemon"}
    log_path().parent.mkdir(parents=True, exist_ok=True)
    _LOG = open(log_path(), "a", encoding="utf-8")
    _LOG.write("\n==== %s 起 daemon：port=%s sig=%s\n" % (time.strftime("%F %T"), port, sig))
    _LOG.flush()
    _PROC = subprocess.Popen(cmd, cwd=dump["workingDir"], env=env,
                             stdout=_LOG, stderr=subprocess.STDOUT)
    deadline = time.time() + max(5, boot_timeout)
    while time.time() < deadline:
        if _PROC.poll() is not None:
            raise DaemonError("daemon 启动即退出（码 %s）：%s" % (_PROC.returncode, _log_tail()))
        got = ping(port, timeout=0.3)
        if got and got.get("sig") == sig:
            info = {"pid": _PROC.pid, "port": port, "sig": sig, "idle_sec": int(idle_sec),
                    "started_at": time.time()}
            _write_info(info)
            from core.jvm_runtime_snapshot import compare_runtime_snapshot
            compare_runtime_snapshot("daemon", "daemon")
            return info
        time.sleep(0.2)
    _kill_proc()
    raise DaemonError("daemon %.0fs 内没起来（看 %s）" % (boot_timeout, log_path()))


def ensure(dump: Dict[str, Any], idle_sec: int = DEFAULT_IDLE_SEC,
           boot_timeout: int = DEFAULT_BOOT_TIMEOUT) -> Dict[str, Any]:
    """拿到一个**可用**的 daemon：活着、身份对且版本对就复用，否则（重）起一个。

    身份从**端口问**（ping 回的 pid 与记录一致），不从进程外杀 pid——pid 会被复用。
    """
    jvm_direct.validate_dump(dump)
    with _LOCK:
        info = _read_info()
        if info.get("port"):
            got = ping(int(info["port"]), timeout=1.0)
            if got and got.get("pid") == info.get("pid"):
                if info.get("sig") == source_sig(dump):
                    return info
                # 版本对不上：进程里是旧类。**显式收掉**再起（不靠 pid kill 的运气）
                _stop_port(int(info["port"]), int(info.get("pid") or 0))
        return start(dump, idle_sec=idle_sec, boot_timeout=boot_timeout)


def probe(dump: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """只读探测：daemon 活着且版本匹配才返回 info；其余一律 None。

    与 ``ensure`` 的差别是**绝不杀进程、绝不重启**：ping 不通可能只是 daemon
    正忙于别人的请求（daemon 串行，忙时 ping 排队到超时），探测方不持有互斥，
    杀掉会伤及在跑的 op。调用方（批量块）拿 None 就报告 daemon 不可用。
    死活由批量开跑前的 ``prepare``（先分类再行动）或单条路径的 ``ensure``
    恢复，探测本身绝不杀。
    """
    info = _read_info()
    port = int(info.get("port") or 0)
    if not port:
        return None
    got = ping(port, timeout=1.0)
    if got and got.get("sig") == source_sig(dump):
        return info
    return None


def _notify_wait(callback: Any, elapsed: float) -> None:
    """上报忙等待进度；回调失败只记日志，**不许**破坏 ``prepare`` 的「永不抛」。"""
    try:
        callback(round(elapsed, 1))
    except Exception as exc:                    # noqa: BLE001 - 回调不参与判定
        print("警告：忙等待进度回调失败（不影响等待）：%s" % exc, flush=True)


def prepare(dump: Dict[str, Any],
            boot_timeout: int = DEFAULT_BOOT_TIMEOUT,
            busy_wait_sec: float = 2.0,
            on_wait: Optional[Any] = None) -> Dict[str, Any]:
    """批量开跑前的一次性 daemon 准备：能复用就复用，该重启才重启。

    与 ``ensure`` 的差别是**先分类再行动**——``ensure`` 对「ping 不通」一律
    清掉重启，而 ping 不通可能只是 daemon 正串行忙别人的 op（见 ``probe``）。
    返回 ``{"outcome": ..., "info"?/, "reason"?}``，outcome 四档，**永不抛**：

    - ``ready``   热着且签名匹配：原样复用，不动任何进程。
    - ``started`` 原本没在跑（info 缺失 / pid 已死）或版本不符（ping 通 =
                  正向确认是我们的 daemon 且此刻空闲 → 优雅停旧起新）。
    - ``busy``    进程还活着但 ping 没应答：先按 ``busy_wait_sec`` 有界等待它空闲
                  （daemon 串行处理 op，忙是瞬时的），到点仍不确定就**不杀、不启**，
                  调用方报告 busy 并保留当前操作归属。
    - ``failed``  启动尝试失败，reason 保留 ``DaemonError`` 原文。

    判「死」的唯一依据是 info 里的 pid 已不在（``_pid_alive``）；其余一律不杀。
    """
    try:
        info = _read_info()
        port = int(info.get("port") or 0)
        if port:
            got = ping(port, timeout=1.0)
            if got:
                if got.get("sig") == source_sig(dump):
                    return {"outcome": "ready", "info": info}
                # ping 通 = 此刻空闲且身份已确认（kind=="engine"），优雅停旧
                # 起新；ensure 在这条路上走不到它的杀分支
                return {"outcome": "started",
                        "info": ensure(dump, boot_timeout=boot_timeout)}
            pid = int(info.get("pid") or 0)
            if pid > 0 and _pid_alive(pid):
                wait = max(0.0, float(busy_wait_sec))
                deadline = time.monotonic() + wait
                waited_from = time.monotonic()
                next_notice = _BUSY_NOTICE_INTERVAL
                while time.monotonic() < deadline:
                    if on_wait is not None:
                        elapsed = time.monotonic() - waited_from
                        if elapsed >= next_notice:
                            next_notice = elapsed + _BUSY_NOTICE_INTERVAL
                            _notify_wait(on_wait, elapsed)
                    got = ping(port, timeout=0.25)
                    if got:
                        if got.get("sig") == source_sig(dump):
                            return {"outcome": "ready", "info": info}
                        return {"outcome": "started",
                                "info": ensure(dump, boot_timeout=boot_timeout)}
                    if not _pid_alive(pid):
                        return {"outcome": "started",
                                "info": ensure(dump, boot_timeout=boot_timeout)}
                    time.sleep(0.1)
                owner = _read_current_operation()
                if owner:
                    job = str(owner.get("job_id") or "未记录")
                    chunk = str(owner.get("chunk") or "未记录")
                    source = str(owner.get("source_name") or "源名称未记录")
                    age = max(0.0, time.time() - float(owner.get("started_at") or time.time()))
                    detail = ("；当前操作 job=%s chunk=%s source=%s 已运行 %.1fs"
                              % (job, chunk, source, age))
                else:
                    detail = "；当前操作归属未知"
                return {"outcome": "busy",
                        "reason": ("daemon 进程还在（pid=%d）但在 %.1fs 内未空闲，"
                                   "本批不准备也不杀%s" %
                                   (pid, max(0.0, float(busy_wait_sec)), detail))}
            if pid <= 0:
                return {"outcome": "busy",
                        "reason": ("daemon 的 info 没有 pid，无法判死活，"
                                   "本批不准备也不杀")}
        # info 缺失，或 pid 已死（info 是残骸）→ 此刻 ensure 的清理只碰得到死进程
        return {"outcome": "started",
                "info": ensure(dump, boot_timeout=boot_timeout)}
    except DaemonError as exc:
        return {"outcome": "failed", "reason": str(exc)}


def resolve(dump: Dict[str, Any], boot_timeout: int = DEFAULT_BOOT_TIMEOUT,
            busy_wait_sec: float = 2.0,
            on_wait: Optional[Any] = None) -> Dict[str, Any]:
    """``prepare`` 的**抛异常版本**：取到可用 daemon 的 info，忙/失败则抛。

    存在的理由是 ``ensure`` 的一个真实危害：它对「ping 不通」一律杀进程重启，
    而 ping 不通可能只是 daemon 正串行跑**别人**的 op——单条校验踩上去就会掐断
    那次执行（对方那一块可能仍在运行）。这里改成等一小会儿，仍忙就把
    原因原样抛出去，由调用方按任务执行策略处理，进程一个都不动。
    """
    got = prepare(dump, boot_timeout=boot_timeout, busy_wait_sec=busy_wait_sec,
                  on_wait=on_wait)
    if got.get("outcome") in ("ready", "started"):
        return dict(got.get("info") or {})
    raise DaemonError(str(got.get("reason") or "daemon 未就绪"))


def run_validate(dump: Dict[str, Any], args_file: str, timeout_slack: int = 30,
                 socket_timeout: Optional[int] = None,
                 busy_wait_sec: float = 2.0,
                 owner: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """向 daemon 发一次 op=validate 并等应答（原 `jvm_validate_daemon.run`）。

    ``socket_timeout``：等待**应答**的秒数。不给就沿用单条口径（每源 timeout +
    slack）；批量块的执行时长是块内源数 × 每源预算，必须由调用方按块规模给足
    （Kotlin 侧对 op 时长没有上限，这个客户端等待是唯一护栏——超时会抛
    ``DaemonError``，不应把它误报成源失败）。

    ``busy_wait_sec`` 交给 ``resolve``：忙时等一小会儿，不杀进程（见 ``resolve``）。
    """
    cfg = validate_params_from_args(args_file)
    info = resolve(dump, busy_wait_sec=busy_wait_sec)
    operation_id = _begin_current_operation(owner)
    cfg.update({
        "owner_job_id": str((owner or {}).get("job_id") or ""),
        "owner_chunk": str((owner or {}).get("chunk") or ""),
        "owner_source_name": str(_read_current_operation().get("source_name") or ""),
    })
    wait = int(socket_timeout) if socket_timeout else int(cfg["timeout"]) + timeout_slack
    try:
        response = _validate_request(cfg, int(info["port"]), wait)
        response["port"] = info["port"]
        return response
    finally:
        _end_current_operation(operation_id)


def _stop_port(port: int, pid: int = 0) -> bool:
    """让指定端口上的 daemon **优雅退出**（`op=stop`），必要时才动手 kill。

    先问身份再动手的理由：pid 会被系统复用，直接 kill 一个「恰好等于记录值」的 pid
    有误杀风险；从端口问身份没有。

    **必须先试 `op=stop`**：`TerminateProcess` 不执行 `finally` —— 而 daemon 的 `finally`
    里要关 server、收浏览器、清残留。正在跑的那一瞬间被硬杀，那个 Chromium 会活下来
    **继续占着 profile**：之后任何一次抓页都「自愈」换临时 profile、**cookie 静默全丢**
    （实测：结论从 3 段变 1 段，还白等 18s 超时）。所以能优雅停就优雅停。
    """
    got = ping(port, timeout=1.0)
    if not got or (pid and got.get("pid") != pid):
        return False
    graceful = False
    try:
        _exchange({"id": 0, "op": "stop"}, port, timeout=20)
        graceful = True
    except Exception:
        graceful = False
    if _PROC is not None and (not pid or _PROC.pid == pid):
        ok = _kill_proc()
        return ok or graceful
    if not graceful and pid:
        try:
            os.kill(int(pid), signal.SIGTERM)   # Windows 上等价于 TerminateProcess
        except OSError:
            return False
    # **验它真的停了**（优雅退出要收浏览器，可能几秒）。只看「有应答」就报成功是
    # 同类错里最阴的一种：跑旧类的 daemon 不认 `op=stop`，会把指令当调试请求回一句
    # 「缺参数」——那也是一条应答（实测踩过，当时报的是「已停：True」）。
    for _ in range(50):
        if not ping(port, timeout=0.4):
            return True
        time.sleep(0.2)
    return False


def _kill_proc() -> bool:
    global _PROC, _LOG
    if _PROC is None:
        return False
    try:
        if _PROC.poll() is None:
            _PROC.kill()
            _PROC.wait(timeout=10)
    except Exception:
        return False
    finally:
        _PROC = None
        if _LOG is not None:
            try:
                _LOG.close()
            except Exception:
                pass
            _LOG = None
    return True


def stop() -> bool:
    """收掉我们记录的 daemon（CLI `--daemon-stop` 用）。"""
    with _LOCK:
        info = _read_info()
        ok = False
        if info.get("port"):
            ok = _stop_port(int(info["port"]), int(info.get("pid") or 0))
        _kill_proc()
        try:
            info_path().unlink()
        except OSError:
            pass
        return ok


def stop_idle(owner_inactive: bool = False,
              expected_operation_id: Optional[str] = None) -> Dict[str, Any]:
    """仅停止经 ping 确认、当前空闲的 daemon；绝不强杀。

    **所有拒绝停止的判据必须保留**（每一条都对应一种「看起来能停其实不能」的状态）：
    owner 还在跑 / owner 身份对不上 / daemon 无响应 / 进程实例身份变化 / 停止后
    进程仍存活——任何一条命中都拒绝并保留原因。
    """
    with _LOCK:
        state = status()
        owner = state.get("current_operation") or {}
        current_operation_id = owner.get("operation_id") if owner else None
        if current_operation_id != expected_operation_id:
            return {"stopped": False, "reason": "daemon 操作归属在检查后发生变化，拒绝停止"}
        if owner and not owner_inactive:
            return {"stopped": False, "reason": "当前操作仍有 owner，拒绝停止"}
        if not state.get("process_alive"):
            info = _read_info()
            if not isinstance(info, dict):
                info = {}
            pid = int(state.get("pid") or 0)
            if pid and _pid_alive(pid):
                return {"stopped": False, "reason": "daemon 进程身份无法确认，拒绝停止"}
            if info and (not pid or not _pid_alive(pid)):
                try:
                    info_path().unlink()
                except OSError:
                    pass
            if owner:
                _end_current_operation(str(owner.get("operation_id") or ""))
            return {"stopped": True, "already_stopped": True, "reason": "daemon 进程已退出"}
        if not state.get("identity_verified") or not state.get("responsive"):
            return {"stopped": False,
                    "reason": "daemon 未响应，无法确认空闲和进程身份；未发送停止或强杀"}
        try:
            expected_pid = int(_read_info().get("pid") or 0)
            expected_process_created = process_created(expected_pid) if expected_pid else ""
        except (OSError, TypeError, ValueError, IndexError):
            return {"stopped": False, "reason": "daemon 进程实例身份无法验证；拒绝停止"}
        pid = int(state["pid"])
        port = int(state["port"])
        if not expected_process_created or process_created(pid) != expected_process_created:
            return {"stopped": False, "reason": "daemon 进程实例身份变化，拒绝停止"}
        try:
            response = _exchange({"id": 0, "op": "stop"}, port, timeout=10)
        except DaemonError as exc:
            return {"stopped": False, "reason": "daemon 未确认停止：%s" % exc}
        if response.get("code") != 0:
            return {"stopped": False,
                    "reason": "daemon 拒绝停止：%s" % str(response.get("error") or response)}
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if not _pid_alive(pid):
                break
            try:
                if process_created(pid) != expected_process_created:
                    return {"stopped": False, "reason": "daemon PID 已被其他进程复用；不清理新实例记录"}
            except (OSError, ValueError, IndexError):
                return {"stopped": False, "reason": "停止后无法确认 daemon 进程实例；不清理记录"}
            time.sleep(0.1)
        try:
            process_still_alive = _pid_alive(pid)
            if process_still_alive and process_created(pid) != expected_process_created:
                return {"stopped": False, "reason": "daemon PID 已被其他进程复用；不清理新实例记录"}
            still_same_instance = process_still_alive
        except (OSError, ValueError, IndexError):
            still_same_instance = True
        if still_same_instance:
            return {"stopped": False, "reason": "daemon 收到停止指令但进程仍存活；未强杀"}

        global _PROC, _LOG
        if _PROC is not None and _PROC.pid == pid:
            _PROC = None
            if _LOG is not None:
                try:
                    _LOG.close()
                except OSError:
                    pass
                _LOG = None
        latest = _read_info()
        if not isinstance(latest, dict):
            latest = {}
        if (int(latest.get("pid") or 0) == pid
                and int(latest.get("port") or 0) == port
                and not _pid_alive(pid)):
            try:
                info_path().unlink()
            except OSError:
                pass
        if owner:
            _end_current_operation(str(owner.get("operation_id") or ""))
        return {"stopped": True, "pid": pid, "reason": "daemon 已优雅停止并确认退出"}


def reset_for_tests() -> None:
    global _PROC, _LOG
    _PROC = None
    _LOG = None


# ---------------------------------------------------------------- 给 run_jvm_debug 的缝

def launcher_from_args(dump: Dict[str, Any], idle_sec: int = DEFAULT_IDLE_SEC,
                       boot_timeout: int = DEFAULT_BOOT_TIMEOUT,
                       slack_sec: int = 90,
                       on_note: Optional[Callable[[str], None]] = None,
                       fallback: Optional[Callable[[], Tuple[int, float, str, str]]] = None,
                       args_file: Optional[str] = None,
                       ) -> Callable[[], Tuple[int, float, str, str]]:
    """返回一个给 `run_jvm_debug(launcher=...)` 用的可调用对象：**优先 daemon，失败回落**。

    回落是**每次调用**判定的（不是启动时判一次）：daemon 半路死了、空闲退出了、版本被
    改了，下一次调用仍然能跑——用户看到的顶多是「这次慢一点」。

    `fallback` 是回落目标：CLI/开发默认走**直起**（快、且 dump 新鲜时等价）；**产品那条
    传 Gradle**（`core.jvm_debug.default_launcher`）——Gradle 一定会先编译，所以它是
    「无论如何都能跑对」的那条。提示语不区分回落叫什么：用户只需要知道「这次慢」与
    「怎么恢复快速」，不需要知道拉起方式的名字。
    """
    def _run() -> Tuple[int, float, str, str]:
        t0 = time.time()
        cfg = params_from_args(args_file) if args_file else params_from_args()
        try:
            info = ensure(dump, idle_sec=idle_sec, boot_timeout=boot_timeout)
            r = request(cfg, int(info["port"]), timeout=int(cfg["timeout"]) + slack_sec)
            return int(r.get("code", -1)), time.time() - t0, "", ""
        except Exception as e:
            # **没走成快速启动要说出来**：静默会把「快速通道一直没生效」藏起来（用户只看到
            # 「也没快多少」，下次还得再踩一遍）。措辞照界面的规矩：说结果与恢复办法，
            # 别用内部黑话（「降级/回落/常驻进程/Gradle」这类词用户无从行动）。
            if on_note is not None:
                on_note("快速启动没走成（%s），这次用了慢速方式；"
                        "跑一次「刷新引擎快照」可恢复" % e)
            fn = fallback or jvm_direct.direct_launcher(
                dump, timeout=int(cfg["timeout"]) + slack_sec + 60)
            rc, _cost, so, se = fn()
            return rc, time.time() - t0, so, se

    return _run
