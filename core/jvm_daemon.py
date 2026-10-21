# -*- coding: utf-8 -*-
"""常驻引擎 daemon 的客户端（**唯一一份**，2026-10-18 起调试与校验共用）。

**它解决什么**：单次调试/校验的**入场费**（JVM + Robolectric + App 初始化）比站点请求
贵一个量级，常驻把「起一次」摊到多次请求上。
**实测数字别在这里抄**（会过期）：去代码与 `git log` 查。

## boot 经 Gradle（2026-10-18 起唯一 boot 路径，方案甲）

拉起 = `legado-gradle.bat :app:testAppDebugUnitTest --tests AppServiceDaemonLauncher
--rerun`（测试阻塞 serve，Gradle 握着进程）。**Gradle boot 前必编译**，所以 daemon
进程里跑的**永远是当前源码**——「dump 过期 → 跑旧类 → 规则改了没生效」这个失败模式
随直起一起退场（原 `jvm_direct` 的 dump/argfile 机制不再存在）。
实测（2026-10-18 本机）：Kotlin 未变时 boot 到 ping-ok 33.9s——它是每 daemon 生命周期
一次、30 分钟 idle 共享的入场费。

## 与一次性那条**同形**（关键设计，别改成另一套）

daemon 只换「怎么拉起」：请求走一行 JSON 过 socket（`op=debug` / `op=validate`，见
`appservice` 的 `AppServiceDaemon`），产物（NDJSON + 侧车 / results.jsonl）**还是落到
原来的路径**，于是 `core.jvm_debug.run_jvm_debug` 与批量那条链的解析一行不改——它靠
`launcher=` 缝接进来。

## 生命周期语义（2026-10-18 合并时取 validate 侧的强语义为准）

## 身份与端口（2026-10-21 起：身份只认端口应答）

`jvm_daemon.json` 是**缓存**（boot 时间、日志路径、owner 提示），**不是身份**：
任何复用/停止判据都不读它的 pid。理由是被实测证过两次——pid 记错（记过包装进程）时
判据恒假，表现是「每次调用都冷启一个」（2026-10-20：`rule_try` ~28s/次，日志「起 daemon」
55 次 /「就绪」30 次）；而记录本身会被 `stop_idle` 清掉，清了就再也找不到那个引擎。

随机端口（`pick_port`）因此退场：它是**唯一线索必须写进记录**的那套设计。现在端口是
推导出来的——`base = 41000 + slot*20`，`slot = crc32(data 目录) % 200`，窗口
`[base, base+20)`，整段压在 Windows 动态端口范围（49152–65535，出站连接会自动占用）
下面；窗口按 20 对齐分槽，**不同槽的窗口不相交**，同槽（200 分之 1 对）会共用同一个
窗口——两个 checkout 要么把起点错开 20，要么接受这种极低概率的耦合（后果见
`_daemon_base_port`）。settings 的 `jvm.daemon_port` 可显式覆盖 base。

找引擎 = 一次 netstat 拿窗口内 LISTEN 端口及属主，**只对「被占」的端口 ping**——Windows
上对已关闭端口的 connect 抛的是超时（模块头那行实测），盲扫 20 个端口要 6s，而
`status()` 每次刷新都会调它。三条分支：

- 应答 `kind=="engine"` 且 `sig` 相符 → 复用；
- 应答 `kind=="engine"` 但 `sig` 不符 → 进程里是旧类，`_stop_port` 收掉再起；
  **停不掉就报错**（不"先起一个再说"——那正是「两个引擎抢同一份浏览器 profile」的来源）；
- 不是我们的协议 → 跳过，换窗口里第一个空闲端口起。

- **probe 只读**：绝不杀进程、绝不重启——ping 不通可能只是 daemon 正串行忙别人的
  op；忙与死的分界靠「端口还在不在监听」与 netstat 给出的属主 pid，不靠缓存 pid。
- **prepare 先分类再行动**：ready / started / busy / failed 四档，永不抛；busy 是
  「端口被占但没应答我们」，有界等待，到点不杀不启，原因留给调用方。
- **resolve 抛异常版**：单条路径拿不到可用 daemon 就带原因抛，进程一个都不动。
- **stop_idle 保守停**：身份核验（ping + 进程实例身份）+ PID 复用防护 + 优雅退出
  确认，绝不强杀。
- **降级路径（必须有）**：`launcher_from_args()` 的每一次调用都是「先试 daemon，
  任何失败就回落 Gradle」——用户看到的顶多是「这次慢一点」，而不是「调试坏了」。

## 版本键

`source_sig()` = appservice 的 Kotlin 源码指纹。**改了 Kotlin 不重启进程，进程里跑的
还是旧类**，而那看起来像「规则改了没生效」——所以 sig 变了就换进程。daemon 自己不判
版本（它不知道磁盘上现在是什么），只在 `op=ping` 时把起它时收到的 sig 原样回吐，
由客户端比。Gradle boot 必编译，所以「sig 对不上」只发生在复用**起得更早**的进程时。
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
import zlib
from typing import Any, Callable, Dict, List, Optional, Tuple
from uuid import uuid4

from core.paths import data_dir, data_path

DAEMON_CLASS = "io.legado.app.service.AppServiceDaemonLauncher"
BOOT_TASK = ":app:testAppDebugUnitTest"
INFO_NAME = "jvm_daemon.json"
LOG_NAME = "jvm_daemon.log"

#: 默认空闲退出（秒）。**真正的计时在 daemon 里**，这里只是传给它；
#: 改值要两边一起想（Kotlin 侧 `AppServiceDaemon.DEFAULT_IDLE_SEC`）。
DEFAULT_IDLE_SEC = 1800
#: 首次拉起要等 Gradle 配置 + 编译检查 + Robolectric + App 起来
#: （实测 2026-10-18 本机：Kotlin 未变 33.9s；改了 Kotlin 要加上编译时间）
DEFAULT_BOOT_TIMEOUT = 180
#: boot 看门狗杀完进程后等端口释放的秒数。Gradle daemon 是脱离 cmd 进程树的
#: 常驻服务（`taskkill /T` 够不到），`--no-daemon` 之后整树可杀，这里只是收尾余量。
_KILL_VERIFY_SEC = 10.0

#: 忙等待期间每隔这么久回调一次 ``on_wait``（让调用方把等待写进时间线）。
#: 短等待（≤ 这个值）不上报，避免刷屏。
_BUSY_NOTICE_INTERVAL = 10.0

#: 端口窗口：``base = SLOT_BASE + slot*PORT_WINDOW``，窗口 = ``[base, base+PORT_WINDOW)``。
#: **整段压在 Windows 动态端口范围（49152–65535）下面**：那里会被出站连接自动占走，
#: 窗口落进去就会时不时被一个无关的 TCP 连接顶掉。``PORT_SLOTS`` 个槽按窗口对齐：
#: 不同槽的窗口不相交（最后一个槽的下界是 44980），同槽的后果见 ``_daemon_base_port``。
SLOT_BASE = 41000
PORT_WINDOW = 20
PORT_SLOTS = 200
#: 扫描时单次 ping 的超时：只打 netstat 说「有人在听」的端口，0.4s 够 loopback 应答。
_SCAN_PING_TIMEOUT = 0.4

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


def source_sig() -> str:
    """版本键：appservice 的 Kotlin 源码指纹（数量 + mtime + 总大小）。

    **不是密码学哈希**，职责是「改了代码别用旧进程」。boot 经 Gradle 必编译，
    所以「sig 对不上」只出现在**复用起得更早的 daemon**时——重用前比对，
    对不上就换进程。**刻意不缓存**：源码每次重新读取，必须逐次反映真实状态；
    加缓存省下的时间抵不上多出来的失效条件。"""
    from core import jvm_debug          # 惰性：jvm_debug 在函数内 import 本模块
    files = list((jvm_debug.AGSVC / "test").rglob("*.kt"))
    mt = max((f.stat().st_mtime for f in files), default=0.0)
    size = sum(f.stat().st_size for f in files)
    return "kt%d-%d-%d" % (len(files), int(mt), size)


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


def _answer_pid(got: Optional[Dict[str, Any]]) -> int:
    """应答里报的 pid（拿不出来就是 0）。

    判别据一律走它：端口上可能是**别人的进程**，`pid` 是任何 JSON 值，
    把 `int()` 直接写在判据里会让 ValueError 从「永不抛」的函数里逃出去。
    """
    try:
        return int((got or {}).get("pid") or 0)
    except (TypeError, ValueError):
        return 0


def identity_ok(got: Optional[Dict[str, Any]], sig: Optional[str] = None) -> bool:
    """端口上的应答是不是**我们这台 daemon**：`kind == "engine"` 且 `sig` 与当前源码一致。

    ``sig`` 不给就现算 :func:`source_sig`；boot 期间要传**起进程时那个** sig——那时源码
    可能又改了，比现算的才对（见 :func:`_start_locked`）。

    **复用判据只有这一份**（`ensure` / `prepare` / `probe` / `status` / `is_running` 都走它）。
    ``_stop_port`` **刻意不走**它：停止要求的不是「sig 相符」，而是「此刻应答的就是要停的
    那个进程」——sig 不符（旧类）正是要停掉它的理由。

    为什么不比 pid：`info` 里的 pid 曾经写的是**启动器/包装进程**的（`cmd /c
    legado-gradle.bat …`），与 daemon 的 JVM 不是一个数——拿它当身份会让判据恒假，而表现
    不是报错，是**每次调用都冷启一个 daemon**（2026-10-20 实测：`rule_try` 每次 ~28s，
    全是冷启；同期日志「起 daemon」55 次 /「就绪」30 次）。而且 pid 会被系统复用，本来
    就不适合当身份——pid 的用途只剩「防 pid 复用」（`process_created`）与整树收尾。
    """
    if not got or got.get("kind") != "engine":
        return False
    try:
        return got.get("sig") == (sig if sig is not None else source_sig())
    except (OSError, ValueError):
        return False


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
    """读**缓存**记录（boot 时间 / 日志路径 / 上次端口）。**任何身份判据都不读它**。"""
    p = info_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_info(info: Dict[str, Any]) -> None:
    """写**缓存**记录：只服务展示与诊断，写坏了大不了多看一句「状态未知」。"""
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
    """请求失败时的诊断上下文：**扫描到的**引擎、owner、日志尾巴。

    用扫描而不是缓存：缓存里的 pid 可能是包装进程的旧值，写进诊断只会把人带偏；
    「端口上有东西但没应答我们」和「窗口里什么都没有」必须是两句不同的话。
    """
    scan = _scan()
    got = scan.get("got") or {}
    if got:
        parts = ["daemon pid=%s port=%s（应答）"
                 % (got.get("pid") or "未报", scan.get("port"))]
    elif scan.get("blind"):
        parts = ["daemon 监听表读不到（netstat/lsof 失败）"]
    elif scan.get("port"):
        parts = ["daemon 端口=%s 被占但没有应答我们" % scan.get("port")]
    else:
        parts = ["daemon 端口窗口里没有引擎在应答（请求发往 port=%s）" % port]
    owner = _read_current_operation()
    if owner:
        parts.append("owner job=%s chunk=%s pid=%s" % (
            owner.get("job_id") or "?", owner.get("chunk") or "?",
            owner.get("pid") or "?"))
    process = _PROC
    if process is not None:
        try:
            exit_code = process.poll()
        except OSError:
            exit_code = None
        if exit_code is not None:
            parts.append("daemon 启动器已退出（exit_code=%s）" % exit_code)
    tail = _log_tail(8)
    if tail:
        parts.append("daemon log tail=" + tail[-1000:])
    return "; ".join(parts)


def _daemon_base_port() -> int:
    """端口窗口的起点：settings 的 ``jvm.daemon_port`` 优先，否则按 data 目录落到槽位。

    **为什么是推导的而不是随机的**：随机端口的唯一线索只能写进 `jvm_daemon.json`，
    记录一错/一丢就再也找不到那个引擎（见模块头「身份与端口」）。槽位按窗口对齐
    （``slot * PORT_WINDOW``），**不同槽的窗口不相交**；同槽（``PORT_SLOTS``=200 分之
    一对）会共用同一个窗口——同一台机器上两个 checkout 若还在跑不同一份源码，后者的
    `ensure` 会把前者的引擎停掉（旧的随机端口设计没有这个耦合）。两个 checkout 的起点
    留出 20 的间距就必然不重叠；或者用 ``jvm.daemon_port`` 显式错开。

    显式值**不做槽位对齐**（那是自己的选择，窗口从它开始）；只有窗口会顶出 65535 时
    才整体往回挪——不然窗口里会出现不存在的端口。
    """
    from core import settings_store          # 惰性：settings_store 会 import 本模块
    conf = settings_store.load().get("jvm", {})
    try:
        configured = int(conf.get("daemon_port") or 0)
    except (TypeError, ValueError):
        configured = 0
    if configured > 0:
        return min(configured, 65536 - PORT_WINDOW)
    slot = zlib.crc32(str(data_dir()).encode("utf-8")) % PORT_SLOTS
    return SLOT_BASE + slot * PORT_WINDOW


def _daemon_window() -> List[int]:
    base = _daemon_base_port()
    return list(range(base, base + PORT_WINDOW))


def _parse_netstat_listeners(text: str) -> Dict[int, int]:
    """``netstat -ano`` 的 LISTENING 行 → ``{端口: 属主 pid}``。纯解析，便于钉住。"""
    out: Dict[int, int] = {}
    for line in text.splitlines():
        parts = line.split()
        # TCP  127.0.0.1:49975  0.0.0.0:0  LISTENING  1234
        if len(parts) < 5 or parts[3] != "LISTENING":
            continue
        _, _, port = parts[1].rpartition(":")
        try:
            out[int(port)] = int(parts[-1])
        except ValueError:
            continue
    return out


def _parse_lsof_listeners(text: str) -> Dict[int, int]:
    """``lsof -F pn`` → ``{端口: 属主 pid}``（``p<pid>`` 行后跟若干 ``n<addr:port>``）。"""
    out: Dict[int, int] = {}
    pid = 0
    for line in text.splitlines():
        if line.startswith("p"):
            try:
                pid = int(line[1:])
            except ValueError:
                pid = 0
        elif line.startswith("n") and pid:
            _, _, port = line.rpartition(":")
            try:
                out[int(port)] = pid
            except ValueError:
                continue
    return out


def _listen_map() -> Optional[Dict[int, int]]:
    """一次 netstat/lsof：``{端口: 属主 pid}``，只含 LISTEN；**读不到返回 None**。

    先问「谁在听」再 ping 的理由是实测出来的：Windows 上对已关闭端口的 connect 抛的是
    **超时**而不是拒绝连接，盲扫 20 个端口要 6s（每次刷新状态都吃这一下）。

    ``None`` 与 ``{}`` 必须分开（AGENTS #12：读不到 ≠ 没有）：把「netstat 跑不起来」
    读成「窗口里没人」，会让 `status` 报 stopped、让 `ensure` 去冷启一个撞上活引擎的
    端口——两处都不报错，只是行为莫名其妙。
    """
    if os.name == "nt":
        try:
            out = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                                 capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return None
        if out.returncode != 0:
            return None
        return _parse_netstat_listeners(out.stdout or "")
    try:
        out = subprocess.run(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-F", "pn"],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    # lsof 没有匹配项时退出码就是 1 —— 那是「空表」，不是「读不到」
    if out.returncode not in (0, 1):
        return None
    return _parse_lsof_listeners(out.stdout or "")


def _scan() -> Dict[str, Any]:
    """扫一遍端口窗口，返回身份事实（**唯一的身份入口**）。

    - ``got`` / ``port`` / ``owner``：窗口里第一个应答我们协议的引擎（``kind=="engine"``，
      sig 由调用方比）与 netstat 给出的属主 pid。
    - ``occupied``：``{端口: 属主 pid}``，被占但**没应答我们**（陌生程序 / 忙 / 卡死）。
    - ``free``：窗口里第一个空闲端口（boot 用）。找到引擎时不再算（调用方要复用它）。
    - ``blind``：监听表读不到。此时只 ping 缓存提示的那个端口，其余端口**无从判断**——
      调用方据此拒绝冷启，而不是把「读不到」当成「没有」。
    """
    listening = _listen_map()
    if listening is None:
        hint = _hint_port()
        got = ping(hint, timeout=_SCAN_PING_TIMEOUT) if hint else None
        if got:
            return {"port": hint, "got": got, "owner": 0,
                    "occupied": {}, "free": 0, "blind": True}
        return {"port": 0, "got": None, "owner": 0, "occupied": {},
                "free": 0, "blind": True}
    occupied: Dict[int, int] = {}
    free = 0
    for port in _daemon_window():
        owner = int(listening.get(port) or 0)
        if not owner:
            if not free:
                free = port
            continue
        got = ping(port, timeout=_SCAN_PING_TIMEOUT)
        if got:
            return {"port": port, "got": got, "owner": owner,
                    "occupied": occupied, "free": 0, "blind": False}
        occupied[port] = owner
    return {"port": 0, "got": None, "owner": 0, "occupied": occupied,
            "free": free, "blind": False}


def _hint_port() -> int:
    """缓存里记着的端口（**只当提示**，且必须在窗口内）。

    用途仅一个：判断「这个被占的端口多半是我们的引擎」（`prepare` 的忙等待）。
    身份仍然只认应答——提示错了最多多等一会儿，不会误停、也不会误复用。
    """
    try:
        port = int((_read_info() or {}).get("port") or 0)
    except (TypeError, ValueError):
        return 0
    return port if port in _daemon_window() else 0


def _scan_info(scan: Dict[str, Any]) -> Dict[str, Any]:
    """把一次扫描与缓存合成调用方要的 info：``port``/``pid``/``sig`` **一律以应答为准**。

    应答没报 pid 时用 netstat 的属主 pid——缓存里的 pid 不许在应答之后继续冒充身份。
    """
    info = dict(_read_info() or {})
    port = int(scan.get("port") or 0)
    if port:
        info["port"] = port
    got = scan.get("got") or {}
    answered = _answer_pid(got) or int(scan.get("owner") or 0)
    if answered:
        info["pid"] = answered
    if got.get("sig"):
        info["sig"] = str(got["sig"])
    return info


def status() -> Dict[str, Any]:
    """daemon 的完整状态（后端 daemon 管理面与 CLI 共用）。

    状态只从**端口窗口**推出来：缓存（`jvm_daemon.json`）里只有 boot 时间与日志路径，
    pid 一律用应答里那个；没应答时用 netstat 给出的属主 pid（那是**观测**，不是我们的结论）。
    """
    info = _read_info()
    if not isinstance(info, dict):
        info = {}
    scan = _scan()
    got = scan.get("got")
    port = int(scan.get("port") or 0)
    # 应答里的 pid 优先；它没报 pid 时用 netstat 的属主兜底——展示与 `stop_allowed`
    # 的闸门都要一个「这台引擎是谁」，而扫描是**观测**（见 backend/api/jvm.py 的判据）
    pid = _answer_pid(got) or int(scan.get("owner") or 0)
    occupied = dict(scan.get("occupied") or {})
    # 两个轴**分开报**（identity_verified / signature_matches），别合并成一个：
    #   identity_verified   = 应答的是**我们的引擎**（`kind == "engine"`，与 pid 无关）
    #   signature_matches   = 它跑的类等不等于当前源码
    # 合成一个字段，界面就分不清该说「端口上不是我们的东西」还是「跑的是旧类，重启一下」。
    identity = bool(got)
    sig_matches = False
    if got:
        try:
            sig_matches = bool(got.get("sig") == source_sig())
        except (OSError, ValueError):
            sig_matches = False
    process_alive = bool(got or occupied)
    # 监听表读不到时**不许**报 stopped/missing：那是「没看见」，不是「没有」（AGENTS #12）。
    # 界面把 unknown 落到「状态未知」；按钮也随之隐藏（pid 为 None）。
    blind = bool(scan.get("blind"))
    if not got and occupied:
        # 端口被占但没人应答我们：可能它在忙/卡死，也可能是陌生程序。报「进程在但没响应」，
        # 并给出 netstat 看到的属主——**不猜**它是不是我们的。
        port = next(iter(occupied))
        pid = int(occupied[port])
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
        "state": ("unknown" if blind and not got else
                  "missing" if not info and not process_alive else
                  "stopped" if not process_alive else
                  "idle" if identity else "unresponsive"),
        "pid": pid or None,
        "port": port or None,
        "started_at": info.get("started_at"),
        "process_alive": process_alive,
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


def start(idle_sec: int = DEFAULT_IDLE_SEC,
          boot_timeout: int = DEFAULT_BOOT_TIMEOUT,
          port: int = 0,
          reason: str = "") -> Dict[str, Any]:
    """拉起一个 daemon 并等它就绪。失败抛 `DaemonError`（**带日志尾巴**，别让人对着空手猜）。

    ``port`` 不给就扫窗口取第一个空闲端口；``reason`` 只进日志——冷启是每 daemon 生命周期
    一次的入场费，「为什么这次要冷启」没有第二处能回答（2026-10-21 实测：日志只写
    「起 daemon」，10 次冷启里哪次是判据失败、哪次是进程树被外部杀掉，当时分不出来）。
    """
    with _LOCK:
        return _start_locked(idle_sec=idle_sec, boot_timeout=boot_timeout,
                             port=port, reason=reason)


def _start_locked(idle_sec: int = DEFAULT_IDLE_SEC,
                  boot_timeout: int = DEFAULT_BOOT_TIMEOUT,
                  port: int = 0,
                  reason: str = "") -> Dict[str, Any]:
    """boot 经 Gradle（唯一 boot 路径，方案甲）：`legado-gradle.bat` 起测试 JVM，
    测试阻塞 serve，Gradle 握着进程。**Gradle 必先编译**，所以 daemon 跑的永远是
    当前源码；env 来自 readiness（与 Gradle 回落那条同一份，不读原始环境变量——
    手工漏传 ANDROID_HOME 时启动器直接报错）。

    **kill 必须整树**（`_kill_proc`）：cmd → gradlew → Gradle daemon → 测试 JVM 四层，
    漏一层就留下「还占着 profile/端口」的孤儿。
    """
    global _PROC, _LOG
    from core import jvm_debug          # 惰性：jvm_debug 在函数内 import 本模块
    from core import jvm_env, settings_store
    conf = settings_store.load().get("jvm", {})
    readiness = jvm_env.readiness(conf.get("app_repo", ""), conf.get("android_sdk_dir", ""))
    if not readiness.get("ok"):
        bad = next((c for c in (readiness.get("checks") or []) if not c.get("ok")), {})
        raise DaemonError("本机引擎不可用：%s——%s"
                          % (bad.get("name") or "环境就绪检查未通过",
                             bad.get("hint") or "先完成 JVM 环境检查"))
    try:
        base_env = jvm_env.process_environment(readiness)
    except (KeyError, TypeError, ValueError) as exc:
        raise DaemonError("环境自检结果不完整：%s" % exc) from exc
    window = _daemon_window()
    if not port:
        scan = _scan()
        if scan.get("blind"):
            # 读不到监听表就**不猜**：盲起会撞上那个其实在监听的引擎，而那种失败
            # 只有 Gradle 的 bind 异常，看不出是「顺手把观测失败当成了没有」
            raise DaemonError("监听表读不到（netstat/lsof 失败），无法判断端口窗口 %d–%d "
                              "里有没有引擎；先修好它再起引擎" % (window[0], window[-1]))
        port = int(scan.get("free") or 0)
    if not port:
        raise DaemonError("端口窗口 %d–%d 没有空闲端口，无法拉起 daemon"
                          % (window[0], window[-1]))
    sig = source_sig()
    # `--no-daemon`：boot 是低频事件（daemon 热着时根本不走这里），让 Gradle 客户端
    # 进程自己跑构建——进程树完整归 `_PROC` 管，看门狗杀得干净；常驻 Gradle daemon
    # 脱离客户端树，杀完留僵尸是 2026-10-19 的实测（一次失败挂了 50 分钟）。
    # 编译增量与 configuration cache 仍在（它们在项目目录与全局缓存里，不依赖 daemon）。
    cmd = ["cmd", "/c", str(jvm_debug.LAUNCHER), BOOT_TASK,
           "--tests", DAEMON_CLASS, "--rerun", "--no-daemon"]
    env = {**base_env,
           "LEGADO_DAEMON_PORT": str(port),
           "LEGADO_DAEMON_IDLE_SEC": str(int(idle_sec)),
           "LEGADO_DAEMON_SIG": sig}
    log_path().parent.mkdir(parents=True, exist_ok=True)
    _LOG = open(log_path(), "a", encoding="utf-8")
    _LOG.write("\n==== %s 起 daemon（gradle boot）：port=%s sig=%s reason=%s\n"
               % (time.strftime("%F %T"), port, sig, reason or "cold"))
    _LOG.flush()
    proc = subprocess.Popen(cmd, cwd=str(jvm_debug.AGSVC), env=env,
                            stdout=_LOG, stderr=subprocess.STDOUT)
    _PROC = proc
    deadline = time.time() + max(5, boot_timeout)
    while time.time() < deadline:
        if proc.poll() is not None:
            _PROC = None
            raise DaemonError("daemon 启动即退出（码 %s）：%s" % (proc.returncode, _log_tail()))
        got = ping(port, timeout=0.3)
        if identity_ok(got, sig=sig):
            answered = _answer_pid(got)
            if not answered:
                # 应答少了 pid：宁可清场报错，也不许记一条假身份（老的 `or proc.pid`：
                # proc 是 `cmd /c legado-gradle.bat …` 的**包装进程**，与 JVM 不是一个数）
                residue = _hard_kill_boot(port, proc)
                _PROC = None
                raise DaemonError("daemon 就绪应答里没有 pid（%r），已清场%s"
                                  % (got, ("；" + residue) if residue else ""))
            # pid 记 **daemon 自己报的那个**（`op=ping` 回的 `ProcessHandle.current().pid()`），
            # 它只是 kill 目标与展示值，**不是身份**（身份只认应答 + sig）。
            info = {"pid": answered, "port": port, "sig": sig,
                    "idle_sec": int(idle_sec), "started_at": time.time()}
            _write_info(info)
            return info
        time.sleep(0.2)
    # 看门狗到点：整树杀 + 按端口补杀 + 验端口释放。残局必须可见（进日志与异常文案），
    # 不许像旧版那样静默——旧版 kill 失败留下的僵尸 Gradle 挂 50 分钟没人知道。
    residue = _hard_kill_boot(port, proc)
    note = ("；清场残局：%s" % residue) if residue else ""
    _LOG.write("[%s] boot 看门狗：%.0fs 超时，已清场%s\n"
               % (time.strftime("%F %T"), boot_timeout, ("；" + residue) if residue else ""))
    _LOG.flush()
    _PROC = None
    raise DaemonError("daemon %.0fs 内没起来（看 %s）%s" % (boot_timeout, log_path(), note))


def ensure(idle_sec: int = DEFAULT_IDLE_SEC,
           boot_timeout: int = DEFAULT_BOOT_TIMEOUT) -> Dict[str, Any]:
    """拿到一个**可用**的 daemon：窗口里应答身份对且版本对就复用，否则（重）起一个。

    身份从**端口应答**问（`kind` + `sig`，见 :func:`identity_ok`），**不读缓存的 pid**：
    记录里的 pid 曾经是包装进程、也被清掉过，拿它当身份会让判据恒假，表现是「每次都冷启
    一个」（2026-10-20 实测 ~28s/次）。缓存只用来补 `started_at` / 日志路径这类展示字段。

    sig 不符（进程里是旧类）→ 先收掉旧引擎再起；**停不掉就抛**，绝不"先起一个再说"——
    两个引擎会抢同一份浏览器 profile（后起的那个静默换临时 profile，cookie 全丢）。
    """
    with _LOCK:
        scan = _scan()
        got = scan.get("got")
        if got and identity_ok(got):
            return _scan_info(scan)
        reason = "port_occupied" if scan.get("occupied") else "no_engine"
        if got:
            answered = _answer_pid(got)
            result = _stop_port(int(scan["port"]), answered)
            if not result.get("stopped"):
                raise DaemonError(
                    "端口 %d 上的旧引擎停不掉（应答 pid=%s）：%s"
                    % (int(scan["port"]), answered or "未知",
                       result.get("reason") or "原因未记录"))
            reason = "sig_mismatch"
            scan = _scan()
        port = int(scan.get("free") or 0)
        if not port:
            window = _daemon_window()
            if scan.get("got"):
                # 停掉一个之后窗口里还有引擎在应答 → 再起就是第三个，宁可不判断
                raise DaemonError("停掉旧引擎后端口 %s 上仍有引擎在应答，不再起新的"
                                  % scan.get("port"))
            if scan.get("blind"):
                raise DaemonError("监听表读不到（netstat/lsof 失败），无法判断端口窗口 "
                                  "%d–%d 里有没有引擎；先修好它再起引擎"
                                  % (window[0], window[-1]))
            raise DaemonError("端口窗口 %d–%d 没有空闲端口，无法拉起 daemon"
                              % (window[0], window[-1]))
        return start(idle_sec=idle_sec, boot_timeout=boot_timeout,
                     port=port, reason=reason)


def probe() -> Optional[Dict[str, Any]]:
    """只读探测：窗口里有**我们的**引擎且版本匹配就返回 info；其余一律 None。

    与 ``ensure`` 的差别是**绝不杀进程、绝不重启**：ping 不通可能只是 daemon
    正忙于别人的请求（daemon 串行，忙时 ping 排队到超时），探测方不持有互斥，
    杀掉会伤及在跑的 op。调用方（批量块）拿 None 就报告 daemon 不可用。
    死活由批量开跑前的 ``prepare``（先分类再行动）或单条路径的 ``ensure``
    恢复，探测本身绝不杀。
    """
    scan = _scan()
    if identity_ok(scan.get("got")):
        return _scan_info(scan)
    return None


def _notify_wait(callback: Any, elapsed: float) -> None:
    """上报忙等待进度；回调失败只记日志，**不许**破坏 ``prepare`` 的「永不抛」。"""
    try:
        callback(round(elapsed, 1))
    except Exception as exc:                    # noqa: BLE001 - 回调不参与判定
        print("警告：忙等待进度回调失败（不影响等待）：%s" % exc, flush=True)


def prepare(boot_timeout: int = DEFAULT_BOOT_TIMEOUT,
            busy_wait_sec: float = 2.0,
            on_wait: Optional[Any] = None) -> Dict[str, Any]:
    """批量开跑前的一次性 daemon 准备：能复用就复用，该重启才重启。

    与 ``ensure`` 的差别是**先分类再行动**——``ensure`` 对「ping 不通」一律
    清掉重启，而 ping 不通可能只是 daemon 正串行忙别人的 op（见 ``probe``）。
    返回 ``{"outcome": ..., "info"?/, "reason"?}``，outcome 四档，**永不抛**：

    - ``ready``   热着且签名匹配：原样复用，不动任何进程。
    - ``started`` 原本没在跑（窗口里没有我们的引擎 / 有占位但不是我们的）或版本不符
                  （ping 通 = 正向确认是我们的 daemon 且此刻空闲 → 优雅停旧起新）。
    - ``busy``    缓存里那个端口还 LISTEN 着、属主进程也活着，但 ping 没应答：先按
                  ``busy_wait_sec`` 有界等待它空闲（daemon 串行处理 op，忙是瞬时的），
                  到点仍不确定就**不杀、不启**，调用方报告 busy 并保留当前操作归属。
    - ``failed``  启动尝试失败，reason 保留 ``DaemonError`` 原文。

    判「忙」= 缓存里那个端口还在监听且属主进程活着（缓存只是提示，见 ``_hint_port``）；
    判「死」= 端口没了或属主进程没了。其余一律不杀。
    """
    try:
        scan = _scan()
        got = scan.get("got")
        if got:
            if identity_ok(got):
                return {"outcome": "ready", "info": _scan_info(scan)}
            # ping 通 = 此刻空闲且身份已确认（kind=="engine"），优雅停旧起新；
            # ensure 在这条路上走不到它的杀分支
            return {"outcome": "started",
                    "info": ensure(boot_timeout=boot_timeout)}
        hint = _hint_port()
        occupied = dict(scan.get("occupied") or {})
        owner = int(occupied.get(hint) or 0) if hint else 0
        if owner:
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
                got = ping(hint, timeout=0.25)
                if got:
                    if identity_ok(got):
                        return {"outcome": "ready",
                                "info": _scan_info({"port": hint, "got": got})}
                    return {"outcome": "started",
                            "info": ensure(boot_timeout=boot_timeout)}
                if not _pid_alive(owner):
                    return {"outcome": "started",
                            "info": ensure(boot_timeout=boot_timeout)}
                time.sleep(0.1)
            owner_op = _read_current_operation()
            if owner_op:
                job = str(owner_op.get("job_id") or "未记录")
                chunk = str(owner_op.get("chunk") or "未记录")
                source = str(owner_op.get("source_name") or "源名称未记录")
                age = max(0.0, time.time() - float(owner_op.get("started_at") or time.time()))
                detail = ("；当前操作 job=%s chunk=%s source=%s 已运行 %.1fs"
                          % (job, chunk, source, age))
            else:
                detail = "；当前操作归属未知"
            return {"outcome": "busy",
                    "reason": ("端口 %d 被占（属主 pid=%d）但在 %.1fs 内没应答我们，"
                               "本批不准备也不杀%s" %
                               (hint, owner, max(0.0, float(busy_wait_sec)), detail))}
        # 窗口里没有应答的引擎、也没有「像是我们的」占位 → 交给 ensure 起一个
        return {"outcome": "started",
                "info": ensure(boot_timeout=boot_timeout)}
    except DaemonError as exc:
        return {"outcome": "failed", "reason": str(exc)}


def resolve(boot_timeout: int = DEFAULT_BOOT_TIMEOUT,
            busy_wait_sec: float = 2.0,
            on_wait: Optional[Any] = None) -> Dict[str, Any]:
    """``prepare`` 的**抛异常版本**：取到可用 daemon 的 info，忙/失败则抛。

    存在的理由是 ``ensure`` 的一个真实危害：它对「ping 不通」一律杀进程重启，
    而 ping 不通可能只是 daemon 正串行跑**别人**的 op——单条校验踩上去就会掐断
    那次执行（对方那一块可能仍在运行）。这里改成等一小会儿，仍忙就把
    原因原样抛出去，由调用方按任务执行策略处理，进程一个都不动。
    """
    got = prepare(boot_timeout=boot_timeout, busy_wait_sec=busy_wait_sec,
                  on_wait=on_wait)
    if got.get("outcome") in ("ready", "started"):
        return dict(got.get("info") or {})
    raise DaemonError(str(got.get("reason") or "daemon 未就绪"))


def run_validate(args_file: str, timeout_slack: int = 30,
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
    info = resolve(busy_wait_sec=busy_wait_sec)
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


def _stop_port(port: int, pid: int = 0) -> Dict[str, Any]:
    """让端口上的 daemon **优雅退出**（`op=stop`），必要时才动手 kill。

    返回 ``{"stopped": bool, "reason": str}``。**成功判据 = 端口静 + 进程没了**（两半都要）：
    只看「有应答」会把不认 `op=stop` 的旧类进程报成已停；只看「端口静」会把还在收尾的
    进程报成已停（两半都有实测）。

    闸门：只停「此刻在**我们的端口窗口**应答、且应答里报的就是 ``pid``」的那个进程——
    端口来自配置/扫描，应答者自证身份，**缓存的 pid 不参与**。传进来的 pid 对不上时
    **一个字节都不发**（那是「扫描之后应答者变了」，只能重扫，不能瞎杀）。

    **必须先试 `op=stop`**：`TerminateProcess` 不执行 `finally` —— 而 daemon 的 `finally`
    里要关 server、收浏览器、清残留。正在跑的那一瞬间被硬杀，那个 Chromium 会活下来
    **继续占着 profile**：之后任何一次抓页都「自愈」换临时 profile、**cookie 静默全丢**
    （实测：结论从 3 段变 1 段，还白等 18s 超时）。所以能优雅停就优雅停。

    失败原因**逐字返回**（带 daemon 自己的话）：`ensure` 把它抛给用户，`stop()` 靠日志
    行留下同一句——一律不许在中途换成一句笼统文案（AGENTS #4）。
    """
    got = ping(port, timeout=1.0)
    if not got:
        return {"stopped": False, "reason": "端口 %d 上没有 daemon 应答" % port}
    answered = _answer_pid(got)
    if pid and answered != int(pid):
        return {"stopped": False,
                "reason": "端口 %d 上应答的是 pid=%s，不是要停的 pid=%s（拒绝误停）"
                          % (port, answered or "未知", pid)}
    # 强杀前要能证明「还是同一个进程实例」：pid 会被系统复用
    created = ""
    try:
        created = process_created(answered) if answered else ""
    except (OSError, ValueError, IndexError):
        created = ""
    graceful = False
    note = ""
    try:
        response = _exchange({"id": 0, "op": "stop"}, port, timeout=20)
        try:
            code = int(response.get("code", -1))
        except (TypeError, ValueError):
            code = -1                 # 应答形状不对 = 没停成，绝不能当成已停
        graceful = code == 0
        if not graceful:
            # 旧类不认 op=stop 时回的是「缺参数」——**那也是应答**，曾经被当成已停
            note = "daemon 拒绝停止（code=%s%s）" % (
                response.get("code"),
                ("：" + str(response.get("error"))) if response.get("error") else "")
    except DaemonError as exc:
        note = "op=stop 没走通（%s）" % exc
    # 我们那棵四层树的包装进程还活着 → 整树杀（`_kill_proc` 只看 `_PROC`，与 pid 参数无关）
    if _PROC is not None and _PROC.poll() is None:
        ok = bool(_kill_proc() or graceful)
        return {"stopped": ok, "reason": "" if ok else "整树杀没成功"}
    if not graceful and answered:
        if created:
            try:
                still_same = process_created(answered) == created
            except (OSError, ValueError, IndexError):
                still_same = False
            if not still_same:
                return {"stopped": False,
                        "reason": "pid=%s 的进程实例已变（可能被复用），拒绝强杀" % answered}
        try:
            os.kill(int(answered), signal.SIGTERM)   # Windows 上等价于 TerminateProcess
        except OSError as exc:
            return {"stopped": False, "reason": "强杀 pid=%s 失败：%s" % (answered, exc)}
    # **验它真的停了**（优雅退出要收浏览器，可能几秒）。只看「有应答」就报成功是
    # 同类错里最阴的一种：跑旧类的 daemon 不认 `op=stop`，会把指令当调试请求回一句
    # 「缺参数」——那也是一条应答（实测踩过，当时报的是「已停：True」）。
    for _ in range(50):
        if not ping(port, timeout=0.4):
            break
        time.sleep(0.2)
    else:
        reason = "端口 %d 仍在应答，daemon 没停" % port
        if note:
            reason += "：" + note
        return {"stopped": False, "reason": reason}
    # 另一半：**端口静 ≠ 进程没了**。优雅退出的收尾（关 server、收浏览器、清残留）可能还没
    # 走完，这时报「已停」同样是假报告（2026-10-21 实测：端口静了，JVM 还挂着）。
    # 有界等进程真的消失再报成功；等不到就如实报 False，**不擅自强杀**（policy 见 stop_idle）。
    for _ in range(50):
        if not _pid_alive(answered):
            return {"stopped": True, "reason": ""}
        time.sleep(0.2)
    return {"stopped": False,
            "reason": "端口 %d 已静，但进程 %s 仍没收尾（未强杀）" % (port, answered or "未知")}


def _log_stop_failure(reason: str) -> None:
    """把「为什么没停成」写进引擎日志：`stop()` 只回 bool，调用方拿不到理由（AGENTS #4）。"""
    if not reason:
        return
    try:
        with open(log_path(), "a", encoding="utf-8") as fh:
            fh.write("[%s] 停止未完成：%s\n" % (time.strftime("%F %T"), reason))
    except OSError:
        pass


