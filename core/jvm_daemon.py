# -*- coding: utf-8 -*-
"""常驻调试 daemon 的客户端（S5-A 第二期 D1）。

**它解决什么**（D0 实测）：单次调试的入场费是 **10.7s**（JVM + Robolectric + App 初始化），
而站点本身只要 0.7s。daemon 把「起一次」摊到多次请求上——同一条源连跑两次，第二次
只剩站点的时间。

## 与一次性那条**同形**（这是关键设计，别改成另一套）

daemon 只换「怎么拉起」：请求走一行 JSON 过 socket，产物（NDJSON + 侧车）**还是落到
原来的路径**，于是 `core.jvm_debug.run_jvm_debug` 的解析一行不改——它就靠 D0 留的
`launcher=` 缝接进来。所以三条拉起方式（Gradle / `java` 直起 / 常驻）的差别**只有拉起**，
结论不可能因为换了拉起方式而不同（这正是 D1 的验收判据）。

## 降级路径（必须有，别等 D2）

`launcher_from_args()` 的每一次调用都是「先试 daemon，**任何失败都回落直起**」：
daemon 起不来 / 半路死了 / 版本对不上 / 端口被占——用户看到的顶多是「这次慢一点」，
而不是「调试坏了」。回落走 `core.jvm_direct`（D0 那条直起路径，已与 bat 路径对拍过）。

## 版本键

`source_sig()` = appservice 的 Kotlin 源码指纹 + dump 指纹。**改了 Kotlin 不重启进程，
进程里跑的还是旧类**，而那看起来像「规则改了没生效」——所以 sig 变了就换进程。
daemon 自己不判版本（它不知道磁盘上现在是什么），只在 `op=ping` 时把起它时收到的 sig
原样回吐，由客户端比。
"""

from __future__ import annotations

import json
import os
import pathlib
import signal
import socket
import subprocess
import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple

from core import jvm_direct
from core.paths import data_path

DAEMON_CLASS = "io.legado.app.service.DebugServiceDaemonLauncher"
INFO_NAME = "jvm_daemon.json"
LOG_NAME = "jvm_daemon.log"

#: 默认空闲退出（秒）。**真正的计时在 daemon 里**，这里只是传给它；
#: 改值要两边一起想（Kotlin 侧 `DebugServiceDaemon.DEFAULT_IDLE_SEC`）。
DEFAULT_IDLE_SEC = 1800
#: 首次拉起要等 Robolectric + App 起来（D0 实测 10.7s；首次编译/冷缓存会更久）
DEFAULT_BOOT_TIMEOUT = 180

_LOCK = threading.Lock()
_PROC: Optional[subprocess.Popen] = None
_LOG = None


class DaemonError(RuntimeError):
    """daemon 不可用（起不来 / 不认路 / 半路死）。**调用方应回落，不该把它当结论**。"""


def info_path() -> pathlib.Path:
    return pathlib.Path(data_path("app_probe", INFO_NAME))


def log_path() -> pathlib.Path:
    return pathlib.Path(data_path("app_probe", LOG_NAME))


def source_sig(dump: Optional[Dict[str, Any]] = None) -> str:
    """版本键：Kotlin 源码（数量+mtime+总大小）+ dump 的 mtime。

    **不是密码学哈希**，D1 够用：它的职责只是「改了代码别用旧进程」。文件大小也进来是
    为了挡住同一秒内的连续编辑（mtime 精度够、但 truncate 成秒就不够了）。"""
    files = list((jvm_direct.AGSVC / "test").rglob("*.kt"))
    mt = max((f.stat().st_mtime for f in files), default=0.0)
    size = sum(f.stat().st_size for f in files)
    d = jvm_direct.dump_path()
    dm = d.stat().st_mtime if d.exists() else 0.0
    return "kt%d-%d-%d-dump%d" % (len(files), int(mt), size, int(dm))


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
    return r if "sig" in r else None


