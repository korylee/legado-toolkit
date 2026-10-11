# -*- coding: utf-8 -*-
"""常驻 JVM 校验服务客户端。

单条校验复用 ValidateService 的真实参数和结果文件，只把「每次起 Gradle」替换成
「首次起一个测试 JVM，后续通过 localhost 请求」。daemon 失败时由调用方保留真实原因；批量
jvm_run 不再把失败隐式转换成 Gradle fallback。
"""

from __future__ import annotations

import json
import os
import pathlib
import socket
import subprocess
import threading
import time
from typing import Any, Dict, Optional
from uuid import uuid4

from core import jvm_daemon, jvm_direct
from core.paths import data_path


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
                                            ctypes.byref(exited), ctypes.byref(kernel),
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

DAEMON_CLASS = "io.legado.app.service.ValidateServiceDaemonLauncher"
INFO_NAME = "validate_daemon.json"
LOG_NAME = "validate_daemon.log"
DEFAULT_IDLE_SEC = 1800
DEFAULT_BOOT_TIMEOUT = 180

#: 忙等待期间每隔这么久回调一次 ``on_wait``（让调用方把等待写进时间线）。
#: 短等待（≤ 这个值）不上报，避免刷屏。
_BUSY_NOTICE_INTERVAL = 10.0

#: 守护启动/停止的互斥锁。**必须是可重入锁**：``ensure`` 内部会调 ``start``，两处都要持锁。
#: 没有它，两个调用方（后端进程内两条链、或后端与 CLI 同机）同时判定「没在跑」就会各起一个
#: daemon：多占一个端口与一份堆，``_PROC`` 只记住后一个，前一个成孤儿。
_LOCK = threading.RLock()
_PROC: Optional[subprocess.Popen] = None
_LOG = None


class ValidateDaemonError(RuntimeError):
    """daemon 执行失败；调用方保留真实原因，并按任务执行策略处理。"""


def current_operation_path() -> pathlib.Path:
    return pathlib.Path(data_path("app_probe", "validate_daemon.current.json"))


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
        raise ValidateDaemonError("无法记录 operation owner 的进程实例身份：%s" % exc) from exc
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


def info_path() -> pathlib.Path:
    return pathlib.Path(data_path("app_probe", INFO_NAME))


def log_path() -> pathlib.Path:
    return pathlib.Path(data_path("app_probe", LOG_NAME))


