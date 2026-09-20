# -*- coding: utf-8 -*-
"""「不起 Gradle 直起一次 JVM」的原语（S5-A 第二期 D0 的产物，D1 起被常驻 daemon 复用）。

**为什么值得单独一个模块**：这套东西现在有两个消费方——CLI（`scripts/jvm_debug_direct.py`）
与常驻 daemon 的客户端（`core/jvm_daemon.py`），而常驻那条是**产品路径**（后端要用），
不能反过来 import `scripts/`。参数拼装（classpath / 系统属性 / jvmArgs / 堆）各写一份的
后果与 `core/jvm_debug` 当初拆出来的理由一模一样：两处各写一遍就会漂，而漂的表现是
「界面上跑出来的和命令行跑出来的不一样」。

## dump 是什么、为什么必须有它

`appservice/legado-test.init.gradle` 里挂了 `doFirst`：按环境变量
`LEGADO_TEST_JVM_ENV_OUT` 把**测试 JVM 的运行环境**（classpath / 系统属性 / jvmArgs /
workingDir）写成 JSON。绕开 Gradle 直起就得拿这份 dump——尤其**系统属性**：
Robolectric 靠 AGP 注入的那些定位 Android 资源，Gradle 里是隐式给的、看不见。

**dump 是编译后的产物**：改了 `appservice/` 的 Kotlin 要重新 `refresh()`，
`dump_is_stale()` 就是干这个判断的。

## 三条实测细节（别改回去）

- classpath 有 **625 项 / 75232 字符** → 直接拼命令行必撞 Windows 的 32767 上限，
  所以走 `java @argfile`；argfile 里 `\\` 要翻倍（JDK 的 `@file` 解析把它当转义符）。
- `javaLauncher` 的 `installationPath` 是 **JDK 根目录**，不是可执行文件。
- `maxHeapSize`（3g，为全量跑批 OOM 设的）**不在 jvmArgs 里**，得自己补 `-Xmx`。
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.jvm_debug import AGSVC, LAUNCHER, LAUNCHER_CLASS
from core.paths import data_path

DUMP_NAME = "test_jvm_env.json"
#: refresh 用最便宜的那个测试：dump 挂在**所有** test 任务上，跑哪个都能拿到，
#: 而 `DebugServiceLauncher` 会真跑一次调试，白花一次站点请求。
REFRESH_TEST = "io.legado.app.service.ServiceJsonTest"


def dump_path() -> pathlib.Path:
    return pathlib.Path(data_path("app_probe", DUMP_NAME))


def newest_kt() -> float:
    """`appservice/test` 下 Kotlin 源码的最新 mtime——判断 dump 是不是过期了。"""
    src = AGSVC / "test"
    return max((p.stat().st_mtime for p in src.rglob("*.kt")), default=0.0)


def dump_is_stale() -> bool:
    p = dump_path()
    return (not p.exists()) or newest_kt() > p.stat().st_mtime


def refresh(timeout_min: int = 30) -> int:
    """拉一次 Gradle（带 dump 的环境变量），把测试 JVM 的环境写出来。

    **环境变量是唯一开关**：不设它，`legado-test.init.gradle` 与现在完全一样。
    """
    out = dump_path()
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    env = {**os.environ, "LEGADO_TEST_JVM_ENV_OUT": str(out)}
    t0 = time.time()
    p = subprocess.run(
        ["cmd", "/c", str(LAUNCHER), ":app:testAppDebugUnitTest", "--tests", REFRESH_TEST,
         "--rerun"],
        cwd=str(AGSVC), env=env, capture_output=True, text=True, errors="replace",
        timeout=timeout_min * 60)
    cost = time.time() - t0
    if not out.exists():
        print("dump 失败（退出码 %d，%.1fs）——看 Gradle 输出里 [appservice] 那几行"
              % (p.returncode, cost))
        for ln in (p.stdout or "").strip().splitlines()[-8:]:
            print("  | " + ln)
        return 1
    print("dump 完成：%s（%.1fs）" % (out, cost))
    return 0


def load_dump(warn_stale: bool = True) -> Dict[str, Any]:
    p = dump_path()
    if not p.exists():
        raise FileNotFoundError(
            "没有 dump（%s）：先跑一次 `scripts/jvm_debug_direct.py --refresh`" % p)
    dump = json.loads(p.read_text(encoding="utf-8"))
    if warn_stale and dump_is_stale():
        print("⚠️  dump 早于 appservice 的 Kotlin 源码——类可能不是最新的，"
              "建议先 `--refresh`（否则时间与结论都不是真实产物的）")
    return dump


def java_exe(dump: Dict[str, Any]) -> str:
    """用 Gradle 自己会 fork 的那个 JVM（dump 里的 `javaLauncher`），其次 JAVA_HOME。

    **`javaLauncher` 是 JDK 根目录**（`installationPath`），要自己拼 `bin/java`。
    """
    exe_name = "java.exe" if os.name == "nt" else "java"
    cand = dump.get("javaLauncher")
    if cand:
        p = pathlib.Path(str(cand))
        return str(p / "bin" / exe_name) if p.is_dir() else str(p)
    jh = (dump.get("javaHomeEnv") or os.environ.get("JAVA_HOME") or "").rstrip("\\/")
    if jh:
        exe = pathlib.Path(jh) / "bin" / exe_name
        if exe.exists():
            return str(exe)
    return "java"


def _quote(arg: str) -> str:
    return '"' + arg.replace("\\", "\\\\").replace('"', '\\"') + '"'


def java_argv(dump: Dict[str, Any], main_class: str = LAUNCHER_CLASS) -> List[str]:
    argv: List[str] = []
    heap = str(dump.get("maxHeapSize") or "").strip()
    # 堆要显式带上：它是 Test 任务的另一个属性、不在 jvmArgs 里，而它正是为
    # 「全量跑批 OOM」设的 3g——漏了就等于悄悄把堆退回默认值（实测过）
    if heap:
        argv.append("-Xmx" + heap)
    argv += [a for a in (dump.get("jvmArgs") or []) if str(a).strip()]
    for k, v in (dump.get("systemProperties") or {}).items():
        if v is None:      # 空值不能写 -Dk=null：那是把 null 当字符串传进去
            continue
        argv.append("-D%s=%s" % (k, v))
    argv += ["-cp", dump["classpath"]]
    argv += ["org.junit.runner.JUnitCore", main_class]
    return argv


def write_argfile(argv: List[str], path: Optional[pathlib.Path] = None) -> pathlib.Path:
    p = path or (dump_path().parent / "jvm_direct.args")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(_quote(a) for a in argv) + "\n", encoding="utf-8", newline="\n")
    return p


def java_command(dump: Dict[str, Any], main_class: str = LAUNCHER_CLASS,
                 use_argfile: bool = True) -> List[str]:
    """完整命令行：`[java, @argfile]`（或展开的参数，`use_argfile=False` 时）。"""
    argv = java_argv(dump, main_class)
    if use_argfile:
        return [java_exe(dump), "@" + str(write_argfile(argv))]
    return [java_exe(dump)] + argv


def java_env(dump: Dict[str, Any]) -> Dict[str, str]:
    return {**os.environ, **(dump.get("environment") or {})}


def run_direct(dump: Dict[str, Any], main_class: str = LAUNCHER_CLASS, timeout: int = 300,
               use_argfile: bool = True) -> Tuple[int, float, str, str]:
    """直起一次，返回 `(退出码, 墙钟秒, stdout, stderr)`（签名同 `_run_launcher`）。"""
    cmd = java_command(dump, main_class, use_argfile=use_argfile)
    t0 = time.time()
    p = subprocess.run(cmd, cwd=dump.get("workingDir") or None, env=java_env(dump),
                       capture_output=True, text=True, errors="replace", timeout=timeout)
    return p.returncode, time.time() - t0, (p.stdout or ""), (p.stderr or "")


def direct_launcher(dump: Dict[str, Any], timeout: int = 300) -> Callable[[], Tuple[int, float, str, str]]:
    """给 `run_jvm_debug(launcher=...)` 用的可调用对象。"""
    def _run() -> Tuple[int, float, str, str]:
        return run_direct(dump, LAUNCHER_CLASS, timeout=timeout)
    return _run