def request(cfg: Dict[str, Any], port: int, timeout: float) -> Dict[str, Any]:
    """跑一次调试（参数与 `DebugService.Config` 一一对应）。"""
    payload = {
        "id": int(time.time() * 1000) % 1_000_000,
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
    """把 `appservice/args.properties` 读成请求参数。

    **故意读那个文件**：一次性那条链的契约就是「参数在 args.properties 里，拉起方自己
    去读」（Kotlin 侧 `AppserviceEnv.loadArgs` 是同一个约定）。常驻这边读同一份，
    `run_jvm_debug` 写一次参数、三条拉起方式都认得，不会出现「命令行跑了另一条源」。
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
    info = _read_info()
    got = ping(int(info.get("port") or 0), timeout=1.0) if info.get("port") else None
    ok = bool(got and got.get("pid") == info.get("pid"))
    return {"running": ok, "info": info, "sig_now": source_sig(),
            "sig_matches": bool(ok and got and got.get("sig") == info.get("sig")
                                and info.get("sig") == source_sig()),
            "log": str(log_path())}


def start(dump: Dict[str, Any], idle_sec: int = DEFAULT_IDLE_SEC,
          boot_timeout: int = DEFAULT_BOOT_TIMEOUT) -> Dict[str, Any]:
    """拉起一个 daemon 并等它就绪。失败抛 `DaemonError`（**带日志尾巴**，别让人对着空手猜）。"""
    global _PROC, _LOG
    port = pick_port()
    sig = source_sig(dump)
    cmd = jvm_direct.java_command(dump, DAEMON_CLASS)
    env = {**jvm_direct.java_env(dump),
           "LEGADO_DAEMON_PORT": str(port),
           "LEGADO_DAEMON_IDLE_SEC": str(int(idle_sec)),
           "LEGADO_DAEMON_SIG": sig}
    log_path().parent.mkdir(parents=True, exist_ok=True)
    _LOG = open(log_path(), "a", encoding="utf-8")
    _LOG.write("\n==== %s 起 daemon：port=%s sig=%s\n" % (time.strftime("%F %T"), port, sig))
    _LOG.flush()
    _PROC = subprocess.Popen(cmd, cwd=dump.get("workingDir") or None, env=env,
                             stdout=_LOG, stderr=subprocess.STDOUT)
    deadline = time.time() + max(5, boot_timeout)
    while time.time() < deadline:
        if _PROC.poll() is not None:
            raise DaemonError("daemon 启动即退出（码 %s）：%s" % (_PROC.returncode, _log_tail()))
        if ping(port, timeout=0.3):
            info = {"pid": _PROC.pid, "port": port, "sig": sig, "idle_sec": int(idle_sec),
                    "started_at": time.strftime("%F %T")}
            _write_info(info)
            return info
        time.sleep(0.2)
    _kill_proc()
    raise DaemonError("daemon %.0fs 内没起来（看 %s）" % (boot_timeout, log_path()))


def ensure(dump: Dict[str, Any], idle_sec: int = DEFAULT_IDLE_SEC,
           boot_timeout: int = DEFAULT_BOOT_TIMEOUT) -> Dict[str, Any]:
    """拿到一个**可用**的 daemon：活着且版本对就复用，否则（重）起一个。"""
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


def _stop_port(port: int, pid: int) -> bool:
    """让指定端口上的 daemon **优雅退出**（`op=stop`），必要时才动手 kill。

    先问身份再动手的理由：pid 会被系统复用，直接 kill 一个「恰好等于记录值」的 pid
    有误杀风险；从端口问身份没有。

    **必须先试 `op=stop`**：常驻里浏览器是刻意留着的（`keepBrowserOpen`），而
    TerminateProcess 不执行 `finally` —— 那个 Chromium 会活下来**继续占着 profile**，
    之后任何一次抓页都「自愈」换临时 profile、**cookie 静默全丢**（实测：结论从 3 段
    变 1 段，还白等 18s 超时）。所以能优雅停就优雅停。
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
    if _PROC is not None and _PROC.pid == pid:
        ok = _kill_proc()
        return ok or graceful
    if not graceful:
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
    """收掉我们记录的 daemon（界面/CLI 的「停掉常驻」）。"""
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
                       fallback_name: str = "直起",
                       ) -> Callable[[], Tuple[int, float, str, str]]:
    """返回一个给 `run_jvm_debug(launcher=...)` 用的可调用对象：**优先 daemon，失败回落**。

    回落是**每次调用**判定的（不是启动时判一次）：daemon 半路死了、空闲退出了、版本被
    改了，下一次调用仍然能跑——用户看到的顶多是「这次慢一点」。

    `fallback` 是回落目标：CLI/开发默认走**直起**（快、且 dump 新鲜时等价）；**产品那条
    传 Gradle**（`core.jvm_debug.default_launcher`）——Gradle 一定会先编译，所以它是
    「无论如何都能跑对」的那条。`fallback_name` 只影响提示语里怎么称呼它。
    """
    def _run() -> Tuple[int, float, str, str]:
        t0 = time.time()
        cfg = params_from_args()
        try:
            info = ensure(dump, idle_sec=idle_sec, boot_timeout=boot_timeout)
            r = request(cfg, int(info["port"]), timeout=int(cfg["timeout"]) + slack_sec)
            return int(r.get("code", -1)), time.time() - t0, "", ""
        except Exception as e:
            # **没走成常驻要说出来**：静默会把「常驻一直没生效」藏起来（用户只看到
            # 「也没快多少」，下次还得再踩一遍）。措辞照界面的规矩：说结果、别用内部黑话
            # （「降级/回落」这类过程词在 `tools/check_copy.py` 的术语表里是禁的）。
            if on_note is not None:
                on_note("这次没能用常驻进程（%s），已改用%s（启动慢一些）" % (e, fallback_name))
            fn = fallback or jvm_direct.direct_launcher(
                dump, timeout=int(cfg["timeout"]) + slack_sec + 60)
            rc, _cost, so, se = fn()
            return rc, time.time() - t0, so, se

    return _run