def source_sig(dump: Optional[Dict[str, Any]] = None) -> str:
    return jvm_daemon.source_sig(dump)


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
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=min(timeout, 10)) as sock:
            sock.settimeout(timeout)
            sock.sendall((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
            line = _read_line(sock)
    except OSError as exc:
        raise ValidateDaemonError("连不上 Validate daemon(port=%s): %s" % (port, exc)) from exc
    if not line.strip():
        raise ValidateDaemonError("Validate daemon 没有回应答（连接被关）")
    try:
        return json.loads(line)
    except Exception as exc:
        raise ValidateDaemonError("Validate daemon 的应答不是 JSON: %r" % line[:200]) from exc


def ping(port: int, timeout: float = 5.0) -> Optional[Dict[str, Any]]:
    try:
        result = _exchange({"id": 0, "op": "ping"}, port, timeout)
    except ValidateDaemonError:
        return None
    return result if "sig" in result and result.get("kind") == "validate" else None


def params_from_args(args_file: Optional[str] = None) -> Dict[str, Any]:
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


def request(cfg: Dict[str, Any], port: int, timeout: float) -> Dict[str, Any]:
    payload = {
        "id": int(time.time() * 1000) % 1_000_000,
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
    result = _exchange(payload, port, timeout)
    if "code" not in result:
        raise ValidateDaemonError("Validate daemon 的应答缺 code: %r" % result)
    return result


def _read_info() -> Dict[str, Any]:
    try:
        return json.loads(info_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_info(info: Dict[str, Any]) -> None:
    path = info_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")


def _log_tail(lines: int = 12) -> str:
    try:
        text = log_path().read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return " / ".join(text.strip().splitlines()[-lines:])


def _kill_proc() -> None:
    global _PROC, _LOG
    if _PROC is not None:
        try:
            if _PROC.poll() is None:
                _PROC.kill()
                _PROC.wait(timeout=10)
        except Exception:
            pass
        _PROC = None
    if _LOG is not None:
        try:
            _LOG.close()
        except Exception:
            pass
        _LOG = None


def _stop_port(port: int) -> None:
    try:
        _exchange({"id": 0, "op": "stop"}, port, 10)
    except Exception:
        pass
    for _ in range(50):
        if not ping(port, timeout=0.4):
            return
        time.sleep(0.2)


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


def status() -> Dict[str, Any]:
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
    identity = bool(got and got.get("kind") == "validate" and got_pid == pid)
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
    }


def stop_idle(owner_inactive: bool = False,
              expected_operation_id: Optional[str] = None) -> Dict[str, Any]:
    """仅停止经 ping 确认、当前空闲的 Validate daemon；绝不强杀。"""
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
        except ValidateDaemonError as exc:
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
        return {"stopped": True, "pid": pid, "reason": "Validate daemon 已优雅停止并确认退出"}


def stop() -> bool:
    global _PROC
    with _LOCK:
        info = _read_info()
        port = int(info.get("port") or 0)
        running = bool(port and ping(port, timeout=1.0))
        if running:
            _stop_port(port)
        _kill_proc()
        try:
            info_path().unlink()
        except OSError:
            pass
        return running


def start(dump: Dict[str, Any], idle_sec: int = DEFAULT_IDLE_SEC,
          boot_timeout: int = DEFAULT_BOOT_TIMEOUT) -> Dict[str, Any]:
    with _LOCK:
        return _start_locked(dump, idle_sec=idle_sec, boot_timeout=boot_timeout)


def _start_locked(dump: Dict[str, Any], idle_sec: int = DEFAULT_IDLE_SEC,
                  boot_timeout: int = DEFAULT_BOOT_TIMEOUT) -> Dict[str, Any]:
    global _PROC, _LOG
    jvm_direct.validate_dump(dump)
    port = jvm_daemon.pick_port()
    sig = source_sig(dump)
    env = {
        **jvm_direct.java_env(dump),
        "LEGADO_VALIDATE_DAEMON_PORT": str(port),
        "LEGADO_VALIDATE_DAEMON_IDLE_SEC": str(int(idle_sec)),
        "LEGADO_VALIDATE_DAEMON_SIG": sig,
        "LEGADO_TEST_JVM_ENV_OUT": str(jvm_direct.dump_path()),
        "LEGADO_TEST_JVM_LAUNCH_MODE": "validate_daemon",
    }
    log_path().parent.mkdir(parents=True, exist_ok=True)
    _LOG = open(log_path(), "a", encoding="utf-8")
    _LOG.write("\n==== %s 起 Validate daemon：port=%s sig=%s\n" %
               (time.strftime("%F %T"), port, sig))
    _LOG.flush()
    _PROC = subprocess.Popen(
        jvm_direct.java_command(dump, DAEMON_CLASS),
        cwd=dump["workingDir"], env=env, stdout=_LOG, stderr=_LOG,
        text=True,
    )
    deadline = time.time() + boot_timeout
    while time.time() < deadline:
        got = ping(port, timeout=1.0)
        if got and got.get("sig") == sig:
            info = {"pid": _PROC.pid, "port": port, "sig": sig,
                    "started_at": time.time()}
            _write_info(info)
            return info
        if _PROC.poll() is not None:
            raise ValidateDaemonError("Validate daemon 启动即退出（码 %s）：%s" %
                                      (_PROC.returncode, _log_tail()))
        time.sleep(0.25)
    raise ValidateDaemonError("Validate daemon %.0fs 内没起来（看 %s）" %
                              (boot_timeout, log_path()))


def ensure(dump: Dict[str, Any], idle_sec: int = DEFAULT_IDLE_SEC,
           boot_timeout: int = DEFAULT_BOOT_TIMEOUT) -> Dict[str, Any]:
    with _LOCK:
        info = _read_info()
        port = int(info.get("port") or 0)
        current_sig = source_sig(dump)
        if port:
            got = ping(port, timeout=1.0)
            if got and got.get("sig") == current_sig:
                return info
            if got:
                _stop_port(port)
        _kill_proc()
        try:
            info_path().unlink()
        except OSError:
            pass
        return start(dump, idle_sec=idle_sec, boot_timeout=boot_timeout)


def probe(dump: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """只读探测：daemon 活着且版本匹配才返回 info；其余一律 None。

    与 ``ensure`` 的差别是**绝不杀进程、绝不重启**：ping 不通可能只是 daemon
    正忙于别人的请求（daemon 串行，忙时 ping 排队到超时），探测方不持有互斥，
    杀掉会伤及在跑的 op。调用方（批量块）拿 None 就报告 daemon 不可用，不能杀掉仍可能执行的请求。
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
    - ``failed``  启动尝试失败，reason 保留 ``ValidateDaemonError`` 原文。

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
                # ping 通 = 此刻空闲且身份已确认（kind=="validate"），优雅停旧
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
                        "reason": ("Validate daemon 进程还在（pid=%d）但在 %.1fs 内未空闲，"
                                   "本批不准备也不杀%s" %
                                   (pid, max(0.0, float(busy_wait_sec)), detail))}
            if pid <= 0:
                return {"outcome": "busy",
                        "reason": ("Validate daemon 的 info 没有 pid，无法判死活，"
                                   "本批不准备也不杀")}
        # info 缺失，或 pid 已死（info 是残骸）→ 此刻 ensure 的清理只碰得到死进程
        return {"outcome": "started",
                "info": ensure(dump, boot_timeout=boot_timeout)}
    except ValidateDaemonError as exc:
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
    raise ValidateDaemonError(str(got.get("reason") or "Validate daemon 未就绪"))


def run(dump: Dict[str, Any], args_file: str, timeout_slack: int = 30,
        socket_timeout: Optional[int] = None,
        busy_wait_sec: float = 2.0,
        owner: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """向 daemon 发一次执行请求并等应答。

    ``socket_timeout``：等待**应答**的秒数。不给就沿用单条口径（每源 timeout +
    slack）；批量块的执行时长是块内源数 × 每源预算，必须由调用方按块规模给足
    （Kotlin 侧对 op 时长没有上限，这个客户端等待是唯一护栏——超时会抛
    ``ValidateDaemonError``，不应把它误报成源失败）。

    ``busy_wait_sec`` 交给 ``resolve``：忙时等一小会儿，不杀进程（见 ``resolve``）。
    单条等待窗口**有意短**；批量按块规模计算 socket 超时，daemon 不可用就让块失败、保留原因。
    """
    cfg = params_from_args(args_file)
    info = resolve(dump, busy_wait_sec=busy_wait_sec)
    operation_id = _begin_current_operation(owner)
    cfg.update({
        "owner_job_id": str((owner or {}).get("job_id") or ""),
        "owner_chunk": str((owner or {}).get("chunk") or ""),
        "owner_source_name": str(_read_current_operation().get("source_name") or ""),
    })
    wait = int(socket_timeout) if socket_timeout else int(cfg["timeout"]) + timeout_slack
    try:
        response = request(cfg, int(info["port"]), wait)
        response["port"] = info["port"]
        return response
    finally:
        _end_current_operation(operation_id)


def reset_for_tests() -> None:
    global _PROC, _LOG
    _PROC = None
    _LOG = None