def _port_owner_pid(port: int) -> int:
    """当前监听该端口的进程 pid；**0 = 没人监听，-1 = 读不到监听表**。

    看门狗用它定位杀树漏掉的 Gradle 测试 JVM。读不到时绝不能报 0——那等于
    「端口已释放」，会让看门狗把**没法确认**的残局说成清干净了（AGENTS #12）。
    """
    listening = _listen_map()
    if listening is None:
        return -1
    return int(listening.get(int(port)) or 0)


def _hard_kill_boot(port: int, proc: Optional[subprocess.Popen]) -> str:
    """boot 超时后的清场，返回一段**人看的诊断**（空串 = 清干净了）。

    三层手段、层层验证：
    ① `taskkill /F /T` 杀 cmd 树（cmd → gradlew 客户端）；
    ② ①够不到的 Gradle daemon / 测试 JVM 按**端口 owner**定位后补杀——2026-10-19 实测
       Gradle daemon 是故意脱离客户端树的常驻服务，只走 ① 会
       留下僵尸 Gradle 挂 50–80 分钟；
    ③ 最后验端口真的释放了；还占着就把残局写进返回值（调用方落日志，不许静默）。
    """
    notes: List[str] = []
    cmd_pid = proc.pid if proc is not None else 0
    if proc is not None:
        _kill_proc()
    deadline = time.time() + _KILL_VERIFY_SEC
    while time.time() < deadline:
        pid = _port_owner_pid(port)
        if pid == -1:
            notes.append("监听表读不到（netstat/lsof 失败），无法确认端口 %d 是否释放" % port)
            return "；".join(notes)
        if not pid:
            return ""
        if pid == cmd_pid:
            # 树杀刚打出去，端口收回有延迟；继续等（剩余窗口内端口消失即成功）
            time.sleep(0.5)
            continue
        try:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True, timeout=10)
        except OSError as exc:
            notes.append("补杀 pid=%s 失败：%s" % (pid, exc))
            break
        time.sleep(0.5)
    still = _port_owner_pid(port)
    if still == -1:
        notes.append("监听表读不到（netstat/lsof 失败），无法确认端口 %d 是否释放" % port)
    elif still:
        notes.append("端口 %d 在杀完后仍被 pid=%s 占用" % (port, still))
    return "；".join(notes)


