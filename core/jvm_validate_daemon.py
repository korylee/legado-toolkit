# -*- coding: utf-8 -*-
"""常驻 JVM 校验服务客户端。

单条校验复用 ValidateService 的真实参数和结果文件，只把「每次起 Gradle」替换成
「首次起一个测试 JVM，后续通过 localhost 请求」。daemon 失败时由调用方回到 Gradle，
不能把 daemon 的失败误报成源校验结论。
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

from core import jvm_daemon, jvm_direct
from core.paths import data_path

DAEMON_CLASS = "io.legado.app.service.ValidateServiceDaemonLauncher"
INFO_NAME = "validate_daemon.json"
LOG_NAME = "validate_daemon.log"
DEFAULT_IDLE_SEC = 1800
DEFAULT_BOOT_TIMEOUT = 180

_LOCK = threading.Lock()
_PROC: Optional[subprocess.Popen] = None
_LOG = None


class ValidateDaemonError(RuntimeError):
    """Validate daemon 不可用，调用方应保留原因并回到 Gradle。"""


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
        "keyword": props.get("keyword", "我"),
        "out": props.get("out", ""),
        "concurrency": int(props.get("concurrency") or 1),
        "timeout": int(props.get("timeout") or 30),
        "limit": int(props.get("limit") or 0),
        "depth": props.get("depth", "search"),
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
        "keyword": str(cfg.get("keyword") or "我"),
        "out": str(cfg.get("out") or ""),
        "concurrency": int(cfg.get("concurrency") or 1),
        "timeout": int(cfg.get("timeout") or 30),
        "limit": int(cfg.get("limit") or 0),
        "depth": str(cfg.get("depth") or "search"),
        "cookie": str(cfg.get("cookie") or ""),
        "no_strip_webview": bool(cfg.get("no_strip_webview")),
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

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:  # PROCESS_QUERY_LIMITED_INFORMATION
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 活着，只是没权限发信号
    return True


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
    杀掉会伤及在跑的 op。调用方（批量块）拿 None 就回落 Gradle——daemon 的
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


def prepare(dump: Dict[str, Any],
            boot_timeout: int = DEFAULT_BOOT_TIMEOUT) -> Dict[str, Any]:
    """批量开跑前的一次性 daemon 准备：能复用就复用，该重启才重启。

    与 ``ensure`` 的差别是**先分类再行动**——``ensure`` 对「ping 不通」一律
    清掉重启，而 ping 不通可能只是 daemon 正串行忙别人的 op（见 ``probe``）。
    返回 ``{"outcome": ..., "info"?/, "reason"?}``，outcome 四档，**永不抛**：

    - ``ready``   热着且签名匹配：原样复用，不动任何进程。
    - ``started`` 原本没在跑（info 缺失 / pid 已死）或版本不符（ping 通 =
                  正向确认是我们的 daemon 且此刻空闲 → 优雅停旧起新）。
    - ``busy``    进程还活着但 ping 没应答：忙或探测不确定——**不杀、不启**，
                  调用方按块回落 Gradle。
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
                return {"outcome": "busy",
                        "reason": ("Validate daemon 进程还在（pid=%d）但 ping 没应答"
                                   "（正忙或探测不确定），本批不准备也不杀" % pid)}
            if pid <= 0:
                return {"outcome": "busy",
                        "reason": ("Validate daemon 的 info 没有 pid，无法判死活，"
                                   "本批不准备也不杀")}
        # info 缺失，或 pid 已死（info 是残骸）→ 此刻 ensure 的清理只碰得到死进程
        return {"outcome": "started",
                "info": ensure(dump, boot_timeout=boot_timeout)}
    except ValidateDaemonError as exc:
        return {"outcome": "failed", "reason": str(exc)}


def run(dump: Dict[str, Any], args_file: str, timeout_slack: int = 30,
        socket_timeout: Optional[int] = None) -> Dict[str, Any]:
    """向 daemon 发一次执行请求并等应答。

    ``socket_timeout``：等待**应答**的秒数。不给就沿用单条口径（每源 timeout +
    slack）；批量块的执行时长是块内源数 × 每源预算，必须由调用方按块规模给足
    （Kotlin 侧对 op 时长没有上限，这个客户端超时是唯一的护栏——超时会抛
    ``ValidateDaemonError``，调用方回落 Gradle，不是源失败）。
    """
    cfg = params_from_args(args_file)
    info = ensure(dump)
    wait = int(socket_timeout) if socket_timeout else int(cfg["timeout"]) + timeout_slack
    response = request(cfg, int(info["port"]), wait)
    response["port"] = info["port"]
    return response


def reset_for_tests() -> None:
    global _PROC, _LOG
    _PROC = None
    _LOG = None