def _kill_proc() -> bool:
    """硬杀 boot 进程。**Gradle boot 的进程是四层树**（cmd → gradlew → Gradle daemon
    → 测试 JVM）：`Popen.kill()` 只杀第一层，剩下的 Gradle daemon 与测试 JVM 变成
    「还占着 profile/端口」的孤儿（隔离判据）。Windows 走
    `taskkill /F /T`（jvm_exec 的 Gradle 进程同款）；POSIX 走进程组信号。
    """
    global _PROC, _LOG
    if _PROC is None:
        return False
    try:
        if _PROC.poll() is None:
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(_PROC.pid)],
                               capture_output=True, timeout=15)
            else:
                os.killpg(os.getpgid(_PROC.pid), signal.SIGTERM)
            _PROC.wait(timeout=15)
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
    """收掉端口窗口里的 daemon（CLI ``--daemon-stop`` 用）。

    **只停窗口里应答的那个进程**：缓存里的 pid 不参与（它可能是包装进程，也可能早被清掉）。
    停不掉要把原因写进日志——这个函数的签名是 bool，调用方拿不到理由（AGENTS #4）。
    """
    with _LOCK:
        scan = _scan()
        got = scan.get("got")
        ok = False
        reason = "端口窗口里没有应答的引擎"
        if got:
            result = _stop_port(int(scan["port"]), _answer_pid(got))
            ok = bool(result.get("stopped"))
            reason = str(result.get("reason") or "")
        _kill_proc()
        if ok:
            try:
                info_path().unlink()
            except OSError:
                pass
        else:
            _log_stop_failure(reason)
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
            # 身份来自**应答**（status 扫出来的 pid），不读缓存：缓存里可能是包装进程的旧 pid
            expected_pid = int(state.get("pid") or 0)
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
        if (int(latest.get("port") or 0) == port
                and int(latest.get("pid") or 0) in (0, pid)
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

def launcher_from_args(idle_sec: int = DEFAULT_IDLE_SEC,
                       boot_timeout: int = DEFAULT_BOOT_TIMEOUT,
                       slack_sec: int = 90,
                       on_note: Optional[Callable[[str], None]] = None,
                       fallback: Optional[Callable[[], Tuple[int, float, str, str]]] = None,
                       args_file: Optional[str] = None,
                       ) -> Callable[[], Tuple[int, float, str, str]]:
    """返回一个给 `run_jvm_debug(launcher=...)` 用的可调用对象：**优先 daemon，失败回落**。

    回落是**每次调用**判定的（不是启动时判一次）：daemon 半路死了、空闲退出了、版本被
    改了，下一次调用仍然能跑——用户看到的顶多是「这次慢一点」。

    `fallback` 是回落目标，**产品那条传 Gradle**（`core.jvm_debug.default_launcher`）——
    Gradle 一定会先编译，所以它是「无论如何都能跑对」的那条；不给就回落 Gradle 跑一次
    调试（与产品同一条）。提示语不区分回落叫什么：用户只需要知道「这次慢」，
    不需要知道拉起方式的名字。
    """
    def _run() -> Tuple[int, float, str, str]:
        t0 = time.time()
        cfg = params_from_args(args_file) if args_file else params_from_args()
        try:
            info = ensure(idle_sec=idle_sec, boot_timeout=boot_timeout)
            r = request(cfg, int(info["port"]), timeout=int(cfg["timeout"]) + slack_sec)
            return int(r.get("code", -1)), time.time() - t0, "", ""
        except Exception as e:
            # **没走成快速启动要说出来**：静默会把「快速通道一直没生效」藏起来（用户只看到
            # 「也没快多少」，下次还得再踩一遍）。措辞照界面的规矩：说结果，别用内部黑话
            # （「降级/回落/常驻进程/Gradle」这类词用户无从行动）。
            if on_note is not None:
                on_note("快速启动没走成（%s），这次用了慢速方式" % e)
            from core import jvm_debug          # 惰性：jvm_debug 在函数内 import 本模块
            fn = fallback or (lambda: jvm_debug._run_launcher(
                timeout_min=max(5, (int(cfg["timeout"]) + slack_sec + 60) // 60),
                args_path=pathlib.Path(args_file) if args_file else None))
            rc, _cost, so, se = fn()
            return rc, time.time() - t0, so, se

    return _run
